"""Тест мок-режима: все 15 инструментов возвращают фейковые данные,
маркер 'mock-ошибка' вызывает status='error' в аудите (FIX-01).

Стиль mock_smoke.py / test_admin_ui_*.py: изолированная временная sqlite,
env задаётся ДО импорта приложения, рабочая netops.db не затрагивается.

Запуск: cd backend && python -m pytest tests/test_mock_mode.py -v
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

TMP = tempfile.mkdtemp(prefix="netops_mock_mode_")
os.environ["NETOPS_DATABASE_URL"] = f"sqlite:///{TMP}/mock_mode_test.db".replace("\\", "/")
os.environ["NETOPS_MOCK_MODE"] = "true"
os.environ["NETOPS_DEV_MODE"] = "true"
os.environ.setdefault("NETOPS_JWT_SECRET", "mock-mode-test-secret")
os.environ.setdefault("NETOPS_LLM_BASE_URL", "http://localhost:59997/v1")  # ничего не слушает
os.environ.setdefault("NETOPS_BOOTSTRAP_ADMIN", "admin@mock.local")

import pytest  # noqa: E402

from app.agent.tools import execute_tool, TOOLS_SCHEMA  # noqa: E402
from app.db import SessionLocal, Base, engine  # noqa: E402
from app.models import AuditLog, User, Role  # noqa: E402

# Схема БД для теста (без полного lifespan приложения)
Base.metadata.create_all(engine)

ROLE = "admin"  # роли по умолчанию разрешают все 15 инструментов

EXPECTED_MOCK_TOOLS = {
    "ping", "vmware_vms", "vmware_hosts", "vmware_snapshots",
    "vmware_datastores", "vmware_vm_disks", "vmware_host_networks",
    "vmware_vm_networks", "vmware_events", "vmware_host_sensors",
    "zabbix_problems", "zabbix_items", "zabbix_history",
    "get_device_full_health", "get_infrastructure_health",
}


@pytest.fixture()
def db_and_user():
    with SessionLocal() as db:
        user = User(username="test@mock", role=Role.engineer, is_active=True)
        db.add(user)
        db.commit()
        db.refresh(user)
        yield db, user.id
        db.query(AuditLog).delete()
        db.delete(user)
        db.commit()


def _execute(name: str, args: dict, user_id: int) -> tuple[str, str]:
    """execute_tool пишет аудит в собственной сессии."""
    return execute_tool(name, args, user_id, 1, ROLE)


def test_ping_returns_mock_data(db_and_user):
    db, user_id = db_and_user
    result, status = _execute("ping", {"host": "mock-host"}, user_id)
    assert status == "ok"
    assert "192.0.2.10" in result  # фейковый IP из mock.py
    log = db.query(AuditLog).filter_by(tool="ping").one()
    assert log.status == "ok"
    assert "192.0.2.10" in log.result


def test_error_marker_sets_error_status(db_and_user):
    db, user_id = db_and_user
    # Мок-функция бросает исключение; execute_tool глотает его и
    # помечает статус error (не "Запрещено" -> ветка error, не denied).
    result, status = _execute("ping", {"host": "mock-ошибка"}, user_id)
    assert status == "error"
    assert "Mock" in result or "Ошибка" in result
    log = (db.query(AuditLog).filter_by(tool="ping")
           .order_by(AuditLog.id.desc()).first())
    assert log.status == "error"
    # Мок-исключение не должно попадать в аудит как «Запрещено»
    assert log.status != "denied"


def test_vmware_vms_returns_mock_array(db_and_user):
    db, user_id = db_and_user
    # Роль по умолчанию у vmware_vms — viewer+, у устройства указан device
    result, status = _execute("vmware_vms", {"device": "vcenter"}, user_id)
    assert status == "ok"
    data = json.loads(result)
    assert len(data) == 4
    assert data[0]["name"] == "srv-app-01"


def test_all_15_tools_registered():
    from app.agent.mock import MOCK_TOOLS
    assert set(MOCK_TOOLS.keys()) == EXPECTED_MOCK_TOOLS
    # Все 15 моков соответствуют реально зарегистрированным инструментам
    assert EXPECTED_MOCK_TOOLS.issubset({t["function"]["name"]
                                        for t in TOOLS_SCHEMA})


def test_audit_status_ok_for_any_prompt_tool(db_and_user):
    """Критерий готовности: при мок-режиме вызов инструмента оставляет
    в аудите запись status='ok' (как при запуске чата)."""
    db, user_id = db_and_user
    for tool, args in [
        ("ping", {"host": "srv-app-01"}),
        ("vmware_hosts", {"device": "vcenter"}),
        ("zabbix_problems", {"device": "zbx"}),
        ("get_infrastructure_health", {"device": "vcenter"}),
    ]:
        _, status = _execute(tool, args, user_id)
        assert status == "ok", f"{tool}: status={status}"
    ok_count = db.query(AuditLog).filter_by(status="ok").count()
    assert ok_count == 4
