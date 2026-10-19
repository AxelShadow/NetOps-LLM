"""Индексатор runbook'ов: обход каталога, чанки, Qdrant, статус.

Контракт:

* `index_status()` -> dict для админки:
  `{enabled, docs_dir, documents, chunks, last_indexed_at, qdrant_ok,
    embedding_model, error}`;
* `reindex(force=False)` -> dict результата:
  `{indexed, skipped, deleted, chunks, duration_ms, errors: [str]}`;
  инкрементально: файл с тем же sha256 пропускается, изменённый —
  перезаписывается, удалённый — удаляется из Qdrant и `rag_documents`;
* каталог читается только внутри `NETOPS_RAG_DOCS_DIR` (защита от обхода
  путей: относительный путь, `..` и симлинки наружу отбрасываются);
* расширения `.md`, `.markdown`, `.txt`; файлы больше
  `NETOPS_RAG_MAX_FILE_BYTES` пропускаются с записью в `errors`.

Безопасность: имена файлов в `errors` — относительные пути внутри каталога
документов (без содержимого документов и без секретов).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import os
import time
from pathlib import Path

from ..config import get_settings
from ..db import SessionLocal
from ..models import RagDocument
from .chunking import chunk_markdown
from .embeddings import EmbeddingError, build_embedding_provider
from .store import QdrantError, QdrantStore, build_store, point_id

log = logging.getLogger(__name__)

# Что индексируем: текстовые runbook'и. Остальные файлы каталога игнорируем.
SUPPORTED_SUFFIXES = frozenset({".md", ".markdown", ".txt"})

# Сколько текстов отправлять в один запрос /embeddings.
_EMBED_BATCH = 64

# Дефолт лимита размера файла (совпадает с config.py::rag_max_file_bytes).
# Используется, если настройка неположительна: 0/отрицательное значение не
# должно ОТКЛЮЧАТЬ защиту от чтения гигантского файла в память.
DEFAULT_MAX_FILE_BYTES = 1024 * 1024


class IndexerError(RuntimeError):
    """Ошибка индексации (каталог, чтение файла, хранилище)."""


def _runtime_enabled() -> bool:
    """Мок-режим включает RAG «виртуально» (эмбеддинги — MockEmbeddings)."""
    settings = get_settings()
    if settings.mock_mode:
        return True
    return settings.rag_enabled


def _file_size(path: Path) -> int:
    """Размер файла без чтения содержимого (отдельная функция — для тестов)."""
    return path.stat().st_size


def _max_file_bytes(settings) -> int:
    """Действующий лимит размера файла (неположительное значение -> дефолт).

    `NETOPS_RAG_MAX_FILE_BYTES <= 0` трактуем как «применить дефолт 1 МиБ»,
    а не «без лимита»: иначе один огромный файл в каталоге runbook'ов
    вызовет рост памяти индексатора (DoS). О некорректном значении
    предупреждаем в лог, в `errors` шум не добавляем.
    """
    try:
        value = int(settings.rag_max_file_bytes or 0)
    except (TypeError, ValueError):
        value = 0
    if value <= 0:
        log.warning(
            "NETOPS_RAG_MAX_FILE_BYTES=%s некорректен: применяю дефолт "
            "%s байт", settings.rag_max_file_bytes, DEFAULT_MAX_FILE_BYTES)
        return DEFAULT_MAX_FILE_BYTES
    return value


def docs_dir_path() -> Path:
    """Путь каталога документов (Path.resolve) — общий для статуса и админки."""
    raw = (get_settings().rag_docs_dir or "").strip()
    if not raw:
        raise IndexerError(
            "Не задан каталог runbook'ов (NETOPS_RAG_DOCS_DIR)")
    try:
        return Path(raw).expanduser().resolve()
    except OSError as exc:
        raise IndexerError(
            f"Не удалось определить каталог runbook'ов ({type(exc).__name__})"
        ) from exc


def _build_store() -> QdrantStore:
    """Фабрика клиента Qdrant — отдельная функция ради подмены в тестах."""
    return build_store()


def _safe_source(root: Path, file_path: Path) -> tuple[str | None, str | None]:
    """Абсолютный путь файла -> относительный source внутри каталога.

    Возвращает `(source, None)` либо `(None, причина)`. Защита от обхода
    путей: `Path.resolve()` раскрывает `..` и симлинки, после чего путь
    обязан остаться внутри уже разрешённого `root`. Симлинк наружу
    (в т.ч. на файл вне каталога runbook'ов) отбрасывается.
    """
    try:
        resolved = file_path.resolve(strict=True)
    except OSError as exc:
        return None, f"{file_path.name}: путь не разрешается ({type(exc).__name__})"
    try:
        relative = resolved.relative_to(root)
    except ValueError:
        return None, f"{file_path.name}: путь вне каталога документов отброшен"
    if not relative.parts or any(part in ("", "..") for part in relative.parts):
        return None, f"{file_path.name}: подозрительный путь отброшен"
    return relative.as_posix(), None


def _iter_files(root: Path) -> list[Path]:
    """Все файлы каталога без перехода по симлинкам каталогов."""
    files: list[Path] = []
    if not root.is_dir():
        return files
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            files.append(Path(dirpath) / name)
    return files


def _embed_in_batches(provider, texts: list[str]) -> list[list[float]]:
    """Эмбеддинги пачками: большой файл не должен уходить одним запросом."""
    vectors: list[list[float]] = []
    for start in range(0, len(texts), _EMBED_BATCH):
        batch = texts[start:start + _EMBED_BATCH]
        batch_vectors = provider.embed_texts(batch)
        if len(batch_vectors) != len(batch):
            raise EmbeddingError(
                "Провайдер эмбеддингов вернул неверное число векторов: "
                f"ожидалось {len(batch)}")
        vectors.extend(batch_vectors)
    return vectors


def index_status() -> dict:
    """Статус индекса для админки (без исключений наружу)."""
    settings = get_settings()
    status: dict = {
        "enabled": _runtime_enabled(),
        "docs_dir": "",
        "documents": 0,
        "chunks": 0,
        "last_indexed_at": None,
        "qdrant_ok": False,
        "qdrant_points": 0,
        "embedding_model": settings.embedding_model or settings.llm_default_model,
        "error": None,
    }

    try:
        root = docs_dir_path()
    except IndexerError as exc:
        status["error"] = str(exc)
        return status
    status["docs_dir"] = str(root)
    if not root.is_dir():
        status["error"] = "Каталог документов не найден"
    elif not any(p.suffix.lower() in SUPPORTED_SUFFIXES for p in _iter_files(root)):
        status["error"] = "В каталоге документов нет файлов .md/.markdown/.txt"

    # Состояние индекса в БД: документы/чанки/время последней индексации.
    try:
        with SessionLocal() as db:
            rows = db.query(RagDocument).all()
            status["documents"] = len(rows)
            status["chunks"] = sum(int(row.chunks or 0) for row in rows)
            last = max((row.indexed_at for row in rows if row.indexed_at),
                       default=None)
            if isinstance(last, dt.datetime):
                status["last_indexed_at"] = last.isoformat(timespec="seconds")
    except Exception as exc:  # noqa: BLE001 - статус не должен падать
        log.warning("Не удалось прочитать статус RAG из БД: %s",
                    type(exc).__name__)
        status["error"] = status["error"] or (
            f"БД индекса недоступна ({type(exc).__name__})")

    # Qdrant опрашиваем только при включённом RAG: выключенная фича не
    # должна тратить секунды на сетевой таймаут в админке.
    if status["enabled"]:
        try:
            store = _build_store()
            status["qdrant_ok"] = bool(store.health())
            if status["qdrant_ok"]:
                info = store.collection_info()
                status["qdrant_points"] = int(info.get("points_count") or 0)
        except QdrantError as exc:
            status["qdrant_ok"] = False
            status["error"] = status["error"] or f"Qdrant: {exc}"

    return status


def reindex(force: bool = False) -> dict:
    """Переиндексация каталога runbook'ов (инкрементально по sha256).

    Не бросает известных ошибок: сбои файла/Qdrant/эмбеддингов попадают в
    `errors`, остальные файлы индексируются дальше.
    """
    started = time.monotonic()
    settings = get_settings()
    result: dict = {
        "ok": False,
        "indexed": 0,
        "skipped": 0,
        "deleted": 0,
        "chunks": 0,
        "documents": 0,
        "duration_ms": 0,
        "errors": [],
        "message": "",
    }

    def finish() -> dict:
        result["duration_ms"] = int((time.monotonic() - started) * 1000)
        result["ok"] = not result["errors"]
        if result["ok"]:
            result["message"] = (
                f"Индексирование завершено: файлов обработано "
                f"{result['indexed']}, пропущено {result['skipped']}, "
                f"удалено {result['deleted']}, чанков {result['chunks']}")
        else:
            result["message"] = (
                "Индексирование завершено с ошибками: "
                f"{len(result['errors'])}")
        return result

    if not _runtime_enabled():
        result["errors"].append("RAG выключен (NETOPS_RAG_ENABLED=false)")
        return finish()

    try:
        root = docs_dir_path()
    except IndexerError as exc:
        result["errors"].append(str(exc))
        return finish()

    if not root.is_dir():
        result["errors"].append(f"Каталог документов не найден: {root}")
        return finish()

    try:
        provider = build_embedding_provider()
    except (EmbeddingError, QdrantError) as exc:
        result["errors"].append(f"Эмбеддинги недоступны: {exc}")
        return finish()

    try:
        store = _build_store()
        store.ensure_collection(provider.dim)
    except QdrantError as exc:
        result["errors"].append(f"Qdrant: {exc}")
        return finish()

    max_bytes = _max_file_bytes(settings)
    try:
        with SessionLocal() as db:
            existing = {row.source: row
                        for row in db.query(RagDocument).all()}
            seen: set[str] = set()

            for file_path in _iter_files(root):
                if file_path.suffix.lower() not in SUPPORTED_SUFFIXES:
                    continue

                source, problem = _safe_source(root, file_path)
                if source is None:
                    result["errors"].append(problem)
                    continue
                # source считается «есть на диске»: если файл не прочитался,
                # старая версия индекса сохраняется, а не удаляется.
                seen.add(source)

                # Размер проверяем ДО чтения: файл больше лимита не должен
                # попадать в память целиком (DoS даже при кривой настройке).
                try:
                    size = _file_size(file_path)
                except OSError as exc:
                    result["errors"].append(
                        f"{source}: не удалось получить размер файла "
                        f"({type(exc).__name__})")
                    continue
                if size > max_bytes:
                    result["errors"].append(
                        f"{source}: файл больше лимита "
                        f"NETOPS_RAG_MAX_FILE_BYTES ({max_bytes} байт), "
                        "пропущен")
                    continue

                # Читаем не более лимита + 1 байт: защита от гонки, когда файл
                # вырос между stat() и чтением. Больше лимита — не читаем.
                try:
                    with file_path.open("rb") as handle:
                        data = handle.read(max_bytes + 1)
                except OSError as exc:
                    result["errors"].append(
                        f"{source}: не удалось прочитать файл "
                        f"({type(exc).__name__})")
                    continue
                if len(data) > max_bytes:
                    result["errors"].append(
                        f"{source}: файл больше лимита "
                        f"NETOPS_RAG_MAX_FILE_BYTES ({max_bytes} байт), "
                        "пропущен")
                    continue
                size = len(data)

                digest = hashlib.sha256(data).hexdigest()
                row = existing.get(source)
                if (not force and row is not None and row.sha256 == digest
                        and int(row.chunks or 0) > 0):
                    result["skipped"] += 1
                    continue

                text = data.decode("utf-8", errors="replace")
                chunks = chunk_markdown(
                    text,
                    max_chars=settings.rag_chunk_chars,
                    overlap=settings.rag_chunk_overlap,
                )
                if not chunks:
                    # Пустой документ: в индексе ему делать нечего.
                    try:
                        store.delete_by_source(source)
                    except QdrantError as exc:
                        result["errors"].append(
                            f"{source}: Qdrant при очистке ({exc})")
                        continue
                    if row is not None:
                        db.delete(row)
                        existing.pop(source, None)
                        result["deleted"] += 1
                    continue

                try:
                    vectors = _embed_in_batches(
                        provider, [chunk.text for chunk in chunks])
                except EmbeddingError as exc:
                    result["errors"].append(
                        f"{source}: эмбеддинги недоступны ({exc})")
                    continue

                points = [{
                    "id": point_id(source, chunk.index),
                    "vector": vector,
                    "payload": {
                        "source": source,
                        "heading": chunk.heading,
                        "chunk": chunk.index,
                        "text": chunk.text,
                        "sha256": digest,
                    },
                } for chunk, vector in zip(chunks, vectors)]

                try:
                    # Сначала чистим старые точки: если чанков стало меньше,
                    # «хвостовые» точки иначе остались бы в коллекции.
                    store.delete_by_source(source)
                    store.upsert(points)
                except QdrantError as exc:
                    result["errors"].append(f"{source}: Qdrant ({exc})")
                    continue

                now = dt.datetime.now(dt.UTC)
                if row is None:
                    row = RagDocument(source=source)
                    db.add(row)
                row.sha256 = digest
                row.size_bytes = size
                row.chunks = len(chunks)
                row.indexed_at = now
                existing[source] = row
                result["indexed"] += 1
                result["chunks"] += len(chunks)

            # Файлы, исчезнувшие с диска, вычищаем из Qdrant и rag_documents.
            for source, row in list(existing.items()):
                if source in seen:
                    continue
                try:
                    store.delete_by_source(source)
                except QdrantError as exc:
                    result["errors"].append(
                        f"{source}: Qdrant при удалении ({exc})")
                    continue
                db.delete(row)
                existing.pop(source, None)
                result["deleted"] += 1

            db.commit()
            result["documents"] = db.query(RagDocument).count()
    except Exception as exc:  # noqa: BLE001 - фасад не должен падать 500-й
        log.exception("Ошибка индексатора RAG")
        # Только тип исключения: текст ошибки БД/драйвера может содержать
        # детали подключения (см. .dsh/security/policy.md).
        result["errors"].append(
            f"Внутренняя ошибка индексатора ({type(exc).__name__})")

    return finish()
