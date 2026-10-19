"""Публичный фасад RAG для инструмента агента и админки.

Инструмент агента `search_internal_docs` (регистрируется в `agent/tools.py`)
вызывает `search_documents()`; страница настроек — `get_status()` и
`reindex_documents()`.

Возвращаемое значение `search_documents()` — как у остальных инструментов:
`(json-строка, status)`, где status `ok` / `error`. Ошибки не бросаются наружу:
модель должна получить понятное сообщение, а не 500.

Результат сам ужимается до `MAX_RESULT` (тот же лимит, что в
`agent/tools.py`): сначала сокращаются тексты фрагментов, затем при
необходимости отбрасываются хвостовые хиты. Текст ошибки не содержит
адресов Qdrant/эмбеддингов и ключей доступа.
"""
from __future__ import annotations

import json
import logging

from ..config import get_settings
from . import indexer
from .embeddings import EmbeddingError, build_embedding_provider
from .store import QdrantError

log = logging.getLogger(__name__)

# Должно совпадать с MAX_RESULT в app/agent/tools.py (execute_tool обрежет
# ещё раз, но при обрезке по сырому JSON можно потерять структуру ответа).
MAX_RESULT = 20000

# Минимум текста на фрагмент при ужимании результата.
_MIN_CHUNK_CHARS = 200


class RagError(RuntimeError):
    """Ошибка RAG-поиска/индексации (сообщение без секретов)."""


def _runtime_enabled() -> bool:
    """RAG доступен (учитывает мок-режим — см. rag/indexer.py)."""
    return indexer._runtime_enabled()


def _dumps(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _error(message: str, query: str | None = None) -> tuple[str, str]:
    payload: dict = {"status": "error", "message": message}
    if query is not None:
        payload["query"] = query
        payload["hits"] = []
    return _dumps(payload), "error"


def _format_hits(query: str, hits: list[dict]) -> str:
    """Хиты Qdrant -> JSON ответа инструмента, ужатый до MAX_RESULT."""
    items: list[dict] = []
    for hit in hits:
        payload = hit.get("payload") or {}
        text = str(payload.get("text") or "")
        if not text.strip():
            continue
        try:
            score = round(float(hit.get("score") or 0.0), 4)
        except (TypeError, ValueError):
            score = 0.0
        items.append({
            "source": str(payload.get("source") or ""),
            "heading": str(payload.get("heading") or ""),
            "chunk": payload.get("chunk"),
            "score": score,
            "text": text,
        })

    result = {"query": query, "hits": items}
    serialized = _dumps(result)
    if len(serialized) <= MAX_RESULT:
        return serialized

    # 1) Пропорционально сокращаем тексты фрагментов (структура JSON цела).
    budget = MAX_RESULT - 400          # запас на служебные поля/кавычки
    per_text = max(_MIN_CHUNK_CHARS,
                   budget // max(1, len(items)) - 200)
    for item in items:
        if len(item["text"]) > per_text:
            item["text"] = item["text"][:per_text].rstrip()
    serialized = _dumps(result)
    # 2) Всё ещё длинно (очень много/очень длинные фрагменты) — режем хвост.
    while len(serialized) > MAX_RESULT and items:
        items.pop()
        serialized = _dumps(result)
    return serialized


def search_documents(query: str, top_k: int | None = None) -> tuple[str, str]:
    """Поиск по runbook'ам: JSON `{query, hits: [{source, heading, score, text}]}`."""
    settings = get_settings()
    query = (query or "").strip()
    if not query:
        return _error("Пустой запрос: укажите, что искать в runbook'ах", "")

    if not _runtime_enabled():
        return _error(
            "RAG по runbook'ам выключен (NETOPS_RAG_ENABLED=false) — "
            "поиск по внутренней документации недоступен", query)

    if top_k is None:
        top_k = settings.rag_top_k
    try:
        top_k = int(top_k)
    except (TypeError, ValueError):
        top_k = settings.rag_top_k
    top_k = max(1, min(top_k, 10))

    try:
        provider = build_embedding_provider()
        vectors = provider.embed_texts([query])
        if not vectors:
            raise EmbeddingError("Провайдер эмбеддингов вернул пустой результат")
        store = indexer._build_store()
        hits = store.search(vectors[0], limit=top_k)
    except (EmbeddingError, QdrantError, indexer.IndexerError) as exc:
        log.warning("RAG-поиск недоступен: %s", exc)
        return _error(f"Поиск по документам недоступен: {exc}", query)
    except Exception as exc:  # noqa: BLE001 - инструмент не должен бросать
        log.exception("Внутренняя ошибка RAG-поиска")
        return _error(
            "Внутренняя ошибка поиска по документам "
            f"({type(exc).__name__})", query)

    return _format_hits(query, hits), "ok"


def get_status() -> dict:
    """Статус индекса для админки (см. rag/indexer.py::index_status)."""
    return indexer.index_status()


def reindex_documents(force: bool = False) -> dict:
    """Переиндексация runbook'ов (кнопка в админке / CLI); не бросает."""
    try:
        return indexer.reindex(force=force)
    except Exception as exc:  # noqa: BLE001 - админка не должна получать 500
        log.exception("Ошибка переиндексации RAG")
        return {
            "ok": False,
            "indexed": 0,
            "skipped": 0,
            "deleted": 0,
            "chunks": 0,
            "documents": 0,
            "duration_ms": 0,
            "errors": [f"Внутренняя ошибка индексатора ({type(exc).__name__})"],
            "message": "Переиндексация не выполнена",
        }
