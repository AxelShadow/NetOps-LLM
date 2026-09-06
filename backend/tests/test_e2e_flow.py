"""Сквозной тест Фазы 13 «Тестирование» миграции UI (migration.md, «4. Сквозной тест»).

Все 14 шагов migration.md против TestClient на временной sqlite (env
задаётся ДО импорта app): логин/админка/инвентарь/аудит через cookie
netops_token, чат — по внутреннему маршруту /internal/chat/stream
(путь Chainlit), мок-режим LLM/инструментов, dev-логин. Рабочая netops.db
не затрагивается.

Факты прод-кода, на которые опираются проверки:
- все реальные инструменты регистрируются без roles= (tools.py:87:
  tool_roles = viewer/engineer/admin), поэтому чат viewer выполняет
  ping без denied — шаг 14;
- /admin/audit доступен только admin (ui/router.py:
  require_roles_page(Role.admin)); engineer получает 403 — 12b-12d;
- заголовок диалога = первое user-сообщение (content[:60],
  api/internal.py) — шаг 11;
- flash создания устройства: «Устройство добавлено» (ui/router.py
  inventory_create) — шаг 4.

Сценарий чата: сообщение «пинг mock-host» (НЕ содержит глухих триггеров
«лимит»/«ошибк») → 1-й ответ tool_call ping {"device": "mock-host"} →
после role=tool — финальный текст «Это детерминированный ответ
мок-режима NETOPS_MOCK_MODE. Сетевые вызовы не выполнялись.»

Запуск: .venv/Scripts/python.exe tests/test_e2e_flow.py  (из backend/)
"""
import json
import os
import sys
import tempfile
from pathlib import Path

# Windows-консоль по умолчанию cp1251: «→»/«„» не кодируются
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

TMP = tempfile.mkdtemp(prefix="netops_e2e_flow_")
os.environ["NETOPS_DATABASE_URL"] = f"sqlite:///{TMP}/e2e_flow_test.db".replace("\\", "/")
os.environ["NETOPS_MOCK_MODE"] = "true"
os.environ["NETOPS_DEV_MODE"] = "true"
os.environ["NETOPS_INTERNAL_SERVICE_TOKEN"] = "test-token"
os.environ["NETOPS_AD_DOMAIN"] = "mock.local"
os.environ.setdefault("NETOPS_BOOTSTRAP_ADMIN", "admin@mock.local")
os.environ.setdefault("NETOPS_JWT_SECRET", "e2e-flow-test-secret")
os.environ["NETOPS_LLM_BASE_URL"] = "http://localhost:1234/v1"
os.environ.pop("NETOPS_ZABBIX_URL", None)
os.environ.pop("NETOPS_ZABBIX_TOKEN", None)
os.environ["NETOPS_ZABBIX_URL"] = ""    # перекрывает backend/.env (если есть)
os.environ["NETOPS_ZABBIX_TOKEN"] = ""

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import Device, DeviceType, Role, User  # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, ok: bool, detail: str = ""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"PASS: {name}")
    else:
        FAIL += 1
        print(f"FAIL: {name} {detail}")


def parse_sse(resp) -> list:
    """Разбор SSE-кадров data: {...} + [DONE] в список полезной нагрузки."""
    events = []
    for line in resp.text.split("\n"):
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            events.append("[DONE]")
        elif payload:
            try:
                events.append(json.loads(payload))
            except json.JSONDecodeError:
                pass
    return events


def tool_event(events) -> dict | None:
    """Кадр вызова инструмента ({"tool": ..., "args": ...})."""
    return next((e for e in events if isinstance(e, dict)
                 and "tool" in e and "tool_result" not in e), None)


def tool_result_event(events) -> dict | None:
    """Кадр результата инструмента ({"tool_result": {...}})."""
    return next((e for e in events if isinstance(e, dict)
                 and isinstance(e.get("tool_result"), dict)), None)


def login(client, username: str):
    """Форма /admin/login (form-urlencoded, без follow) → (resp, cookie)."""
    r = client.post("/admin/login", data={"username": username,
                                          "password": "x"},
                    follow_redirects=False)
    return r, r.cookies.get("netops_token")


