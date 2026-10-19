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
"""
from __future__ import annotations

from ..config import get_settings


class IndexerError(RuntimeError):
    """Ошибка индексации (каталог, чтение файла, хранилище)."""


def _runtime_enabled() -> bool:
    settings = get_settings()
    if settings.mock_mode:
        return True
    return settings.rag_enabled


def docs_dir_path() -> "object":
    """Путь каталога документов (Path.resolve) — общий для статуса и админки."""
    raise IndexerError("Индексатор RAG ещё не реализован (Этап 21 в работе)")


def index_status() -> dict:
    from ..config import get_settings
    settings = get_settings()
    return {
        "enabled": _runtime_enabled(),
        "docs_dir": str(settings.rag_docs_dir),
        "documents": 0,
        "chunks": 0,
        "last_indexed_at": None,
        "qdrant_ok": False,
        "embedding_model": settings.embedding_model or settings.llm_default_model,
        "error": "Индексатор RAG ещё не реализован (Этап 21 в работе)",
    }


def reindex(force: bool = False) -> dict:
    raise IndexerError("Индексатор RAG ещё не реализован (Этап 21 в работе)")
