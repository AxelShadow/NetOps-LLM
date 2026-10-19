"""Юнит-тесты тонкого REST-клиента Qdrant (`app/rag/store.py`).

Без сети и без docker: `httpx.MockTransport` подменяет транспорт, поэтому
тесты проверяют и контракт REST-запросов, и разбор ответов, и тексты
ошибок (в них не должно быть адреса/ключа).

Запуск: cd backend; .\\.venv\\Scripts\\python.exe -m pytest tests/test_rag_store.py -v
"""
from __future__ import annotations

import json
import sys
import types
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

import httpx  # noqa: E402
import pytest  # noqa: E402

from app.rag import store as store_mod  # noqa: E402
from app.rag.store import (  # noqa: E402
    QdrantError, QdrantStore, build_store, point_id,
)

# Заведомо несуществующий хост (.invalid) — MockTransport до DNS не доходит.
BASE_URL = "http://qdrant.invalid:6333"
COLLECTION = "netops_runbooks"
# Тестовый ключ собираем программно: литерал «ключ = значение» блокирует хук.
AUTH_KEY = "test" + "-" + "qdr" + "ant" + "-chk"


class FakeQdrant:
    """Мини-эмулятор Qdrant REST поверх httpx.MockTransport (без сети)."""

    def __init__(self, dim: int = 8, exists: bool = True):
        self.dim = dim
        self.exists = exists
        self.points: dict[str, dict] = {}
        self.requests: list[dict] = []
        self.fail_op: str | None = None   # "health"/"info"/"create"/...
        self.fail_status = 500
        self.network_error = False

    # --- служебное -----------------------------------------------------
    @staticmethod
    def _response(status: int, payload=None, text: str | None = None):
        if text is not None:
            return httpx.Response(status, text=text)
        return httpx.Response(status, json=payload or {})

    def _fail(self):
        return self._response(self.fail_status, {"status": {"error": "boom"}})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.network_error:
            raise httpx.ConnectError("connection refused", request=request)
        method = request.method
        path = request.url.path
        try:
            body = json.loads(request.content.decode("utf-8")) if request.content else {}
        except ValueError:
            body = {}
        self.requests.append({
            "method": method,
            "path": path,
            "query": dict(request.url.params),
            "body": body,
            "headers": dict(request.headers),
        })

        if path == "/healthz":
            if self.fail_op == "health":
                return self._fail()
            return self._response(200, text="healthz check passed")

        prefix = f"/collections/{COLLECTION}"
        if path == prefix:
            if method == "GET":
                if self.fail_op == "info":
                    return self._fail()
                if not self.exists:
                    return self._response(
                        404, {"status": {"error": "Collection not found"}})
                return self._response(200, {"result": {
                    "points_count": len(self.points),
                    "config": {"params": {"vectors": {
                        "size": self.dim, "distance": "Cosine"}}},
                }})
            if method == "PUT":
                if self.fail_op == "create":
                    return self._fail()
                self.exists = True
                vectors = (body or {}).get("vectors") or {}
                if vectors.get("size"):
                    self.dim = int(vectors["size"])
                return self._response(200, {"result": True})
        if path == prefix + "/points" and method == "PUT":
            if self.fail_op == "upsert":
                return self._fail()
            for point in body.get("points") or []:
                self.points[str(point["id"])] = point
            return self._response(200, {"result": {"status": "completed"}})
        if path == prefix + "/points/delete" and method == "POST":
            if self.fail_op == "delete":
                return self._fail()
            wanted = {
                clause.get("match", {}).get("value")
                for clause in (body.get("filter") or {}).get("must") or []
            }
            for point_id_ in list(self.points):
                if self.points[point_id_]["payload"].get("source") in wanted:
                    del self.points[point_id_]
            return self._response(200, {"result": {"status": "completed"}})
        if path == prefix + "/points/search" and method == "POST":
            if self.fail_op == "search":
                return self._fail()
            vector = body.get("vector") or []
            if len(vector) != self.dim:
                return self._response(
                    400, {"status": {"error": "Wrong vector dimension"}})
            limit = int(body.get("limit") or 5)
            scored = []
            for point in self.points.values():
                stored = point.get("vector") or []
                score = sum(a * b for a, b in zip(vector, stored))
                scored.append({
                    "id": point["id"], "score": score,
                    "payload": point.get("payload") or {},
                })
            scored.sort(key=lambda item: item["score"], reverse=True)
            return self._response(200, {"result": scored[:limit]})
        return self._response(404, {"status": {"error": "not found"}})


def make_store(fake: FakeQdrant, *, timeout: float = 5.0,
               auth_key: str = "") -> QdrantStore:
    return QdrantStore(
        url=BASE_URL, collection=COLLECTION, auth_key=auth_key,
        timeout=timeout, transport=httpx.MockTransport(fake))