def main():
    with TestClient(app) as client:
        # --- Посев пользователей (после lifespan: create_all + bootstrap) ---
        with SessionLocal() as db:
            admin = db.query(User).filter_by(role="admin").first()
            assert admin is not None, "bootstrap-админ не создан"
            admin_id = admin.id
            engineer = User(username="engineer@mock.local",
                            display_name="Инженер", role=Role.engineer,
                            is_active=True, granted_by="test")
            viewer = User(username="viewer@mock.local",
                         display_name="Вьюер", role=Role.viewer,
                         is_active=True, granted_by="test")
            db.add_all([engineer, viewer])
            db.flush()
            engineer_id, viewer_id = engineer.id, viewer.id
            db.commit()

        # --- Шаг 1: логин администратором ---
        r, admin_token = login(client, "admin@mock.local")
        check("1: логин админом → 303 + cookie netops_token",
              r.status_code == 303 and bool(admin_token),
              f"got {r.status_code}")
        check("1b: cookie HttpOnly",
              "httponly" in r.headers.get("set-cookie", "").lower(),
              r.headers.get("set-cookie", "")[:80])

        # --- Шаг 2: dashboard ---
        r = client.get("/admin/", follow_redirects=False)
        check("2: /admin/ (dashboard) → 200 + имя пользователя",
              r.status_code == 200 and "Dashboard" in r.text
              and ">admin<" in r.text,
              f"got {r.status_code}")

        # --- Шаг 3: инвентарь (страница + HTMX-фрагмент) ---
        r = client.get("/admin/inventory", follow_redirects=False)
        check("3: /admin/inventory → 200",
              r.status_code == 200 and "Инвентарь" in r.text,
              f"got {r.status_code}")
        r = client.get("/admin/inventory/partial/table")
        check("3b: partial/table → HTML-фрагмент (без <!DOCTYPE)",
              r.status_code == 200 and "<!DOCTYPE" not in r.text,
              f"got {r.status_code}")

        # --- Шаг 4: создание устройства (HTMX POST, как из модалки) ---
        r = client.post("/admin/inventory", data={
            "name": "e2e-test-sw", "type": "mikrotik", "host": "192.0.2.50",
            "port": "22", "username": "", "password": "",
            "enabled": "on", "description": "e2e", "group": "e2e"})
        check("4: POST /admin/inventory → flash + устройство в таблице",
              r.status_code == 200 and "Устройство добавлено" in r.text
              and "e2e-test-sw" in r.text,
              f"got {r.status_code}")
        with SessionLocal() as db:
            d = db.query(Device).filter_by(name="e2e-test-sw").first()
            check("4b: Device в БД (name/host/enabled/type)",
                  d is not None and d.host == "192.0.2.50"
                  and d.enabled is True and d.type == DeviceType.mikrotik,
                  f"device={d!r}")

        # --- Шаг 5: аудит (только admin) ---
        r = client.get("/admin/audit", follow_redirects=False)
        check("5: /admin/audit → 200 (admin)",
              r.status_code == 200 and "Аудит" in r.text,
              f"got {r.status_code}")
        r = client.get("/admin/audit/partial/table")
        check("5b: audit partial/table → 200", r.status_code == 200,
             f"got {r.status_code}")

        # --- Шаги 6-10: чат admin через /internal/chat/stream ---
        # «пинг mock-host»: пинг-сценарий мока (без триггеров «лимит»/«ошибк»)
        r = client.post("/internal/chat/stream",
                        json={"content": "пинг mock-host"},
                        headers={"X-Internal-Service-Token": "test-token",
                                 "X-User-Id": str(admin_id)})
        conv_id = r.headers.get("x-conversation-id", "")
        events = parse_sse(r) if r.status_code == 200 else []
        check("6: POST /internal/chat/stream «пинг mock-host» → 200 "
              "+ X-Conversation-Id",
              r.status_code == 200 and conv_id.isdigit()
              and r.headers.get("content-type", "").startswith(
                  "text/event-stream"),
              f"got {r.status_code}, conv={conv_id!r}")

        ev = tool_event(events)
        check("7: кадр tool ping {device: mock-host}",
              ev is not None and ev.get("tool") == "ping"
              and ev.get("args", {}).get("device") == "mock-host",
              f"ev={ev}")

        tr = tool_result_event(events)
        check("8: tool_result ok=true, preview содержит «Обмен пакетами»",
              tr is not None and tr["tool_result"].get("ok") is True
              and "Обмен пакетами" in tr["tool_result"].get("preview", ""),
              f"tr={tr}")

        deltas = "".join(e["delta"] for e in events
                         if isinstance(e, dict) and "delta" in e)
        check("9: финальный delta содержит «Это детерминированный ответ "
              "мок-режима»",
              "Это детерминированный ответ мок-режима" in deltas,
              f"delta={deltas[:80]!r}")

        check("10: [DONE] — последний кадр",
              bool(events) and events[-1] == "[DONE]",
              f"last={events[-1] if events else None!r}")

        # --- Шаг 11: диалог в истории ---
        r = client.get("/admin/conversations/partial/table")
        check("11: диалог в истории (id + заголовок = первое сообщение)",
              r.status_code == 200 and f">{conv_id}<" in r.text
              and "пинг mock-host" in r.text,
              f"got {r.status_code}")
        r = client.get(f"/admin/conversations/{conv_id}/details")
        check("11b: детали: user-сообщение, tool-результат, финальный текст",
              r.status_code == 200 and "пинг mock-host" in r.text
              and "ping" in r.text and "Обмен пакетами" in r.text
              and "Это детерминированный ответ мок-режима" in r.text,
              f"got {r.status_code}")

        # --- Шаг 12: вызов ping в аудите ---
        r = client.get("/admin/audit/partial/table",
                       params={"dialog": conv_id})
        check("12: вызов ping в аудите (dialog=): ping + ok + admin",
              r.status_code == 200 and ">ping<" in r.text
              and ">ok<" in r.text and ">admin<" in r.text,
              f"got {r.status_code}")

        # --- Шаг 12b-12d: engineer — инвентарь да, аудит нет ---
        r, _ = login(client, "engineer@mock.local")
        check("12b: логин engineer → 303", r.status_code == 303,
             f"got {r.status_code}")
        r = client.get("/admin/inventory", follow_redirects=False)
        check("12c: engineer: /admin/inventory → 200",
              r.status_code == 200, f"got {r.status_code}")
        r = client.get("/admin/audit", follow_redirects=False)
        check("12d: engineer: /admin/audit → 403 (только admin)",
              r.status_code == 403 or (
                  r.status_code in (302, 303)
                  and "/admin/login" in r.headers.get("location", "")),
              f"got {r.status_code}")

        # --- Шаг 13: вход viewer, разделы недоступны ---
        r, viewer_token = login(client, "viewer@mock.local")
        check("13: логин viewer → 303 + cookie",
              r.status_code == 303 and bool(viewer_token),
              f"got {r.status_code}")
        r = client.get("/admin/", follow_redirects=False)
        check("13: viewer: /admin/ → 200 (dashboard)",
              r.status_code == 200 and "Вьюер" in r.text,
              f"got {r.status_code}")
        r = client.get("/admin/audit", follow_redirects=False)
        check("13b: viewer: /admin/audit → 403 или редирект",
              r.status_code == 403 or (
                  r.status_code in (302, 303)
                  and "/admin/login" in r.headers.get("location", "")),
              f"got {r.status_code}")
        r = client.get("/admin/inventory", follow_redirects=False)
        check("13c: viewer: /admin/inventory → 403",
              r.status_code == 403 or (
                  r.status_code in (302, 303)
                  and "/admin/login" in r.headers.get("location", "")),
              f"got {r.status_code}")
        r = client.post("/admin/inventory",
                        data={"name": "hack", "type": "eltex", "host": "x"},
                        follow_redirects=False)
        check("13d: viewer: POST /admin/inventory → 403",
              r.status_code == 403 or (
                  r.status_code in (302, 303)
                  and "/admin/login" in r.headers.get("location", "")),
              f"got {r.status_code}")
        r = client.get("/admin/conversations", follow_redirects=False)
        check("13e: viewer: /admin/conversations → 403",
              r.status_code == 403 or (
                  r.status_code in (302, 303)
                  and "/admin/login" in r.headers.get("location", "")),
              f"got {r.status_code}")
        r = client.get("/admin/settings", follow_redirects=False)
        check("13f: viewer: /admin/settings → 403",
              r.status_code == 403 or (
                  r.status_code in (302, 303)
                  and "/admin/login" in r.headers.get("location", "")),
              f"got {r.status_code}")

        # --- Шаг 14: чат viewer работает + аудит его вызова ---
        # Чат идёт по заголовкам сервисного токена + X-User-Id, cookie
        # страницы не участвует. ping допускает viewer (tools.py:87).
        rv = client.post("/internal/chat/stream",
                         json={"content": "пинг mock-host"},
                         headers={"X-Internal-Service-Token": "test-token",
                                  "X-User-Id": str(viewer_id)})
        v_events = parse_sse(rv) if rv.status_code == 200 else []
        v_conv_id = rv.headers.get("x-conversation-id", "")
        v_tr = tool_result_event(v_events)
        check("14: чат viewer работает — ping ok",
              rv.status_code == 200 and v_conv_id.isdigit()
              and v_tr is not None
              and v_tr["tool_result"].get("ok") is True,
              f"got {rv.status_code}, tr={v_tr}")
        # аудит доступен только admin — возвращаем cookie администратора
        client.cookies.set("netops_token", admin_token)
        r = client.get("/admin/audit/partial/table",
                       params={"dialog": v_conv_id})
        check("14b: аудит viewer: запись ping ok + пользователь viewer",
              r.status_code == 200 and ">ping<" in r.text
              and ">ok<" in r.text and "viewer@mock.local" in r.text,
              f"got {r.status_code}")

    print(f"\nИтог: PASS={PASS} FAIL={FAIL}")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
