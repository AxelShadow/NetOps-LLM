"""Юнит-тесты индексатора runbook'ов (`app/rag/indexer.py`).

Без сети и без Qdrant: хранилище подменяется `FakeStore`, эмбеддинги —
`MockEmbeddings`, БД — отдельная временная sqlite. Каталог runbook'ов —
`tmp_path`.

Проверяется: индексация и чанки, инкремент по sha256, удаление исчезнувших
файлов, защита от обхода путей/симлинков, лимит размера, несовпадение
размерности, выключенный RAG, `force`.

Запуск: cd backend; .\\.venv\\Scripts\\python.exe -m pytest tests/test_rag_indexer.py -v
"""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

# env ДО импорта app.*: Settings требует jwt_secret и llm_base_url.
TMP = tempfile.mkdtemp(prefix="netops_rag_indexer_")
os.environ["NETOPS_DATABASE_URL"] = f"sqlite:///{TMP}/rag_indexer.db".replace("\\", "/")
os.environ["NETOPS_JWT_SECRET"] = "rag-indexer" + "-test"
os.environ["NETOPS_LLM_BASE_URL"] = "http://llm.invalid:9/v1"
os.environ["NETOPS_MOCK_MODE"] = "false"

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import Base  # noqa: E402
from app.models import RagDocument  # noqa: E402
from app.rag import indexer  # noqa: E402
from app.rag.embeddings import MockEmbeddings  # noqa: E402
from app.rag.store import QdrantError, point_id  # noqa: E402


class FakeStore:
    """In-memory замена QdrantStore: те же методы, без сети."""

    def __init__(self, dim: int = 8):
        self.dim = dim
        self.points: dict[str, dict] = {}
        self.upsert_calls: list[list[dict]] = []
        self.deleted: list[str] = []
        self.ensure_dims: list[int] = []
        self.health_calls = 0

    def ensure_collection(self, dim: int) -> None:
        self.ensure_dims.append(int(dim))
        if int(dim) != self.dim:
            raise QdrantError(
                f"Размерность коллекции Qdrant ({self.dim}) не совпадает с "
                f"размерностью эмбеддингов ({dim})")

    def upsert(self, points: list[dict]) -> None:
        self.upsert_calls.append(points)
        for point in points:
            self.points[point["id"]] = point

    def delete_by_source(self, source: str) -> None:
        self.deleted.append(source)
        for point_id_, point in list(self.points.items()):
            if (point.get("payload") or {}).get("source") == source:
                del self.points[point_id_]

    def health(self) -> bool:
        self.health_calls += 1
        return True

    def collection_info(self) -> dict:
        return {"exists": True, "points_count": len(self.points)}


def _write(docs: Path, name: str, text: str) -> Path:
    path = docs / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture()
def rag(monkeypatch):
    """Каталог runbook'ов, временная sqlite и подменённые Qdrant/эмбеддинги.

    Каталог создаётся через `tempfile.mkdtemp()`: pytest-фикстура `tmp_path`
    в этой Windows-среде падает по правам DACL (см. §9 дизайн-документа).
    """
    workdir = Path(tempfile.mkdtemp(prefix="netops_rag_work_"))
    docs = workdir / "runbooks"
    docs.mkdir()
    settings = get_settings()
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "rag_enabled", True)
    monkeypatch.setattr(settings, "rag_docs_dir", str(docs))
    monkeypatch.setattr(settings, "rag_max_file_bytes", 4096)
    monkeypatch.setattr(settings, "rag_chunk_chars", 400)
    monkeypatch.setattr(settings, "rag_chunk_overlap", 0)

    engine = create_engine(f"sqlite:///{workdir / 'index.db'}")
    Base.metadata.create_all(engine)
    test_session = sessionmaker(bind=engine)
    monkeypatch.setattr(indexer, "SessionLocal", test_session)
    monkeypatch.setattr(indexer, "build_embedding_provider",
                        lambda: MockEmbeddings(dim=8))
    store = FakeStore(dim=8)
    monkeypatch.setattr(indexer, "_build_store", lambda: store)

    yield SimpleNamespace(
        docs=docs, settings=settings, store=store,
        session=test_session, tmp_path=workdir, engine=engine,
    )
    engine.dispose()
    shutil.rmtree(workdir, ignore_errors=True)


