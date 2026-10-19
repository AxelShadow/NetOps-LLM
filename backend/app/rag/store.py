"""Тонкий клиент Qdrant по REST (без qdrant-client и grpcio).

Зачем свой клиент: `qdrant-client` тянет grpcio (~десятки МБ в образ), а нам
нужны четыре операции — создать коллекцию, upsert точек, удалить точки
источника, поиск. REST API Qdrant стабилен и работает поверх httpx.

Контракт Qdrant (проверяется тестами через подмену httpx-транспорта):

* PUT  /collections/{name}                     {"vectors": {"size", "distance"}}
* GET  /collections/{name}                     -> result.points_count
* PUT  /collections/{name}/points?wait=true    {"points": [{id, vector, payload}]}
* POST /collections/{name}/points/delete?wait=true {"filter": {...}}
* POST /collections/{name}/points/search       {"vector", "limit", "with_payload"}

Идентификаторы точек — детерминированные UUID5 от (source, chunk_index):
повторная индексация того же документа перезаписывает точки, а не плодит
дубликаты.
"""
from __future__ import annotations

import uuid

import httpx


class QdrantError(RuntimeError):
    """Ошибка Qdrant (недоступен, HTTP-ошибка, кривой ответ)."""


class QdrantStore:
    """Клиент REST-API Qdrant с ограниченным набором операций RAG."""

    def __init__(self, url: str, collection: str, auth_key: str = "",
                 timeout: float = 10.0):
        if not url:
            raise QdrantError("Не задан адрес Qdrant (NETOPS_RAG_QDRANT_URL)")
        self.url = url.rstrip("/")
        self.collection = collection
        self.auth_key = auth_key
        self.timeout = timeout

    # --- служебное ---------------------------------------------------------
    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.auth_key:
            headers["api-key"] = self.auth_key
        return headers

    def _request(self, method: str, path: str, **kwargs) -> dict:
        raise QdrantError(
            "Клиент Qdrant ещё не реализован (Этап 21 в работе)")

    # --- операции ----------------------------------------------------------
    def health(self) -> bool:
        """Доступен ли Qdrant (для статуса в админке)."""
        raise QdrantError(
            "Клиент Qdrant ещё не реализован (Этап 21 в работе)")

    def collection_info(self) -> dict:
        """{'exists': bool, 'points_count': int} — для статуса индекса."""
        raise QdrantError(
            "Клиент Qdrant ещё не реализован (Этап 21 в работе)")

    def ensure_collection(self, dim: int) -> None:
        raise QdrantError(
            "Клиент Qdrant ещё не реализован (Этап 21 в работе)")

    def upsert(self, points: list[dict]) -> None:
        raise QdrantError(
            "Клиент Qdrant ещё не реализован (Этап 21 в работе)")

    def delete_by_source(self, source: str) -> None:
        raise QdrantError(
            "Клиент Qdrant ещё не реализован (Этап 21 в работе)")

    def search(self, vector: list[float], limit: int = 5) -> list[dict]:
        raise QdrantError(
            "Клиент Qdrant ещё не реализован (Этап 21 в работе)")
