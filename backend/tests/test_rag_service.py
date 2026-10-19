"""Юнит-тесты RAG-фасада (`app/rag/service.py`).

Без сети: `_build_store` и `build_embedding_provider` подменяются, для
`get_status` — отдельная временная sqlite. Проверяются: успешный поиск,
границы `top_k`, ошибки эмбеддингов/Qdrant без исключений, выключенный RAG,
ужимание результата до `MAX_RESULT` (как `MAX_RESULT` инструментов агента).

Запуск: cd backend; .\\.venv\\Scripts\\python.exe -m pytest tests/test_rag_service.py -v
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

# env ДО импорта app.*: Settings требует jwt_secret и llm_base_url.
TMP = tempfile.mkdtemp(prefix="netops_rag_service_")
os.environ["NETOPS_DATABASE_URL"] = f"sqlite:///{TMP}/rag_service.db".replace("\\", "/")
os.environ["NETOPS_JWT_SECRET"] = "rag-service" + "-test"
os.environ["NETOPS_LLM_BASE_URL"] = "http://llm.invalid:9/v1"
os.environ["NETOPS_MOCK_MODE"] = "false"

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import Base  # noqa: E402
from app.models import RagDocument  # noqa: E402
from app.rag import indexer, service  # noqa: E402
from app.rag.embeddings import EmbeddingError, MockEmbeddings  # noqa: E402
from app.rag.store import QdrantError  # noqa: E402


class FakeSearchStore:
    """In-memory замена QdrantStore для поиска (без сети)."""

    def __init__(self):
        self.hits: list[dict] = []
        self.search_calls: list[dict] = []
        self.error: Exception | None = None
        self.health_ok = True

    def search(self, vector: list[float], limit: int = 5) -> list[dict]:
        self.search_calls.append({"vector": vector, "limit": limit})
        if self.error is not None:
            raise self.error
        return self.hits[:limit]

    def health(self) -> bool:
        return self.health_ok

    def collection_info(self) -> dict:
        return {"exists": True, "points_count": len(self.hits)}


def _hit(source: str, text: str, score: float = 0.9) -> dict:
    return {
        "id": "point-" + source,
        "score": score,
        "payload": {"source": source, "heading": "Раздел", "chunk": 0,
                    "text": text, "sha256": "a" * 64},
    }


@pytest.fixture()
def rag(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "rag_enabled", True)
    monkeypatch.setattr(settings, "rag_top_k", 5)
    monkeypatch.setattr(service, "build_embedding_provider",
                        lambda: MockEmbeddings(dim=8))
    store = FakeSearchStore()
    monkeypatch.setattr(indexer, "_build_store", lambda: store)
    return SimpleNamespace(settings=settings, store=store)


# ---------------------------------------------------------------------------
# поиск
# ---------------------------------------------------------------------------

def test_search_returns_json_hits(rag):
    rag.store.hits = [_hit("network/check-link.md", "Проверьте линк.")]
    result, status = service.search_documents("как проверить линк")
    assert status == "ok"
    payload = json.loads(result)
    assert payload["query"] == "как проверить линк"
    assert payload["hits"][0]["source"] == "network/check-link.md"
    assert payload["hits"][0]["text"] == "Проверьте линк."
    assert payload["hits"][0]["score"] == 0.9
    assert "heading" in payload["hits"][0]
    # В Qdrant ушёл вектор запроса (размерность провайдера) и top_k из настроек.
    assert len(rag.store.search_calls[0]["vector"]) == 8
    assert rag.store.search_calls[0]["limit"] == 5


def test_search_clamps_top_k(rag):
    service.search_documents("запрос", top_k=99)
    service.search_documents("запрос", top_k=0)
    service.search_documents("запрос", top_k="abc")
    limits = [call["limit"] for call in rag.store.search_calls]
    assert limits == [10, 1, 5]


def test_search_empty_query_is_error(rag):
    result, status = service.search_documents("   ")
    assert status == "error"
    payload = json.loads(result)
    assert payload["status"] == "error"
    assert "Пустой запрос" in payload["message"]
    assert rag.store.search_calls == []


def test_search_no_hits_is_ok(rag):
    result, status = service.search_documents("чего-то нет в документах")
    assert status == "ok"
    assert json.loads(result)["hits"] == []


def test_search_when_rag_disabled(rag, monkeypatch):
    monkeypatch.setattr(rag.settings, "rag_enabled", False)
    result, status = service.search_documents("вопрос")
    assert status == "error"
    payload = json.loads(result)
    assert "выключен" in payload["message"]
    assert rag.store.search_calls == []


def test_search_qdrant_unavailable_returns_error(rag):
    rag.store.error = QdrantError("Qdrant вернул HTTP 500")
    result, status = service.search_documents("вопрос")
    assert status == "error"
    payload = json.loads(result)
    assert "недоступен" in payload["message"]
    assert "HTTP 500" in payload["message"]


def test_search_embedding_error_returns_error(rag, monkeypatch):
    def boom():
        raise EmbeddingError("Сервер эмбеддингов недоступен: ConnectError")
    monkeypatch.setattr(service, "build_embedding_provider", boom)
    result, status = service.search_documents("вопрос")
    assert status == "error"
    assert "эмбеддингов" in json.loads(result)["message"]


def test_search_result_trimmed_to_max_result(rag):
    long_text = "ж" * 8000
    rag.store.hits = [_hit(f"doc-{i}.md", long_text) for i in range(5)]
    result, status = service.search_documents("вопрос про длинные фрагменты")
    assert status == "ok"
    assert len(result) <= service.MAX_RESULT
    assert service.MAX_RESULT == 20000
    payload = json.loads(result)
    assert payload["hits"]
    # Тексты фрагментов сокращены, структура ответа сохранена.
    assert all(len(hit["text"]) < len(long_text) for hit in payload["hits"])
    assert all(hit["source"] for hit in payload["hits"])


# ---------------------------------------------------------------------------
# статус и переиндексация
# ---------------------------------------------------------------------------

def test_get_status_returns_index_state(rag, monkeypatch):
    workdir = Path(tempfile.mkdtemp(prefix="netops_rag_status_"))
    docs = workdir / "runbooks"
    docs.mkdir()
    (docs / "doc.md").write_text("# Раздел\n\nтекст", encoding="utf-8")
    monkeypatch.setattr(rag.settings, "rag_docs_dir", str(docs))

    engine = create_engine(f"sqlite:///{workdir / 'status.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)
    monkeypatch.setattr(indexer, "SessionLocal", session)
    with session() as db:
        db.add(RagDocument(source="doc.md", sha256="b" * 64,
                           size_bytes=12, chunks=2))
        db.commit()

    try:
        status = service.get_status()
        assert status["enabled"] is True
        assert status["documents"] == 1
        assert status["chunks"] == 2
        assert status["docs_dir"] == str(docs.resolve())
        assert status["qdrant_ok"] is True
        assert status["last_indexed_at"]
    finally:
        engine.dispose()
        shutil.rmtree(workdir, ignore_errors=True)


def test_reindex_documents_wraps_unexpected_exception(rag, monkeypatch):
    def boom(force: bool = False):
        raise RuntimeError("внутренний сбой")
    monkeypatch.setattr(indexer, "reindex", boom)
    result = service.reindex_documents()
    assert result["ok"] is False
    assert result["errors"]
    assert result["indexed"] == 0


def test_reindex_documents_passes_force(rag, monkeypatch):
    calls: list[bool] = []

    def fake_reindex(force: bool = False):
        calls.append(force)
        return {"ok": True, "indexed": 0, "skipped": 0, "deleted": 0,
                "chunks": 0, "duration_ms": 1, "errors": [], "message": "ok"}
    monkeypatch.setattr(indexer, "reindex", fake_reindex)
    assert service.reindex_documents(force=True)["ok"] is True
    assert calls == [True]