def _rows(rag) -> dict[str, RagDocument]:
    with rag.session() as db:
        return {row.source: row for row in db.query(RagDocument).all()}


# ---------------------------------------------------------------------------
# индексация и чанки
# ---------------------------------------------------------------------------

def test_reindex_indexes_markdown_and_text(rag):
    _write(rag.docs, "network/check-link.md",
           "# Проверка линка\n\nШаг 1: смотрим статус интерфейса.\n\n"
           "## Zabbix\n\nСмотрим активные алерты.")
    _write(rag.docs, "plain.txt",
           "Простой текст runbook'а.\n\nВторой абзац с деталями.")
    _write(rag.docs, "ignored.pdf", "это не индексируется")

    result = indexer.reindex()

    assert result["ok"] is True
    assert result["errors"] == []
    assert result["indexed"] == 2
    assert result["skipped"] == 0
    assert result["deleted"] == 0
    assert result["chunks"] == len(rag.store.points)
    assert result["documents"] == 2
    assert rag.store.ensure_dims == [8]

    sources = {p["payload"]["source"] for p in rag.store.points.values()}
    assert sources == {"network/check-link.md", "plain.txt"}

    first = next(p for p in rag.store.points.values()
                 if p["payload"]["source"] == "network/check-link.md")
    assert first["id"] == point_id(
        "network/check-link.md", first["payload"]["chunk"])
    assert first["payload"]["heading"].startswith("Проверка линка")
    assert len(first["payload"]["text"]) <= rag.settings.rag_chunk_chars
    assert len(first["vector"]) == 8
    assert len(first["payload"]["sha256"]) == 64

    rows = _rows(rag)
    assert set(rows) == {"network/check-link.md", "plain.txt"}
    assert rows["plain.txt"].chunks >= 1
    assert len(rows["plain.txt"].sha256) == 64
    assert rows["plain.txt"].size_bytes > 0
    assert isinstance(rows["plain.txt"].indexed_at, dt.datetime)

    status = indexer.index_status()
    assert status["enabled"] is True
    assert status["docs_dir"] == str(rag.docs.resolve())
    assert status["documents"] == 2
    assert status["chunks"] == result["chunks"]
    assert status["qdrant_ok"] is True
    assert status["qdrant_points"] == len(rag.store.points)
    assert status["last_indexed_at"]
    assert status["error"] is None


def test_reindex_uses_sha256_of_file_content(rag):
    content = "# Раздел\n\nТекст для проверки sha256."
    path = _write(rag.docs, "doc.md", content)
    indexer.reindex()
    expected = hashlib.sha256(path.read_bytes()).hexdigest()
    assert _rows(rag)["doc.md"].sha256 == expected


def test_cyrillic_content_preserved_in_payload(rag):
    _write(rag.docs, "ru.md", "# Регламент\n\nПерезагрузите оборудование.")
    indexer.reindex()
    texts = " ".join(p["payload"]["text"] for p in rag.store.points.values())
    assert "Перезагрузите оборудование" in texts


# ---------------------------------------------------------------------------
# инкремент по sha256
# ---------------------------------------------------------------------------

def test_incremental_reindex_skips_unchanged(rag):
    _write(rag.docs, "a.md", "# Раздел\n\nТекст A.")
    _write(rag.docs, "b.md", "# Раздел\n\nТекст B.")
    first = indexer.reindex()
    assert first["indexed"] == 2
    upserts_after_first = len(rag.store.upsert_calls)

    second = indexer.reindex()
    assert second["ok"] is True
    assert second["indexed"] == 0
    assert second["skipped"] == 2
    assert len(rag.store.upsert_calls) == upserts_after_first

    # Изменён ровно один файл: переиндексируется только он.
    _write(rag.docs, "a.md",
           "# Раздел\n\nТекст A изменён.\n\n## Добавка\n\nНовый текст.")
    third = indexer.reindex()
    assert third["indexed"] == 1
    assert third["skipped"] == 1
    assert len(rag.store.upsert_calls) == upserts_after_first + 1
    assert "a.md" in rag.store.deleted
    texts = " ".join(p["payload"]["text"] for p in rag.store.points.values()
                     if p["payload"]["source"] == "a.md")
    assert "изменён" in texts