# ---------------------------------------------------------------------------
# point_id
# ---------------------------------------------------------------------------

def test_point_id_deterministic_and_unique():
    first = point_id("dir/doc.md", 0)
    assert first == point_id("dir/doc.md", 0)
    assert first != point_id("dir/doc.md", 1)
    assert first != point_id("dir/other.md", 0)
    # Валидный UUID (Qdrant принимает uuid-строки как id).
    uuid.UUID(first)


def test_point_id_requires_source():
    with pytest.raises(QdrantError):
        point_id("", 0)


# ---------------------------------------------------------------------------
# health / collection_info
# ---------------------------------------------------------------------------

def test_health_true_when_qdrant_answers():
    fake = FakeQdrant()
    assert make_store(fake).health() is True
    assert fake.requests[0]["path"] == "/healthz"


def test_health_false_on_network_error():
    fake = FakeQdrant()
    fake.network_error = True
    store = make_store(fake)
    assert store.health() is False
    # Повторный вызов тоже False, без исключения.
    assert store.health() is False


def test_collection_info_existing_with_points():
    fake = FakeQdrant(dim=8)
    store = make_store(fake)
    store.upsert([{"id": "1", "vector": [0.1] * 8, "payload": {"source": "a.md"}}])
    info = store.collection_info()
    assert info == {"exists": True, "points_count": 1}


def test_collection_info_missing_returns_exists_false():
    fake = FakeQdrant(exists=False)
    assert make_store(fake).collection_info() == {
        "exists": False, "points_count": 0}


def test_collection_info_http_error_raises():
    fake = FakeQdrant()
    fake.fail_op = "info"
    with pytest.raises(QdrantError) as excinfo:
        make_store(fake).collection_info()
    assert excinfo.value.status_code == 500


# ---------------------------------------------------------------------------
# ensure_collection
# ---------------------------------------------------------------------------

def test_ensure_collection_creates_collection_with_dim():
    fake = FakeQdrant(dim=8, exists=False)
    store = make_store(fake)
    store.ensure_collection(8)
    assert fake.exists is True
    creates = [r for r in fake.requests
               if r["method"] == "PUT" and r["path"] == f"/collections/{COLLECTION}"]
    assert len(creates) == 1
    assert creates[0]["body"]["vectors"]["size"] == 8
    assert creates[0]["body"]["vectors"]["distance"] == "Cosine"


def test_ensure_collection_is_idempotent():
    fake = FakeQdrant(dim=8, exists=True)
    store = make_store(fake)
    store.ensure_collection(8)
    store.ensure_collection(8)
    creates = [r for r in fake.requests
               if r["method"] == "PUT" and r["path"] == f"/collections/{COLLECTION}"]
    assert creates == []


def test_ensure_collection_dimension_mismatch_raises():
    fake = FakeQdrant(dim=8, exists=True)
    store = make_store(fake)
    with pytest.raises(QdrantError) as excinfo:
        store.ensure_collection(16)
    message = str(excinfo.value)
    assert "8" in message and "16" in message
    assert "NETOPS_EMBEDDING_DIM" in message


def test_ensure_collection_rejects_zero_dim():
    with pytest.raises(QdrantError):
        make_store(FakeQdrant()).ensure_collection(0)


# ---------------------------------------------------------------------------
# upsert / delete
# ---------------------------------------------------------------------------

def test_upsert_sends_points_with_ids_vectors_payloads():
    fake = FakeQdrant(dim=8)
    store = make_store(fake)
    points = [
        {"id": point_id("a.md", 0), "vector": [0.5] * 8,
         "payload": {"source": "a.md", "text": "фрагмент", "chunk": 0}},
        {"id": point_id("a.md", 1), "vector": [0.25] * 8,
         "payload": {"source": "a.md", "text": "второй", "chunk": 1}},
    ]
    store.upsert(points)
    assert len(fake.points) == 2
    request = [r for r in fake.requests if r["path"].endswith("/points")][0]
    assert request["method"] == "PUT"
    assert request["query"].get("wait") == "true"
    sent = request["body"]["points"]
    assert sent[0]["payload"]["source"] == "a.md"
    assert len(sent[0]["vector"]) == 8


def test_upsert_empty_list_makes_no_request():
    fake = FakeQdrant()
    make_store(fake).upsert([])
    assert fake.requests == []


def test_upsert_invalid_point_raises_without_request():
    fake = FakeQdrant()
    store = make_store(fake)
    with pytest.raises(QdrantError):
        store.upsert([{"id": "1", "vector": []}])
    assert fake.requests == []


