"""Независимые сквозные QA-тесты RAG по runbook'ам (Этап 21).

Проверяется не поведение отдельных функций (это делают авторские
`tests/test_rag_*.py`), а сквозной путь контракта
`docs/features/rag-and-scheduler-design.md` §4 и §7:

    файлы на диске -> reindex (sha256, чанки, Qdrant, rag_documents)
    -> search_documents (JSON для инструмента) -> execute_tool
    (RBAC + аудит) -> деградация (RAG выключен, Qdrant недоступен,
    эмбеддинги HTTP 500, пустой/отсутствующий каталог, большой файл,
    path traversal через junction).

Сеть не используется: Qdrant эмулируется через `httpx.MockTransport`,
эмбеддинги — детерминированный `MockEmbeddings`, БД — временная sqlite
(рабочая `backend/netops.db` не затрагивается). `tmp_path` не
используется: на этом Windows-хосте DACL pytest-of-* ломается, поэтому
`tempfile.mkdtemp()`.

Запуск: cd backend; .\\.venv\\Scripts\\python.exe -m pytest tests/test_rag_pipeline_qa.py -v
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

# env ДО импорта app.*: Settings требует jwt_secret и llm_base_url.
_TMP = Path(tempfile.mkdtemp(prefix="netops_rag_qa_"))
os.environ["NETOPS_DATABASE_URL"] = (
    f"sqlite:///{(_TMP / 'qa.db').as_posix()}")
os.environ.setdefault("NETOPS_DEV_MODE", "true")
os.environ["NETOPS_MOCK_MODE"] = "false"
os.environ["NETOPS_RAG_ENABLED"] = "true"
os.environ["NETOPS_RAG_DOCS_DIR"] = str(_TMP / "runbooks")
# Заведомо недостижимый адрес: сеть в тестах не нужна (транспорт подменён).
os.environ["NETOPS_RAG_QDRANT_URL"] = "http://qdrant.invalid:6333"
os.environ["NETOPS_EMBEDDING_DIM"] = "16"
os.environ["NETOPS_JWT_SECRET"] = "rag" + "-qa" + "-jwt"
os.environ["NETOPS_LLM_BASE_URL"] = "http://llm.invalid:9/v1"
os.environ["NETOPS_ZABBIX_URL"] = "http://zbx.invalid/api_jsonrpc.php"

import httpx  # noqa: E402
import pytest  # noqa: E402

from app.agent.tools import execute_tool  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import AuditLog, RagDocument  # noqa: E402
from app.rag import embeddings as embeddings_mod  # noqa: E402
from app.rag import indexer, service  # noqa: E402
from app.rag.embeddings import MockEmbeddings  # noqa: E402
from app.rag.store import QdrantStore  # noqa: E402

Base.metadata.create_all(engine)

COLLECTION = "netops_runbooks"   # дефолт конфигурации
EMBED_DIM = 16
TOOL_NAME = "search_internal_docs"


# ---------------------------------------------------------------------------
# эмулятор Qdrant (REST-контракт из app/rag/store.py), без сети
# ---------------------------------------------------------------------------
class FakeQdrant:
    """Мини-эмулятор Qdrant: health/collection/upsert/delete/search."""

    def __init__(self, dim: int = EMBED_DIM):
        self.dim = dim
        self.exists = False
        self.points: dict[str, dict] = {}
        self.requests: list[dict] = []
        self.network_error = False

    @staticmethod
    def _json(status: int, payload) -> httpx.Response:
        return httpx.Response(status, json=payload)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.network_error:
            raise httpx.ConnectError("connection refused", request=request)
        method = request.method
        path = request.url.path
        body: dict = {}
        if request.content:
            try:
                body = json.loads(request.content.decode("utf-8"))
            except ValueError:
                body = {}
        self.requests.append({"method": method, "path": path, "body": body})

        if path == "/healthz":
            return httpx.Response(200, text="healthz check passed")
        prefix = f"/collections/{COLLECTION}"
        if path == prefix:
            if method == "GET":
                if not self.exists:
                    return self._json(404, {"status": {"error": "not found"}})
                return self._json(200, {"result": {
                    "points_count": len(self.points),
                    "config": {"params": {"vectors": {
                        "size": self.dim, "distance": "Cosine"}}},
                }})
            if method == "PUT":
                self.exists = True
                size = ((body.get("vectors") or {}).get("size"))
                if size:
                    self.dim = int(size)
                return self._json(200, {"result": True})
        if path == prefix + "/points" and method == "PUT":
            for point in body.get("points") or []:
                self.points[str(point["id"])] = point
            return self._json(200, {"result": {"status": "completed"}})
        if path == prefix + "/points/delete" and method == "POST":
            wanted = {
                clause.get("match", {}).get("value")
                for clause in (body.get("filter") or {}).get("must") or []
            }
            for point_id in list(self.points):
                if self.points[point_id]["payload"].get("source") in wanted:
                    del self.points[point_id]
            return self._json(200, {"result": {"status": "completed"}})
        if path == prefix + "/points/search" and method == "POST":
            vector = body.get("vector") or []
            if len(vector) != self.dim:
                return self._json(400, {"status": {"error": "bad dimension"}})
            limit = int(body.get("limit") or 5)
            scored = []
            for point in self.points.values():
                stored = point.get("vector") or []
                score = sum(a * b for a, b in zip(vector, stored))
                scored.append({"id": point["id"], "score": score,
                               "payload": point.get("payload") or {}})
            scored.sort(key=lambda item: item["score"], reverse=True)
            return self._json(200, {"result": scored[:limit]})
        return self._json(404, {"status": {"error": "not found"}})


# ---------------------------------------------------------------------------
# окружение теста: временный каталог документов + подмена границ сети
# ---------------------------------------------------------------------------
def _reset_db() -> None:
    with SessionLocal() as db:
        db.query(RagDocument).delete()
        db.query(AuditLog).filter(AuditLog.tool == TOOL_NAME).delete()
        db.commit()


def _write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _sources() -> set[str]:
    with SessionLocal() as db:
        return {row.source for row in db.query(RagDocument).all()}


def _payload_sources(fake: FakeQdrant) -> set[str]:
    return {p["payload"].get("source") for p in fake.points.values()}


@pytest.fixture()
def pipeline(monkeypatch):
    settings = get_settings()
    docs = Path(tempfile.mkdtemp(prefix="netops_rag_qa_docs_"))
    fake = FakeQdrant()

    def _build_store():
        return QdrantStore(
            url="http://qdrant.invalid:6333", collection=COLLECTION,
            timeout=5.0, transport=httpx.MockTransport(fake))

    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "rag_enabled", True)
    monkeypatch.setattr(settings, "rag_docs_dir", str(docs))
    monkeypatch.setattr(settings, "rag_top_k", 5)
    monkeypatch.setattr(settings, "rag_max_file_bytes", 1024 * 1024)
    monkeypatch.setattr(indexer, "_build_store", _build_store)
    monkeypatch.setattr(indexer, "build_embedding_provider",
                        lambda: MockEmbeddings(dim=EMBED_DIM))
    monkeypatch.setattr(service, "build_embedding_provider",
                        lambda: MockEmbeddings(dim=EMBED_DIM))
    _reset_db()
    try:
        yield SimpleNamespace(docs=docs, fake=fake, settings=settings)
    finally:
        shutil.rmtree(docs, ignore_errors=True)
        _reset_db()


# ---------------------------------------------------------------------------
# 1. сквозной путь: индекс -> поиск -> JSON -> инструмент -> аудит
# ---------------------------------------------------------------------------
def test_e2e_index_search_tool_audit(pipeline):
    _write(pipeline.docs, "link-check.md",
           "# Проверка линка\n\n## Диагностика\n\n"
           "линк коммутатор ping проверка\n")
    _write(pipeline.docs, "zabbix-guide.md",
           "# Zabbix\n\n## Проблемы\n\n"
           "zabbix триггер проблемы активные\n")
    _write(pipeline.docs, "net/switch.md",
           "# Коммутатор\n\n## Сброс\n\nперезагрузка порт питание\n")
    _write(pipeline.docs, "notes.txt", "заметка линк кабель\n")

    result = service.reindex_documents()
    assert result["ok"] is True, result["errors"]
    assert result["indexed"] == 4 and result["skipped"] == 0
    assert result["deleted"] == 0 and result["errors"] == []
    assert result["chunks"] >= 4
    assert _sources() == {"link-check.md", "zabbix-guide.md",
                          "net/switch.md", "notes.txt"}
    assert len(pipeline.fake.points) == result["chunks"]

    # payload точки — относительный источник, sha256, номер чанка, без абсолютных путей
    for point in pipeline.fake.points.values():
        payload = point["payload"]
        assert payload["source"] and not payload["source"].startswith("/")
        assert ".." not in payload["source"]
        assert len(payload["sha256"]) == 64
        assert isinstance(payload["chunk"], int)
        assert payload["text"].strip()

    # поиск: JSON-формат контракта (query + hits с source/heading/score/text)
    raw, status = service.search_documents("zabbix триггер")
    assert status == "ok", raw
    payload = json.loads(raw)
    assert payload["query"] == "zabbix триггер"
    assert payload["hits"], "нет хитов при непустом индексе"
    first = payload["hits"][0]
    assert first["source"] == "zabbix-guide.md", first
    assert "Zabbix" in first["heading"]
    assert set(first) >= {"source", "heading", "chunk", "score", "text"}

    # инструмент агента: реальный execute_tool + RBAC + аудит
    tool_raw, tool_status = execute_tool(
        TOOL_NAME, {"query": "zabbix триггер"}, user_role="viewer")
    assert tool_status == "ok", tool_raw
    tool_payload = json.loads(tool_raw)
    assert tool_payload["hits"][0]["source"] == "zabbix-guide.md"
    with SessionLocal() as db:
        audits = db.query(AuditLog).filter(AuditLog.tool == TOOL_NAME).all()
    assert len(audits) == 1
    assert audits[0].status == "ok"
    assert json.loads(audits[0].arguments)["query"] == "zabbix триггер"


# ---------------------------------------------------------------------------
# 2. инкрементальность: sha256 > пропуск > изменение > удаление файла
# ---------------------------------------------------------------------------
def test_incremental_reindex_and_deleted_document(pipeline):
    _write(pipeline.docs, "a.md", "# A\n\nalpha beta gamma\n")

    first = service.reindex_documents()
    assert first["ok"] is True and first["indexed"] == 1
    point_ids = set(pipeline.fake.points)

    second = service.reindex_documents()
    assert second["ok"] is True
    assert second["indexed"] == 0 and second["skipped"] == 1
    assert set(pipeline.fake.points) == point_ids, "точки перезаписаны зря"

    # изменённый файл — переиндексируется (sha256 изменился)
    _write(pipeline.docs, "a.md", "# A\n\nalpha beta gamma delta\n")
    third = service.reindex_documents()
    assert third["indexed"] == 1 and third["skipped"] == 0

    # удалённый файл — вычищается из Qdrant и rag_documents
    (pipeline.docs / "a.md").unlink()
    fourth = service.reindex_documents()
    assert fourth["deleted"] == 1, fourth
    assert pipeline.fake.points == {}
    assert _sources() == set()
    raw, status = service.search_documents("alpha beta")
    assert status == "ok"
    assert json.loads(raw)["hits"] == []


# ---------------------------------------------------------------------------
# 3. деградация: RAG выключен -> понятная ошибка, без исключений
# ---------------------------------------------------------------------------
def test_rag_disabled_degrades_without_exception(pipeline, monkeypatch):
    monkeypatch.setattr(pipeline.settings, "mock_mode", False)
    monkeypatch.setattr(pipeline.settings, "rag_enabled", False)
    _write(pipeline.docs, "doc.md", "# Doc\n\nлинк\n")

    raw, status = service.search_documents("линк")
    assert status == "error", raw
    payload = json.loads(raw)
    assert payload["status"] == "error"
    assert "выключен" in payload["message"]
    assert payload["hits"] == []

    result = service.reindex_documents()
    assert result["ok"] is False
    assert any("RAG выключен" in err for err in result["errors"]), result

    tool_raw, tool_status = execute_tool(
        TOOL_NAME, {"query": "линк"}, user_role="admin")
    assert tool_status == "error"
    assert json.loads(tool_raw)["status"] == "error"
    with SessionLocal() as db:
        audits = db.query(AuditLog).filter(AuditLog.tool == TOOL_NAME).all()
    assert len(audits) == 1 and audits[0].status == "error"


# ---------------------------------------------------------------------------
# 4. деградация: Qdrant недоступен
# ---------------------------------------------------------------------------
def test_qdrant_unavailable_degrades_without_exception(pipeline):
    _write(pipeline.docs, "doc.md", "# Doc\n\nлинк\n")
    pipeline.fake.network_error = True

    raw, status = service.search_documents("линк")
    assert status == "error", raw
    payload = json.loads(raw)
    assert "недоступен" in payload["message"]

    state = service.get_status()
    assert state["enabled"] is True
    assert state["qdrant_ok"] is False

    result = service.reindex_documents()
    assert result["ok"] is False
    assert result["errors"]
    assert any("Qdrant" in err for err in result["errors"]), result["errors"]

    # аудит: ошибка инструмента фиксируется, исключение наружу не уходит
    tool_raw, tool_status = execute_tool(
        TOOL_NAME, {"query": "линк"}, user_role="engineer")
    assert tool_status == "error"
    assert json.loads(tool_raw)["status"] == "error"


# ---------------------------------------------------------------------------
# 5. деградация: сервер эмбеддингов ответил HTTP 500
# ---------------------------------------------------------------------------
def test_embeddings_http_500_degrades(pipeline, monkeypatch):
    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, url, headers=None, json=None):
            return httpx.Response(
                500, json={"error": "internal"},
                request=httpx.Request("POST", url))

    # Подменяем httpx ТОЛЬКО в модуле эмбеддингов: httpx — общий модуль, и
    # правка httpx.Client сломала бы клиент Qdrant (store.py) в этом же тесте.
    fake_httpx = SimpleNamespace(
        Client=_FakeClient, HTTPStatusError=httpx.HTTPStatusError,
        HTTPError=httpx.HTTPError)
    monkeypatch.setattr(embeddings_mod, "httpx", fake_httpx)
    # модель эмбеддингов по умолчанию наследуется от LLM; в тестовом env её нет
    monkeypatch.setattr(pipeline.settings, "embedding_model",
                        "test-embedding-model")
    provider = embeddings_mod.build_embedding_provider()   # реальный клиент, без сети
    monkeypatch.setattr(service, "build_embedding_provider", lambda: provider)
    monkeypatch.setattr(indexer, "build_embedding_provider", lambda: provider)

    raw, status = service.search_documents("линк")
    assert status == "error", raw
    payload = json.loads(raw)
    assert payload["status"] == "error"
    assert "HTTP 500" in payload["message"], payload

    _write(pipeline.docs, "doc.md", "# Doc\n\nлинк коммутатор\n")
    result = service.reindex_documents()
    assert result["ok"] is False
    assert any("эмбеддинги" in err for err in result["errors"]), result["errors"]
    assert pipeline.fake.points == {}, "точки не должны появиться при сбое эмбеддингов"


# ---------------------------------------------------------------------------
# 6. пустой и отсутствующий каталог документов
# ---------------------------------------------------------------------------
def test_empty_and_missing_docs_dir(pipeline, monkeypatch):
    # каталог существует, но пуст
    state = service.get_status()
    assert state["enabled"] is True
    assert "нет файлов" in (state["error"] or ""), state

    result = service.reindex_documents()
    # фактическое поведение: индексировать нечего -> не ошибка
    assert result["ok"] is True and result["indexed"] == 0, result

    # каталог не существует
    missing = Path(tempfile.mkdtemp(prefix="netops_rag_qa_missing_")) / "no-dir"
    monkeypatch.setattr(pipeline.settings, "rag_docs_dir", str(missing))
    try:
        state = service.get_status()
        assert "не найден" in (state["error"] or ""), state

        result = service.reindex_documents()
        assert result["ok"] is False
        assert any("не найден" in err for err in result["errors"]), result
    finally:
        shutil.rmtree(missing.parent, ignore_errors=True)


# ---------------------------------------------------------------------------
# 7. лимит размера файла: большой пропускается, валидный индексируется
# ---------------------------------------------------------------------------
def test_oversized_file_skipped_valid_one_indexed(pipeline, monkeypatch):
    monkeypatch.setattr(pipeline.settings, "rag_max_file_bytes", 200)
    _write(pipeline.docs, "small.md", "# Small\n\nлинк ok\n")
    _write(pipeline.docs, "big.md", "# Big\n\n" + "x" * 2000)

    result = service.reindex_documents()
    assert result["ok"] is False
    assert result["indexed"] == 1, result
    assert any("больше лимита" in err for err in result["errors"]), result["errors"]
    assert _sources() == {"small.md"}
    assert _payload_sources(pipeline.fake) == {"small.md"}


# ---------------------------------------------------------------------------
# 8. path traversal: junction наружу не индексируется
# ---------------------------------------------------------------------------
def test_path_traversal_via_junction_rejected(pipeline):
    outside = Path(tempfile.mkdtemp(prefix="netops_rag_qa_outside_"))
    (outside / "hidden-doc.md").write_text(
        "# Outside\n\nдокумент вне каталога runbook'ов\n", encoding="utf-8")
    link = pipeline.docs / "linked-outside"
    proc = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
        capture_output=True, text=True)
    if proc.returncode != 0:
        shutil.rmtree(outside, ignore_errors=True)
        pytest.skip("нет прав на создание junction в этой среде")

    try:
        _write(pipeline.docs, "inside.md", "# Inside\n\nразрешённый документ\n")
        result = service.reindex_documents()

        assert _sources() == {"inside.md"}, _sources()
        assert _payload_sources(pipeline.fake) == {"inside.md"}
        assert result["indexed"] == 1
        assert any("вне каталога" in err for err in result["errors"]), result["errors"]
        # наружу ничего не просочилось в payload
        assert not any("hidden-doc" in str(p["payload"])
                       for p in pipeline.fake.points.values())

        # прямой контроль стража: файл вне корня отбрасывается с причиной
        outside_file = outside / "hidden-doc.md"
        source, problem = indexer._safe_source(pipeline.docs.resolve(), outside_file)
        assert source is None
        assert "вне каталога" in problem
    finally:
        try:
            os.rmdir(link)          # снимает junction, не трогая цель
        except OSError:
            shutil.rmtree(link, ignore_errors=True)
        shutil.rmtree(outside, ignore_errors=True)