def test_force_reindexes_unchanged_files(rag):
    _write(rag.docs, "a.md", "# Раздел\n\nТекст A.")
    assert indexer.reindex()["indexed"] == 1
    forced = indexer.reindex(force=True)
    assert forced["indexed"] == 1
    assert forced["skipped"] == 0
    assert len(rag.store.upsert_calls) == 2


# ---------------------------------------------------------------------------
# удаление исчезнувших файлов
# ---------------------------------------------------------------------------

def test_removed_file_cleans_qdrant_and_db(rag):
    _write(rag.docs, "a.md", "# A\n\nтекст A")
    _write(rag.docs, "b.md", "# B\n\nтекст B")
    indexer.reindex()

    (rag.docs / "b.md").unlink()
    result = indexer.reindex()

    assert result["deleted"] == 1
    assert not any(p["payload"]["source"] == "b.md"
                   for p in rag.store.points.values())
    assert set(_rows(rag)) == {"a.md"}
    assert "b.md" in rag.store.deleted


def test_empty_document_creates_no_points(rag):
    _write(rag.docs, "empty.md", "   \n\n  \n")
    result = indexer.reindex()
    assert result["ok"] is True
    assert result["indexed"] == 0
    assert rag.store.points == {}
    assert _rows(rag) == {}


# ---------------------------------------------------------------------------
# защита от обхода путей и симлинков
# ---------------------------------------------------------------------------

def test_safe_source_rejects_path_outside_docs_dir(rag):
    outside = rag.tmp_path / "outside.md"
    outside.write_text("# Вне каталога", encoding="utf-8")
    source, problem = indexer._safe_source(rag.docs.resolve(), outside)
    assert source is None
    assert problem and "вне каталога" in problem

    # Тот же файл, но путь с ".." внутри каталога документов.
    source, problem = indexer._safe_source(
        rag.docs.resolve(), rag.docs.resolve() / ".." / "outside.md")
    assert source is None and problem


def test_safe_source_accepts_nested_file(rag):
    nested = _write(rag.docs, "sub/dir/doc.md", "# Doc\n\nтекст")
    source, problem = indexer._safe_source(rag.docs.resolve(), nested)
    assert problem is None
    assert source == "sub/dir/doc.md"


def test_symlink_outside_docs_dir_is_rejected(rag):
    outside = rag.tmp_path / "secret-outside.md"
    outside.write_text("# Внешний документ\n\nтекст", encoding="utf-8")
    link = rag.docs / "linked.md"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("создание симлинков недоступно в этой среде")

    result = indexer.reindex()

    assert result["indexed"] == 0
    assert any("linked.md" in error for error in result["errors"])
    assert rag.store.points == {}
    assert _rows(rag) == {}


# ---------------------------------------------------------------------------
# лимит размера файла
# ---------------------------------------------------------------------------

def test_oversized_file_is_skipped_with_error(rag, monkeypatch):
    monkeypatch.setattr(rag.settings, "rag_max_file_bytes", 100)
    _write(rag.docs, "big.md", "# Большой\n\n" + "ж" * 500)
    _write(rag.docs, "small.md", "# ОК\n\nкоротко")

    result = indexer.reindex()

    assert result["indexed"] == 1
    assert result["ok"] is False
    assert any("NETOPS_RAG_MAX_FILE_BYTES" in error
               for error in result["errors"])
    assert {p["payload"]["source"] for p in rag.store.points.values()} == {
        "small.md"}


def test_oversized_file_is_not_read_at_all(rag, monkeypatch):
    """C2: размер проверяется ДО чтения — файл > лимита не читается вовсе."""
    monkeypatch.setattr(rag.settings, "rag_max_file_bytes", 100)
    _write(rag.docs, "huge.md", "# Заголовок\n\n" + "ж" * 500)

    def forbidden_open(self, *args, **kwargs):
        raise AssertionError("файл больше лимита не должен читаться")

    monkeypatch.setattr(Path, "open", forbidden_open)
    result = indexer.reindex()

    assert result["indexed"] == 0
    assert result["ok"] is False
    assert any("NETOPS_RAG_MAX_FILE_BYTES" in error
               for error in result["errors"])
    assert any("huge.md" in error for error in result["errors"])
    assert rag.store.points == {}
    assert _rows(rag) == {}


