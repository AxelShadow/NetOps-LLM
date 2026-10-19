"""Тонкий клиент Qdrant по REST (без qdrant-client и grpcio).

Зачем свой клиент: `qdrant-client` тянет grpcio (~десятки МБ в образ), а нам
нужны несколько операций — создать коллекцию, upsert точек, удалить точки
источника, поиск, health. REST API Qdrant стабилен и работает поверх httpx.

Контракт Qdrant (проверяется тестами через подмену httpx-транспорта):

* GET  /healthz                                 -> 200 "healthz check passed"
* GET  /collections/{name}                     -> result.points_count,
                                                  result.config.params.vectors.size
* PUT  /collections/{name}                     {"vectors": {"size", "distance"}}
* PUT  /collections/{name}/points?wait=true    {"points": [{id, vector, payload}]}
* POST /collections/{name}/points/delete?wait=true {"filter": {...}}
* POST /collections/{name}/points/search       {"vector", "limit", "with_payload"}

Идентификаторы точек — детерминированные UUID5 от (source, chunk_index):
повторная индексация того же документа перезаписывает точки, а не плодит
дубликаты.

Безопасность: адрес Qdrant и API-ключ берутся из настроек и НИКОГДА не
попадают в тексты ошибок/логи (см. `.dsh/security/policy.md`); в сообщении
`QdrantError` — только тип ошибки, HTTP-код и имя операции.
"""
from __future__ import annotations

import logging
import uuid
from urllib.parse import quote

import httpx

from ..config import get_settings

log = logging.getLogger(__name__)

# Фиксированный namespace для детерминированных id точек: не меняется между
# перезапусками и версиями приложения, поэтому повторная индексация
# перезаписывает точку, а не создаёт дубль.
_POINT_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "netops-llm/rag-points")


class QdrantError(RuntimeError):
    """Ошибка Qdrant (недоступен, HTTP-ошибка, кривой ответ).

    `status_code` заполняется для HTTP-ошибок, чтобы вызывающий код мог
    отличить «коллекции нет» (404) от прочих сбоев.
    """

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def point_id(source: str, chunk_index: int) -> str:
    """Детерминированный id точки Qdrant: UUID5 от (source, номер чанка)."""
    if not source:
        raise QdrantError("Не задан source точки Qdrant")
    return str(uuid.uuid5(
        _POINT_NAMESPACE, f"{source}#{int(chunk_index)}"))


def _collection_path(collection: str) -> str:
    """Путь коллекции с экранированием имени (без инъекции в URL)."""
    return "/collections/" + quote(str(collection), safe="")


