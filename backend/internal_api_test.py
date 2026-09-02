"""Интеграционный тест внутреннего API /internal/chat/stream (Фаза 1).

Стиль как у mock_smoke.py: TestClient на временной sqlite (env задаётся ДО
импорта app), мок-режим LLM/инструментов, dev-логин. Рабочая netops.db
не затрагивается.

Запуск: python internal_api_test.py  (из каталога backend/)
"""
import os
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="netops_internal_")
os.environ["NETOPS_DATABASE_URL"] = f"sqlite:///{TMP}/internal_test.db".replace("\\", "/")
os.environ["NETOPS_MOCK_MODE"] = "true"
os.environ["NETOPS_DEV_MODE"] = "true"
os.environ["NETOPS_INTERNAL_SERVICE_TOKEN"] = "test-token"
os.environ["NETOPS_AD_DOMAIN"] = "mock.local"
os.environ.setdefault("NETOPS_BOOTSTRAP_ADMIN", "admin@mock.local")
os.environ.setdefault("NETOPS_JWT_SECRET", "internal-test-secret")
os.environ["NETOPS_LLM_BASE_URL"] = "http://localhost:1234/v1"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import User, Conversation, Message  # noqa: E402

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
            import json
            try:
                events.append(json.loads(payload))
            except json.JSONDecodeError:
                pass
    return events


def main():
    with TestClient(app) as client:
        r = client.post("/api/auth/login",
                        json={"username": "admin@mock.local", "password": "x"})
        assert r.status_code == 200, f"dev-логин: {r.status_code} {r.text}"
        token = r.json()["token"]

        with SessionLocal() as db:
            admin = db.query(User).filter_by(role="admin").first()
            admin_id = admin.id
            # Неактивный пользователь
            inactive = User(username="sleepy", display_name="Слипи",
                            role="viewer", is_active=False,
                            granted_by="test")
            db.add(inactive)
            db.commit()
            db.refresh(inactive)
            inactive_id = inactive.id

        headers_ok = {"X-Internal-Service-Token": "test-token",
                      "X-User-Id": str(admin_id)}

        # (a) без сервисного токена -> 401
        r = client.post("/internal/chat/stream",
                        json={"content": "привет"})
        check("a: без токена -> 401", r.status_code == 401,
              f"got {r.status_code}")

        # (b) неверный токен -> 401
        r = client.post("/internal/chat/stream",
                        json={"content": "привет"},
                        headers={"X-Internal-Service-Token": "wrong",
                                 "X-User-Id": str(admin_id)})
        check("b: неверный токен -> 401", r.status_code == 401,
              f"got {r.status_code}")

        # (c) корректный токен: обычный сценарий, новый диалог (f)
        r = client.post("/internal/chat/stream",
                        json={"content": "Сколько времени?"},
                        headers=headers_ok)
        check("c: 200", r.status_code == 200, f"got {r.status_code} {r.text[:200]}")
        events = parse_sse(r)
        has_tool = any("tool" in e and "tool_result" not in e for e in events
                       if isinstance(e, dict))
        has_tr = any("tool_result" in e for e in events if isinstance(e, dict))
        has_delta = any("delta" in e for e in events if isinstance(e, dict))
        has_done = "[DONE]" in events
        check("c: есть tool", has_tool)
        check("c: есть tool_result", has_tr)
        check("c: есть delta", has_delta)
        check("c: есть [DONE]", has_done)

        # (f) новый диалог создан, ответ записан
        with SessionLocal() as db:
            conv = db.query(Conversation).filter_by(
                user_id=admin_id).order_by(Conversation.id.desc()).first()
            check("f: диалог создан", conv is not None)
            msgs = db.query(Message).filter_by(
                conversation_id=conv.id).all()
            roles = [m.role for m in msgs]
            check("f: ответ записан в диалог", "assistant" in roles,
                  f"roles={roles}")
            conv_id = conv.id

        # (c2) повтор в существующий диалог
        r = client.post("/internal/chat/stream",
                        json={"content": "Ещё раз привет",
                              "conversation_id": conv_id},
                        headers=headers_ok)
        check("c2: существующий диалог 200", r.status_code == 200,
              f"got {r.status_code}")

        # чужой диалог
        r = client.post("/internal/chat/stream",
                        json={"content": "взлом", "conversation_id": 999999},
                        headers=headers_ok)
        check("c3: несуществующий диалог -> 404", r.status_code == 404,
              f"got {r.status_code}")

        # (d) неизвестный X-User-Id -> 401/403
        r = client.post("/internal/chat/stream",
                        json={"content": "привет"},
                        headers={"X-Internal-Service-Token": "test-token",
                                 "X-User-Id": "999999"})
        check("d: неизвестный user -> 401/403",
              r.status_code in (401, 403), f"got {r.status_code}")

        # неактивный пользователь -> 403
        r = client.post("/internal/chat/stream",
                        json={"content": "привет"},
                        headers={"X-Internal-Service-Token": "test-token",
                                 "X-User-Id": str(inactive_id)})
        check("d2: неактивный user -> 403", r.status_code == 403,
              f"got {r.status_code}")

        # (e) аудит: есть tool-записи от admin
        r = client.get("/api/audit?limit=50",
                       headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200, f"аудит: {r.status_code}"
        audit = r.json()
        if isinstance(audit, dict):
            audit = audit.get("items", audit.get("data", []))
        check("e: аудит содержит tool-вызовы",
              len(audit) > 0 and all("tool" in a or "arguments" in a
                                    for a in audit[:3]),
              f"len={len(audit)}")

        # пустое сообщение
        r = client.post("/internal/chat/stream",
                        json={"content": "   "}, headers=headers_ok)
        check("extra: пустое сообщение -> 400", r.status_code == 400,
              f"got {r.status_code}")

    # (g) при NETOPS_INTERNAL_SERVICE_TOKEN="" маршрут выключен.
    # get_settings кэшируется lru_cache, поэтому проверяем через новое
    # приложение с другой БД/токеном.
    TMP2 = tempfile.mkdtemp(prefix="netops_internal2_")
    os.environ["NETOPS_DATABASE_URL"] = f"sqlite:///{TMP2}/internal2.db"
    os.environ["NETOPS_INTERNAL_SERVICE_TOKEN"] = ""
    import importlib
    import app.config as cfg
    import app.main as main_mod
    cfg.get_settings.cache_clear()
    importlib.reload(main_mod)
    with TestClient(main_mod.app) as client:
        client.post("/api/auth/login",
                    json={"username": "admin@mock.local", "password": "x"})
        r = client.post("/internal/chat/stream",
                        json={"content": "привет"},
                        headers={"X-Internal-Service-Token": "",
                                 "X-User-Id": "1"})
        check("g: пустой токен -> 401", r.status_code == 401,
              f"got {r.status_code}")

    print(f"\nИтого: PASS={PASS} FAIL={FAIL}")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
