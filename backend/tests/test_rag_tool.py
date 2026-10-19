"""Тесты инструмента агента `search_internal_docs` (RAG).

Проверяется контракт §4.2 дизайн-документа: регистрация в `TOOLS_SCHEMA`,
RBAC (все роли), статус `error` вместо исключений, запись в аудит,
мок-режим `MOCK_TOOLS`, обрезка результата до `MAX_RESULT`.

Без сети: `execute_tool` — реальный, а RAG-фасад подменяется
monkeypatch'ем; БД — временная sqlite.

Запуск: cd backend; .\\.venv\\Scripts\\python.exe -m pytest tests/test_rag_tool.py -v
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
TMP = tempfile.mkdtemp(prefix="netops_rag_tool_")
os.environ["NETOPS_DATABASE_URL"] = f"sqlite:///{TMP}/rag_tool.db".replace("\\", "/")
os.environ["NETOPS_JWT_SECRET"] = "rag-tool" + "-test"
os.environ["NETOPS_LLM_BASE_URL"] = "http://llm.invalid:9/v1"
os.environ["NETOPS_MOCK_MODE"] = "false"

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.agent import mock as agent_mock  # noqa: E402
from app.agent import tools  # noqa: E402
from app.agent.tools import (  # noqa: E402
    MAX_RESULT, TOOLS_SCHEMA, _registry, execute_tool,
)
from app.config import get_settings  # noqa: E402
from app.db import Base  # noqa: E402
from app.models import AuditLog, Role, User  # noqa: E402
from app.rag import service as rag_service  # noqa: E402

TOOL_NAME = "search_internal_docs"
ROLES = ("viewer", "engineer", "admin")


def _hits_json(query: str) -> str:
    return json.dumps({
        "query": query,
        "hits": [{
            "source": "runbooks/check-link.md",
            "heading": "Проверка линка",
            "chunk": 0,
            "score": 0.93,
            "text": "Проверьте статус интерфейса и линк.",
        }],
    }, ensure_ascii=False)


@pytest.fixture()
def tool_db(monkeypatch):
    workdir = Path(tempfile.mkdtemp(prefix="netops_rag_tool_work_"))
    engine = create_engine(f"sqlite:///{workdir / 'tool.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)
    monkeypatch.setattr(tools, "SessionLocal", session)
    # Другие тестовые модули могли включить мок-режим глобально (env
    # NETOPS_MOCK_MODE): для тестов реальной ветки выключаем его явно.
    settings = get_settings()
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "rag_enabled", True)
    with session() as db:
        user = User(username="rag-tool@mock.local", display_name="RAG tool",
                    role=Role.viewer, is_active=True, granted_by="test")
        db.add(user)
        db.commit()
        db.refresh(user)
        uid = user.id
    try:
        yield SimpleNamespace(uid=uid, session=session)
    finally:
        engine.dispose()
        shutil.rmtree(workdir, ignore_errors=True)


def _audit_rows(tool_db, status: str | None = None):
    with tool_db.session() as db:
        query = db.query(AuditLog).filter_by(tool=TOOL_NAME)
        if status is not None:
            query = query.filter_by(status=status)
        return query.order_by(AuditLog.id).all()


# ---------------------------------------------------------------------------
# регистрация и схема
# ---------------------------------------------------------------------------

def test_tool_registered_in_tools_schema():
    names = {item["function"]["name"] for item in TOOLS_SCHEMA}
    assert TOOL_NAME in names
    # Инструмент добавлен к 24 существующим — всего 25.
    assert len(TOOLS_SCHEMA) == 25


def test_tool_schema_and_roles_match_contract():
    func = _registry[TOOL_NAME]
    assert set(func.tool_roles) == set(ROLES)
    parameters = func.tool_parameters
    assert parameters["type"] == "object"
    assert parameters["required"] == ["query"]
    assert parameters["properties"]["query"]["type"] == "string"
    assert parameters["properties"]["top_k"]["minimum"] == 1
    assert parameters["properties"]["top_k"]["maximum"] == 10
    assert "runbook" in func.tool_description.lower()


def test_tool_has_mock_implementation():
    assert TOOL_NAME in agent_mock.MOCK_TOOLS
    result, status = agent_mock.MOCK_TOOLS[TOOL_NAME]({"query": "линк"})
    assert status == "ok"
    payload = json.loads(result)
    assert 2 <= len(payload["hits"]) <= 3
    assert all(hit["source"] for hit in payload["hits"])


# ---------------------------------------------------------------------------
# успешный вызов, RBAC, аудит
# ---------------------------------------------------------------------------

def test_tool_ok_for_all_roles_and_audited(tool_db, monkeypatch):
    calls: list[tuple[str, int | None]] = []

    def fake_search(query: str, top_k: int | None = None):
        calls.append((query, top_k))
        return _hits_json(query), "ok"

    monkeypatch.setattr(rag_service, "search_documents", fake_search)

    for role in ROLES:
        result, status = execute_tool(
            TOOL_NAME, {"query": f"как проверить линк ({role})"},
            tool_db.uid, None, role)
        assert status == "ok", f"роль {role}: {result[:120]}"
        assert json.loads(result)["hits"]

    assert len(calls) == 3
    logs = _audit_rows(tool_db)
    assert len(logs) == 3
    assert all(log.status == "ok" for log in logs)
    assert all(log.user_id == tool_db.uid for log in logs)
    assert all(log.duration_ms is not None and log.duration_ms >= 0
               for log in logs)
    assert json.loads(logs[0].arguments)["query"].startswith("как проверить")


def test_tool_unknown_role_is_denied(tool_db, monkeypatch):
    def fake_search(query: str, top_k: int | None = None):
        raise AssertionError("RBAC должен сработать до вызова RAG")
    monkeypatch.setattr(rag_service, "search_documents", fake_search)

    result, status = execute_tool(TOOL_NAME, {"query": "вопрос гостя"},
                                  tool_db.uid, None, "guest")
    assert status == "denied"
    assert "Недостаточно прав" in result
    logs = _audit_rows(tool_db)
    assert len(logs) == 1 and logs[0].status == "denied"


# ---------------------------------------------------------------------------
# ошибки: status="error", без исключений
# ---------------------------------------------------------------------------

def test_tool_service_error_status_is_passed_through(tool_db, monkeypatch):
    error_payload = json.dumps(
        {"status": "error", "message": "Поиск недоступен"}, ensure_ascii=False)

    def fake_search(query: str, top_k: int | None = None):
        return error_payload, "error"
    monkeypatch.setattr(rag_service, "search_documents", fake_search)

    result, status = execute_tool(
        TOOL_NAME, {"query": "вопрос про сбой RAG"}, tool_db.uid, None, "admin")
    assert status == "error"
    assert json.loads(result)["status"] == "error"
    logs = _audit_rows(tool_db)
    assert len(logs) == 1 and logs[0].status == "error"


def test_tool_exception_is_caught_and_audited(tool_db, monkeypatch):
    def boom(query: str, top_k: int | None = None):
        raise RuntimeError("boom")
    monkeypatch.setattr(rag_service, "search_documents", boom)

    result, status = execute_tool(
        TOOL_NAME, {"query": "вопрос про исключение"}, tool_db.uid, None, "admin")
    assert status == "error"
    assert "Ошибка выполнения" in result
    logs = _audit_rows(tool_db)
    assert len(logs) == 1 and logs[0].status == "error"


def test_tool_when_rag_disabled_returns_error(tool_db, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "rag_enabled", False)

    result, status = execute_tool(
        TOOL_NAME, {"query": "вопрос при выключенном RAG"},
        tool_db.uid, None, "viewer")
    assert status == "error"
    payload = json.loads(result)
    assert "выключен" in payload["message"]
    logs = _audit_rows(tool_db)
    assert len(logs) == 1 and logs[0].status == "error"


# ---------------------------------------------------------------------------
# мок-режим
# ---------------------------------------------------------------------------

def test_tool_in_mock_mode_uses_mock_fragments(tool_db, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "mock_mode", True)

    def must_not_be_called(query: str, top_k: int | None = None):
        raise AssertionError("в мок-режиме RAG-фасад не вызывается")
    monkeypatch.setattr(rag_service, "search_documents", must_not_be_called)

    result, status = execute_tool(
        TOOL_NAME, {"query": "как оформить заявку"}, tool_db.uid, None, "admin")
    assert status == "ok"
    payload = json.loads(result)
    assert 2 <= len(payload["hits"]) <= 3
    assert "Мок-режим" in result
    logs = _audit_rows(tool_db)
    assert len(logs) == 1 and logs[0].status == "ok"


# ---------------------------------------------------------------------------
# обрезка результата
# ---------------------------------------------------------------------------

def test_tool_result_trimmed_to_max_result(tool_db, monkeypatch):
    def huge_search(query: str, top_k: int | None = None):
        return "я" * (MAX_RESULT + 5000), "ok"
    monkeypatch.setattr(rag_service, "search_documents", huge_search)

    result, status = execute_tool(
        TOOL_NAME, {"query": "вопрос про огромный результат"},
        tool_db.uid, None, "admin")
    assert status == "ok"
    assert len(result) == MAX_RESULT
    logs = _audit_rows(tool_db)
    assert len(logs) == 1 and len(logs[0].result or "") == 4000
