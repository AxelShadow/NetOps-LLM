"""Юнит-тест RBAC-веток execute_tool (Фаза 13 «Тестирование»).

Изоляция БЕЗ app.main (стиль tests/test_mock_mode.py): временная sqlite,
env задаётся ДО импорта app.*, рабочая netops.db не затрагивается.

Ключевой факт: все реальные инструменты регистрируются с ролями по
умолчанию ["viewer", "engineer", "admin"] (tools.py:87) — никто не
передаёт roles=. Поэтому denied-ветка тестируется временными
инструментами с roles=["admin"], регистрируемыми публичным декоратором
register_tool и удаляемыми из _registry после прогона.

Запуск: .venv/Scripts/python.exe tests/test_agent_tools_rbac.py  (из backend/)
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

# Windows-консоль по умолчанию cp1251: кириллица в PASS/FAIL-строках
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TMP = tempfile.mkdtemp(prefix="netops_rbac_")
os.environ["NETOPS_DATABASE_URL"] = f"sqlite:///{TMP}/rbac_test.db".replace("\\", "/")
os.environ["NETOPS_MOCK_MODE"] = "true"
os.environ["NETOPS_DEV_MODE"] = "true"
os.environ["NETOPS_JWT_SECRET"] = "rbac-tools-test-secret"
os.environ["NETOPS_LLM_BASE_URL"] = "http://127.0.0.1:9/v1"  # ничего не слушает

from app.agent.tools import (  # noqa: E402
    _registry, execute_tool, register_tool,
)
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import AuditLog, User  # noqa: E402

# Схема БД для теста (без полного lifespan приложения)
Base.metadata.create_all(engine)

PASS, FAIL = 0, 0


def check(name: str, ok: bool, detail: str = ""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"PASS: {name}")
    else:
        FAIL += 1
        print(f"FAIL: {name} {detail}")


def _clear_audit():
    """execute_tool пишет аудит в собственной сессии — чистим таблицу
    перед каждым сценарием, чтобы проверять ровно одну запись."""
    with SessionLocal() as db:
        db.query(AuditLog).delete()
        db.commit()


def _last_audit(tool: str) -> AuditLog | None:
    with SessionLocal() as db:
        return (db.query(AuditLog).filter_by(tool=tool)
                .order_by(AuditLog.id.desc()).first())


# --- тестовые инструменты: в реестре только на время прогона ---

@register_tool(name="t_deny_admin", description="тест RBAC",
               parameters={}, roles=["admin"])
def _t_deny_admin(**kwargs):
    return "ok-result", "ok"


@register_tool(name="t_forbidden", description="тест исключения Запрещено",
               parameters={})
def _t_forbidden(**kwargs):
    raise Exception("Запрещено: тест")


@register_tool(name="t_boom", description="тест прочего исключения",
               parameters={})
def _t_boom(**kwargs):
    raise Exception("boom")


@register_tool(name="t_big", description="тест обрезки аудита (5000)",
               parameters={})
def _t_big(**kwargs):
    return "x" * 5000, "ok"


@register_tool(name="t_huge", description="тест MAX_RESULT (25000)",
               parameters={})
def _t_huge(**kwargs):
    return "y" * 25000, "ok"


def main():
    # Пользователь для аудита: sqlite не enforce'ит FK, но надёжнее
    # создать реального User (AuditLog.user_id -> users.id).
    with SessionLocal() as db:
        user = User(username="rbac@mock.local", display_name="РБАС",
                    role="viewer", is_active=True, granted_by="test")
        db.add(user)
        db.commit()
        db.refresh(user)
        uid = user.id

    try:
        # --- unknown: инструмент не найден ---
        _clear_audit()
        result, status = execute_tool("no_such_tool", {}, uid, None, "admin")
        check("unknown: результат + status",
              result == "Инструмент 'no_such_tool' не найден в системе."
              and status == "error",
              f"result={result!r} status={status!r}")
        log = _last_audit("no_such_tool")
        check("unknown: аудит error",
              log is not None and log.status == "error"
              and log.arguments == "{}" and log.user_id == uid,
              f"log={log and (log.status, log.arguments, log.user_id)}")

        # --- denied: viewer против roles=["admin"] ---
        _clear_audit()
        result, status = execute_tool("t_deny_admin", {}, uid, None, "viewer")
        check("denied: viewer против roles=[admin]",
              result == "Недостаточно прав (роль: viewer) "
                        "для выполнения 't_deny_admin'."
              and status == "denied",
              f"result={result!r} status={status!r}")
        log = _last_audit("t_deny_admin")
        check("denied: аудит (duration_ms=None)",
              log is not None and log.status == "denied"
              and log.duration_ms is None and log.user_id == uid,
              f"log={log and (log.status, log.duration_ms, log.user_id)}")

        # --- allowed: admin против roles=["admin"] ---
        # Заодно: мок-режим не перехватывает инструменты вне MOCK_TOOLS —
        # t_deny_admin отсутствует в mock.MOCK_TOOLS, поэтому wrapper
        # доходит до реальной функции и возвращает "ok-result".
        _clear_audit()
        result, status = execute_tool("t_deny_admin", {}, uid, None, "admin")
        check("allowed: admin, мок не перехватывает t_*",
              result == "ok-result" and status == "ok",
              f"result={result[:50]!r} status={status!r}")
        log = _last_audit("t_deny_admin")
        check("allowed: аудит ok, duration_ms>=0",
              log is not None and log.status == "ok"
              and log.duration_ms is not None and log.duration_ms >= 0,
              f"duration_ms={log and log.duration_ms}")

        # --- исключение «Запрещено» -> denied (tools.py:809-810) ---
        _clear_audit()
        result, status = execute_tool("t_forbidden", {}, uid, None, "admin")
        check("запрещено-исключение -> denied (str(e) целиком)",
              status == "denied" and result == "Запрещено: тест",
              f"result={result!r} status={status!r}")

        # --- прочее исключение -> error ---
        _clear_audit()
        result, status = execute_tool("t_boom", {}, uid, None, "admin")
        log = _last_audit("t_boom")
        check("прочее исключение -> error + аудит error",
              status == "error" and result == "Ошибка выполнения: boom"
              and log is not None and log.status == "error",
              f"result={result!r} status={status!r} "
              f"audit={log and log.status}")

        # --- обрезка: аудит 4000, возврат без обрезки до MAX_RESULT ---
        _clear_audit()
        result, status = execute_tool("t_big", {}, uid, None, "admin")
        log = _last_audit("t_big")
        check("обрезка: аудит 4000 при результате 5000 (возврат полный)",
              status == "ok" and len(result) == 5000
              and log is not None and len(log.result or "") == 4000,
              f"ret={len(result)} audit={log and len(log.result or '')}")

        _clear_audit()
        result, status = execute_tool("t_huge", {}, uid, None, "admin")
        log = _last_audit("t_huge")
        check("обрезка: возврат MAX_RESULT=20000, аудит 4000",
              status == "ok" and len(result) == 20000
              and log is not None and len(log.result or "") == 4000,
              f"ret={len(result)} audit={log and len(log.result or '')}")

        # --- инвариант: все реальные инструменты допускают viewer ---
        # denied-ветка недостижима ни одной реальной ролью; при смене
        # RBAC-политик проверку обновить.
        real_tools = {name: f for name, f in _registry.items()
                      if not name.startswith("t_")}
        bad = [n for n, f in real_tools.items()
               if "viewer" not in f.tool_roles]
        check("инвариант: viewer допущен ко всем реальным инструментам",
              bool(real_tools) and not bad,
              f"без viewer: {bad}; всего инструментов: {len(real_tools)}")

        # --- duration_ms: ok >= 0; denied/unknown = None ---
        _clear_audit()
        execute_tool("no_such_tool", {}, uid, None, "admin")
        execute_tool("t_deny_admin", {}, uid, None, "viewer")
        execute_tool("t_deny_admin", {}, uid, None, "admin")
        with SessionLocal() as db:
            logs = db.query(AuditLog).order_by(AuditLog.id).all()
        ok_log = next((l for l in logs if l.status == "ok"), None)
        denied_log = next((l for l in logs if l.status == "denied"), None)
        unknown_log = next((l for l in logs if l.status == "error"), None)
        check("duration_ms: ok>=0, denied/unknown=None",
              ok_log is not None and ok_log.duration_ms is not None
              and ok_log.duration_ms >= 0
              and denied_log is not None and denied_log.duration_ms is None
              and unknown_log is not None and unknown_log.duration_ms is None,
              f"ok={ok_log and ok_log.duration_ms} "
              f"denied={denied_log and denied_log.duration_ms} "
              f"unknown={unknown_log and unknown_log.duration_ms}")

        # --- RBAC-проверка до мок-диспетчеризации ---
        # Порядок в tools.py: проверка ролей в execute_tool (строка 798)
        # ДО вызова wrapper, а мок-перехват — внутри wrapper (строки
        # 55-59). Проверяем на инструменте из MOCK_TOOLS: временно
        # перерегистрируем "ping" с roles=["admin"] и зовём viewer'ом —
        # мок не должен отдать фейковые данные, ветка denied раньше.
        _clear_audit()
        real_ping = _registry["ping"]

        @register_tool(name="ping", description="тест мок-RBAC",
                       parameters={}, roles=["admin"])
        def _ping_admin(**kwargs):
            return "never-called", "ok"
        try:
            result, status = execute_tool(
                "ping", {"host": "mock-host"}, uid, None, "viewer")
            check("RBAC до мок-диспетчеризации: viewer -> denied",
                  status == "denied" and "Недостаточно прав" in result
                  and result != "never-called",
                  f"result={result[:80]!r} status={status!r}")
        finally:
            _registry["ping"] = real_ping  # вернуть настоящий ping
    finally:
        # гигиена: удалить тестовые инструменты из реестра
        for name in ("t_deny_admin", "t_forbidden", "t_boom",
                     "t_big", "t_huge"):
            _registry.pop(name, None)

    print(f"\nИтого: PASS={PASS} FAIL={FAIL}")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