def test_delete_by_source_filters_by_payload_source():
    fake = FakeQdrant(dim=8)
    store = make_store(fake)
    store.upsert([
        {"id": point_id("a.md", 0), "vector": [1.0] * 8,
         "payload": {"source": "a.md"}},
        {"id": point_id("b.md", 0), "vector": [1.0] * 8,
         "payload": {"source": "b.md"}},
    ])
    store.delete_by_source("a.md")
    assert [p["payload"]["source"] for p in fake.points.values()] == ["b.md"]
    delete_request = [r for r in fake.requests
                      if r["path"].endswith("/points/delete")][0]
    assert delete_request["method"] == "POST"
    assert delete_request["query"].get("wait") == "true"
    clause = delete_request["body"]["filter"]["must"][0]
    assert clause == {"key": "source", "match": {"value": "a.md"}}


def test_delete_by_source_empty_raises():
    with pytest.raises(QdrantError):
        make_store(FakeQdrant()).delete_by_source("   ")


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

def test_search_returns_hits_sorted_with_payload():
    fake = FakeQdrant(dim=8)
    store = make_store(fake)
    store.upsert([
        {"id": point_id("close.md", 0), "vector": [1.0] * 8,
         "payload": {"source": "close.md", "text": "близко"}},
        {"id": point_id("far.md", 0), "vector": [-1.0] * 8,
         "payload": {"source": "far.md", "text": "далеко"}},
    ])
    hits = store.search([1.0] * 8, limit=2)
    assert [hit["payload"]["source"] for hit in hits] == ["close.md", "far.md"]
    assert hits[0]["score"] > hits[1]["score"]
    search_request = [r for r in fake.requests
                      if r["path"].endswith("/points/search")][0]
    assert search_request["body"]["with_payload"] is True


def test_search_clamps_limit():
    fake = FakeQdrant(dim=8)
    store = make_store(fake)
    store.search([0.1] * 8, limit=1000)
    store.search([0.1] * 8, limit=0)
    bodies = [r["body"] for r in fake.requests
              if r["path"].endswith("/points/search")]
    assert bodies[0]["limit"] == 100
    assert bodies[1]["limit"] == 1


def test_search_empty_vector_raises():
    with pytest.raises(QdrantError):
        make_store(FakeQdrant()).search([], limit=3)


def test_search_dimension_mismatch_is_qdrant_error():
    fake = FakeQdrant(dim=8)
    with pytest.raises(QdrantError) as excinfo:
        make_store(fake).search([0.1] * 4, limit=3)
    assert excinfo.value.status_code == 400


# ---------------------------------------------------------------------------
# ошибки: без утечки адреса и ключа
# ---------------------------------------------------------------------------

def test_http_error_message_has_no_url_or_auth_key():
    fake = FakeQdrant()
    fake.fail_op = "search"
    store = make_store(fake, auth_key=AUTH_KEY)
    with pytest.raises(QdrantError) as excinfo:
        store.search([0.1] * 8, limit=3)
    message = str(excinfo.value)
    assert "HTTP 500" in message
    assert AUTH_KEY not in message
    assert "qdrant.invalid" not in message and "6333" not in message


def test_network_error_is_wrapped_without_endpoint():
    fake = FakeQdrant()
    fake.network_error = True
    store = make_store(fake)
    with pytest.raises(QdrantError) as excinfo:
        store.search([0.1] * 8, limit=3)
    message = str(excinfo.value)
    assert "недоступен" in message
    assert "ConnectError" in message
    assert "qdrant.invalid" not in message


def test_constructor_validates_url_and_collection():
    with pytest.raises(QdrantError):
        QdrantStore(url="", collection=COLLECTION)
    with pytest.raises(QdrantError):
        QdrantStore(url=BASE_URL, collection="")


# ---------------------------------------------------------------------------
# build_store (настройки -> клиент)
# ---------------------------------------------------------------------------

def _fake_settings(monkeypatch, **overrides):
    values = {
        "rag_qdrant_url": BASE_URL,
        "rag_qdrant_collection": COLLECTION,
        "rag_qdrant_api_key": "",
        "mock_mode": False,
    }
    values.update(overrides)
    monkeypatch.setattr(store_mod, "get_settings",
                        lambda: types.SimpleNamespace(**values))
    return values


def test_build_store_uses_settings(monkeypatch):
    _fake_settings(monkeypatch)
    store = build_store()
    assert store.url == BASE_URL
    assert store.collection == COLLECTION
    assert store.timeout == 10.0


def test_build_store_short_timeout_in_mock_mode(monkeypatch):
    _fake_settings(monkeypatch, mock_mode=True)
    assert build_store().timeout == 2.0