def test_nonpositive_max_file_bytes_uses_default_limit(rag, monkeypatch):
    """C2: MAX_FILE_BYTES<=0 не отключает защиту — применяется дефолт 1 МиБ."""
    assert indexer.DEFAULT_MAX_FILE_BYTES == 1024 * 1024
    monkeypatch.setattr(rag.settings, "rag_max_file_bytes", 0)
    # Дельту дефолта уменьшаем, чтобы не писать мегабайты в тесте.
    monkeypatch.setattr(indexer, "DEFAULT_MAX_FILE_BYTES", 100)
    _write(rag.docs, "big.md", "# Большой\n\n" + "ж" * 200)
    _write(rag.docs, "small.md", "# ОК\n\nкоротко")

    result = indexer.reindex()

    assert result["indexed"] == 1
    assert result["ok"] is False
    assert any("NETOPS_RAG_MAX_FILE_BYTES (100 байт)" in error
               for error in result["errors"])
    assert {p["payload"]["source"] for p in rag.store.points.values()} == {
        "small.md"}


def test_file_grown_after_stat_is_not_read_beyond_limit(rag, monkeypatch):
    """C2: гонка роста файла между stat() и чтением — читаем <= лимита."""
    monkeypatch.setattr(rag.settings, "rag_max_file_bytes", 100)
    path = _write(rag.docs, "grow.md", "# Заголовок\n\n" + "ж" * 300)
    real_size = indexer._file_size

    def fake_size(candidate):
        if candidate.name == path.name:
            return 5          # «до чтения файл был маленьким»
        return real_size(candidate)

    monkeypatch.setattr(indexer, "_file_size", fake_size)
    result = indexer.reindex()

    assert result["indexed"] == 0
    assert any("grow.md" in error and "лимита" in error
               for error in result["errors"])


# ---------------------------------------------------------------------------
# размерность, конфигурация, выключенный RAG
# ---------------------------------------------------------------------------

def test_dimension_mismatch_reported_without_indexing(rag, monkeypatch):
    monkeypatch.setattr(indexer, "_build_store", lambda: FakeStore(dim=16))
    _write(rag.docs, "a.md", "# A\n\nтекст")
    result = indexer.reindex()
    assert result["ok"] is False
    assert result["indexed"] == 0
    assert any("Размерность" in error for error in result["errors"])


def test_reindex_when_rag_disabled(rag, monkeypatch):
    monkeypatch.setattr(rag.settings, "rag_enabled", False)
    monkeypatch.setattr(rag.settings, "mock_mode", False)
    _write(rag.docs, "a.md", "# A\n\nтекст")

    result = indexer.reindex()

    assert result["ok"] is False
    assert "RAG выключен" in result["errors"][0]
    assert rag.store.upsert_calls == []
    assert rag.store.ensure_dims == []
    assert indexer.index_status()["enabled"] is False


def test_missing_docs_dir_reported(rag, monkeypatch):
    missing = rag.tmp_path / "absent"
    monkeypatch.setattr(rag.settings, "rag_docs_dir", str(missing))

    result = indexer.reindex()
    assert result["ok"] is False
    assert any("не найден" in error for error in result["errors"])
    status = indexer.index_status()
    assert status["error"] and "не найден" in status["error"]


def test_docs_dir_path_resolves_relative_path(rag, monkeypatch):
    monkeypatch.setattr(rag.settings, "rag_docs_dir", "runbooks")
    path = indexer.docs_dir_path()
    assert path.is_absolute()
    assert path.name == "runbooks"


def test_docs_dir_path_rejects_empty_setting(rag, monkeypatch):
    monkeypatch.setattr(rag.settings, "rag_docs_dir", "   ")
    with pytest.raises(indexer.IndexerError):
        indexer.docs_dir_path()


def test_unsupported_suffix_only_dir_reports_no_documents(rag):
    _write(rag.docs, "readme.pdf", "не индексируется")
    status = indexer.index_status()
    assert "нет файлов" in (status["error"] or "")
