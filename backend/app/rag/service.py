"""Публичный фасад RAG для инструмента агента и админки.

Инструмент агента `search_internal_docs` (регистрируется в `agent/tools.py`)
вызывает `search_documents()`; страница настроек — `get_status()` и
`reindex_documents()`.

Возвращаемое значение `search_documents()` — как у остальных инструментов:
`(json-строка, status)`, где status `ok` / `error`. Ошибки не бросаются наружу:
модель должна получить понятное сообщение, а не 500.
"""
from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)


class RagError(RuntimeError):
    """Ошибка RAG-поиска/индексации (сообщение без секретов)."""


def search_documents(query: str, top_k: int | None = None) -> tuple[str, str]:
    """Поиск по runbook'ам: JSON `{query, hits: [{source, heading, score, text}]}`."""
    return json.dumps({
        "status": "error",
        "message": "RAG ещё не реализован (Этап 21 в работе)",
    }, ensure_ascii=False), "error"


def get_status() -> dict:
    """Статус индекса для админки (см. rag/indexer.py::index_status)."""
    from .indexer import index_status
    return index_status()


def reindex_documents(force: bool = False) -> dict:
    """Переиндексация runbook'ов (кнопка в админке / CLI)."""
    from .indexer import reindex
    return reindex(force=force)
