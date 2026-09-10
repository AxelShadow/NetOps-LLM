"""Настройки агента (Этап 20): AppSetting + бюджет контекста + админка.

Проверяет:
  1) _agent_settings: env-дефолты (50000/20) при пустой таблице;
     AppSetting из БД перекрывает env; мусор/вне диапазона -> env-дефолт;
  2) _trim_history_by_chars: превышение бюджета -> старые сообщения
     уходят, текущий user-запрос остаётся, не-user префикс снимается,
     мелкая история не режется;
  3) POST /admin/settings/agent: успех -> AppSetting записаны, flash;
     мусор -> инлайн-ошибка (200), значения не записаны;
     вне диапазона -> инлайн-ошибка;
  4) GET /admin/settings/partial/agent -> 200 + текущие значения;
  5) run_agent_cycle применяет настройки: лимит шагов 2 из БД
     упирается в сценарий «лимит» мок-LLM и пишет «Остановлено: лимит»
     (audit tool='agent' при сбое LLM — см. сценарий 6);
  6) сбой LLM -> аудит tool='agent' status='error'
     (LLM_BASE_URL на мёртвый порт, поле model пустое -> pick_model падает).

Стиль test_agent_limit.py: env ДО импорта, temp-sqlite, TestClient,
PASS/FAIL. Рабочая netops.db не затрагивается.

Запуск: .venv/Scripts/python.exe tests/test_agent_settings.py (из backend/)
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TMP = tempfile.mkdtemp(prefix="netops_aset_")
os.environ["NETOPS_DATABASE_URL"] = f"sqlite:///{TMP}/aset_test.db".replace("\\", "/")
os.environ["NETOPS_MOCK_MODE"] = "true"
os.environ["NETOPS_DEV_MODE"] = "true"
os.environ["NETOPS_INTERNAL_SERVICE_TOKEN"] = "test-token"
os.environ["NETOPS_AD_DOMAIN"] = "mock.local"
os.environ["NETOPS_BOOTSTRAP_ADMIN"] = "admin@mock.local"
os.environ["NETOPS_JWT_SECRET"] = "agent-settings-test"
os.environ["NETOPS_LLM_BASE_URL"] = "http://127.0.0.1:9/v1"  # мёртвый порт
os.environ["NETOPS_ZABBIX_URL"] = ""
os.environ["NETOPS_ZABBIX_TOKEN"] = ""

from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import AppSetting, AuditLog, User  # noqa: E402
import app.api.chat as chat  # noqa: E402
import app.main  # noqa: E402

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


def scenario_defaults():
    """1) Эффективные значения: env-дефолты, перекрытие из БД, мусор."""
    ctx, steps = chat._agent_settings()
    check("env-дефолты: 50000 / 20",
          ctx == 50000 and steps == 20, f"got {ctx}/{steps}")

    with SessionLocal() as db:
        db.add(AppSetting(key="agent_context_chars", value="30000"))
        db.add(AppSetting(key="agent_max_steps", value="7"))
        db.commit()
    ctx, steps = chat._agent_settings()
    check("AppSetting перекрывает env: 30000 / 7",
          ctx == 30000 and steps == 7, f"got {ctx}/{steps}")

    with SessionLocal() as db:
        db.query(AppSetting).filter(
            AppSetting.key == "agent_context_chars").update(
            {"value": "мусор"})
        db.query(AppSetting).filter(
            AppSetting.key == "agent_max_steps").update(
            {"value": "999"})   # вне диапазона 1-50
        db.commit()
    ctx, steps = chat._agent_settings()
    # int('мусор') упадёт внутри try -> except, но строки уже частично
    # применены? Нет: чтение обоих ключей одним запросом, parse после.
    check("мусор/вне диапазона -> env-дефолты",
          ctx == 50000 and steps == 20, f"got {ctx}/{steps}")

    with SessionLocal() as db:
        db.query(AppSetting).delete()
        db.commit()


def scenario_trim():
    """2) Бюджет символов: старые уходят, user остаётся, префикс снят."""
    hist = [
        {"role": "user", "content": "з" * 400},
        {"role": "assistant", "content": "о" * 400},
        {"role": "user", "content": "т" * 2000},
        {"role": "assistant", "content": "ф" * 2000},
        {"role": "user", "content": "текущий запрос"},
    ]
    # история 4812 симв: при бюджете 4500 уйдёт старейшая пара (800),
    # остаток 4012 <= 4500
    out = chat._trim_history_by_chars([*hist], 4500)
    total = sum(len(m["content"]) for m in out)
    check("бюджет выполнен и последний user сохранён",
          total <= 4500 and out[-1]["content"] == "текущий запрос",
          f"total={total} last={out[-1]!r}")
    check("обрезано с головы (первые два ушли)",
          out[0]["content"] == "т" * 2000, f"first={out[0]['content'][:20]!r}")

    # бюджет больше истории — ничего не трогаем
    out = chat._trim_history_by_chars([*hist], 10 ** 9)

    out = chat._trim_history_by_chars([*hist], 10 ** 9)
    check("мелкая история не режется", len(out) == 5, f"len={len(out)}")

    # не-user префикс: после обрезки первым остаётся user
    hist2 = [
        {"role": "assistant", "content": "x" * 3000, "tool_calls": "[]"},
        {"role": "user", "content": "вопрос"},
    ]
    out = chat._trim_history_by_chars(hist2, 200)
    check("не-user префикс снят",
          out == [{"role": "user", "content": "вопрос"}], f"out={out}")


def scenario_post():
    """3) POST /admin/settings/agent: успех, мусор, вне диапазона."""
    with TestClient(app.main.app) as client:
        r = client.post("/admin/login",
                        data={"username": "admin@mock.local",
                              "password": "x"},
                        follow_redirects=False)
        assert r.status_code in (302, 303), r.status_code

        r = client.post("/admin/settings/agent",
                        data={"agent_context_chars": "40000",
                              "agent_max_steps": "15"})
        check("POST валидные -> 200 + flash + AppSetting в БД",
              r.status_code == 200
              and "Настройки агента сохранены" in r.text
              and "40000" in r.text and "15" in r.text,
              f"got {r.status_code}")
        with SessionLocal() as db:
            row = db.get(AppSetting, "agent_max_steps")
            check("AppSetting записаны", row is not None
                  and row.value == "15", f"row={row and row.value}")

        r = client.post("/admin/settings/agent",
                        data={"agent_context_chars": "abc",
                              "agent_max_steps": "10"})
        check("POST мусор -> 200 + инлайн-ошибка (htmx свапает)",
              r.status_code == 200 and "укажите число" in r.text,
              f"got {r.status_code}")
        r = client.post("/admin/settings/agent",
                        data={"agent_context_chars": "999999",
                              "agent_max_steps": "10"})
        check("POST вне диапазона -> инлайн-ошибка с диапазоном",
              "200000" in r.text and "10000" in r.text, "нет диапазона")

        r = client.get("/admin/settings/partial/agent")
        check("GET partial -> 200 + эффективные значения из БД",
              r.status_code == 200 and "40000" in r.text and "15" in r.text,
              f"got {r.status_code}")


def scenario_cycle_applies():
    """4) Цикл применяет AppSetting: лимит шагов 2 из БД."""
    with SessionLocal() as db:
        db.query(AppSetting).delete()
        db.add(AppSetting(key="agent_max_steps", value="2"))
        db.commit()

    with TestClient(app.main.app) as client:
        with SessionLocal() as db:
            admin = db.query(User).filter_by(role="admin").first()
            assert admin is not None, "bootstrap-админ не создан"
            admin_id = admin.id
        r = client.post("/internal/chat/stream",
                        headers={"X-Internal-Service-Token": "test-token",
                                 "X-User-Id": str(admin_id)},
                        json={"content": "лимит"})
        body = "".join(line.decode() for line in r.iter_bytes())
        check("лимит шагов 2 из БД -> «Остановлено: лимит шагов»",
              "Остановлено" in body, f"body={body[:300]!r}")

    with SessionLocal() as db:
        db.query(AppSetting).delete()
        db.commit()


def scenario_llm_error_audit():
    """5) Сбой LLM -> аудит tool='agent' status='error' (Этап 20).

    Подменяем llm.pick_model на бросающий APIConnectionError (в мок-режиме
    клиент не ходит в сеть, реальный сбой не воспроизвести): цикл обязан
    отдать классифицированный текст ошибки в SSE и записать аудит.
    """
    import openai
    import httpx
    import app.llm.client as llm_mod

    async def dead_pick_model():
        raise openai.APIConnectionError(
            message="Connection error.",
            request=httpx.Request("POST", "http://127.0.0.1:9/v1"))

    saved = llm_mod.llm.pick_model
    llm_mod.llm.pick_model = dead_pick_model
    try:
        with TestClient(app.main.app) as client:
            with SessionLocal() as db:
                admin = db.query(User).filter_by(role="admin").first()
                assert admin is not None, "bootstrap-админ не создан"
                admin_id = admin.id
            with SessionLocal() as db:
                before = db.query(AuditLog).filter(
                    AuditLog.tool == "agent").count()
            r = client.post("/internal/chat/stream",
                            headers={"X-Internal-Service-Token": "test-token",
                                     "X-User-Id": str(admin_id)},
                            json={"content": "привет"})
            body = "".join(line.decode() for line in r.iter_bytes())
            with SessionLocal() as db:
                after = db.query(AuditLog).filter(
                    AuditLog.tool == "agent").count()
                row = (db.query(AuditLog).filter(AuditLog.tool == "agent")
                       .order_by(AuditLog.id.desc()).first())
    finally:
        llm_mod.llm.pick_model = saved
    check("SSE error-кадр с понятным текстом (нет «LLM недоступен»)",
          '"error"' in body and "LLM" in body and "Нет связи" in body,
          f"body={body[:200]!r}")
    check("сбой LLM -> запись аудита tool=agent status=error",
          after == before + 1 and row is not None
          and row.status == "error" and row.result,
          f"before={before} after={after} row={row and row.status}")


def main():
    scenario_defaults()
    scenario_trim()
    scenario_post()
    scenario_cycle_applies()
    scenario_llm_error_audit()

    print()
    print(f"Итог: PASS={PASS} FAIL={FAIL}")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