class QdrantStore:
    """Клиент REST-API Qdrant с ограниченным набором операций RAG."""

    def __init__(self, url: str, collection: str, auth_key: str = "",
                 timeout: float = 10.0, transport: httpx.BaseTransport | None = None):
        if not url:
            raise QdrantError("Не задан адрес Qdrant (NETOPS_RAG_QDRANT_URL)")
        if not collection:
            raise QdrantError("Не задано имя коллекции Qdrant "
                              "(NETOPS_RAG_QDRANT_COLLECTION)")
        self.url = url.rstrip("/")
        self.collection = collection
        self.auth_key = auth_key
        self.timeout = float(timeout)
        # transport нужен тестам (httpx.MockTransport) и не влияет на прод:
        # при None httpx создаёт обычный сетевой транспорт.
        self.transport = transport

    # --- служебное ---------------------------------------------------------
    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.auth_key:
            headers["api-key"] = self.auth_key
        return headers

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=self.timeout, transport=self.transport)

    def _raw(self, method: str, path: str, **kwargs) -> httpx.Response:
        """HTTP-запрос без разбора JSON (нужен health: ответ — plain text)."""
        url = f"{self.url}{path}"
        try:
            with self._client() as client:
                return client.request(
                    method, url, headers=self._headers(), **kwargs)
        except httpx.HTTPError as exc:
            # Только тип исключения: URL и ключ не печатаем.
            raise QdrantError(
                f"Qdrant недоступен ({type(exc).__name__})") from exc

    def _request(self, method: str, path: str, **kwargs) -> dict:
        """Запрос с ожиданием JSON-ответа; ошибки — QdrantError."""
        response = self._raw(method, path, **kwargs)
        if response.status_code >= 400:
            raise QdrantError(
                f"Qdrant вернул HTTP {response.status_code} "
                f"({method} {path.split('?')[0]})",
                status_code=response.status_code)
        try:
            payload = response.json()
        except ValueError as exc:
            raise QdrantError("Qdrant вернул не JSON") from exc
        if not isinstance(payload, dict):
            raise QdrantError("Qdrant вернул неожиданный формат ответа")
        return payload

    # --- операции ----------------------------------------------------------
    def health(self) -> bool:
        """Доступен ли Qdrant (для статуса в админке)."""
        try:
            response = self._raw("GET", "/healthz")
        except QdrantError:
            return False
        return response.status_code < 400

    def _collection_result(self) -> dict:
        payload = self._request("GET", _collection_path(self.collection))
        result = payload.get("result")
        if not isinstance(result, dict):
            raise QdrantError("Qdrant вернул неожиданный ответ по коллекции")
        return result

    def _collection_dim(self, result: dict | None = None) -> int | None:
        """Размерность векторов из ответа GET /collections/{name}."""
        if result is None:
            result = self._collection_result()
        config = result.get("config")
        params = config.get("params") if isinstance(config, dict) else None
        vectors = params.get("vectors") if isinstance(params, dict) else None
        if isinstance(vectors, dict):
            size = vectors.get("size")
            if isinstance(size, int):
                return size
            # Именованные векторы: {"name": {"size": N, ...}} — берём первый.
            for value in vectors.values():
                if isinstance(value, dict) and isinstance(value.get("size"), int):
                    return value["size"]
        return None

    def collection_info(self) -> dict:
        """{'exists': bool, 'points_count': int} — для статуса индекса."""
        try:
            result = self._collection_result()
        except QdrantError as exc:
            if exc.status_code == 404:
                return {"exists": False, "points_count": 0}
            raise
        points_count = result.get("points_count")
        try:
            points_count = int(points_count or 0)
        except (TypeError, ValueError):
            points_count = 0
        return {"exists": True, "points_count": points_count}

    def ensure_collection(self, dim: int) -> None:
        """Создаёт коллекцию под `dim`; проверяет размерность существующей."""
        try:
            dim = int(dim)
        except (TypeError, ValueError) as exc:
            raise QdrantError("Некорректная размерность эмбеддинга") from exc
        if dim <= 0:
            raise QdrantError("Размерность эмбеддинга должна быть больше нуля")
        try:
            result = self._collection_result()
        except QdrantError as exc:
            if exc.status_code != 404:
                raise
            self._request(
                "PUT", _collection_path(self.collection),
                json={"vectors": {"size": dim, "distance": "Cosine"}})
            log.info("Создана коллекция Qdrant %s (dim=%s)",
                     self.collection, dim)
            return

        existing = self._collection_dim(result)
        if existing is not None and existing != dim:
            raise QdrantError(
                f"Размерность коллекции Qdrant ({existing}) не совпадает с "
                f"размерностью эмбеддингов ({dim}): удалите коллекцию "
                f"'{self.collection}' или задайте NETOPS_EMBEDDING_DIM как у "
                f"модели")

    def upsert(self, points: list[dict]) -> None:
        """Записать/перезаписать точки (id — из `point_id`)."""
        if not points:
            return
        normalized: list[dict] = []
        for point in points:
            if not isinstance(point, dict):
                raise QdrantError("Точка Qdrant должна быть словарём")
            vector = point.get("vector")
            if not isinstance(vector, list) or not vector:
                raise QdrantError("У точки Qdrant пустой вектор")
            if not point.get("id"):
                raise QdrantError("У точки Qdrant нет id")
            normalized.append({
                "id": str(point["id"]),
                "vector": [float(x) for x in vector],
                "payload": point.get("payload") or {},
            })
        self._request(
            "PUT", _collection_path(self.collection) + "/points?wait=true",
            json={"points": normalized})

    def delete_by_source(self, source: str) -> None:
        """Удалить все точки документа (фильтр по payload.source)."""
        if not source or not str(source).strip():
            raise QdrantError("Не задан source для удаления точек Qdrant")
        self._request(
            "POST",
            _collection_path(self.collection) + "/points/delete?wait=true",
            json={"filter": {"must": [
                {"key": "source", "match": {"value": str(source)}}]}})

    def search(self, vector: list[float], limit: int = 5) -> list[dict]:
        """Векторный поиск: [{'id','score','payload'}] по убыванию score."""
        if not isinstance(vector, list) or not vector:
            raise QdrantError("Пустой вектор запроса к Qdrant")
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 5
        limit = max(1, min(limit, 100))
        payload = self._request(
            "POST", _collection_path(self.collection) + "/points/search",
            json={"vector": [float(x) for x in vector],
                  "limit": limit, "with_payload": True})
        result = payload.get("result")
        if not isinstance(result, list):
            raise QdrantError("Qdrant вернул неожиданный ответ поиска")
        hits: list[dict] = []
        for item in result:
            if not isinstance(item, dict):
                continue
            try:
                score = float(item.get("score") or 0.0)
            except (TypeError, ValueError):
                score = 0.0
            hits.append({
                "id": item.get("id"),
                "score": score,
                "payload": item.get("payload") or {},
            })
        return hits


def build_store(timeout: float | None = None) -> QdrantStore:
    """Собирает клиент Qdrant из настроек приложения.

    В мок-режиме таймаут короче: админка не должна «висеть» на странице
    статуса, когда Qdrant в dev-стеке не поднят.
    """
    settings = get_settings()
    if timeout is None:
        timeout = 2.0 if settings.mock_mode else 10.0
    return QdrantStore(
        url=settings.rag_qdrant_url,
        collection=settings.rag_qdrant_collection,
        auth_key=settings.rag_qdrant_api_key,
        timeout=timeout,
    )
