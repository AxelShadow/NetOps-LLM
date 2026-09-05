"""Тесты расширений внутреннего API: X-Conversation-Id (Фаза 6),
/internal/auth-check и поле id в /api/auth/me (Фаза 7);
percent-encoding display_name и нечисловой sub (Фаза 10).

Стиль как у internal_api_test.py: TestClient на временной sqlite (env
задаётся ДО импорта app), мок-режим LLM/инструментов, dev-логин.
Рабочая netops.db не затрагивается.

Запуск: python internal_ext_test.py  (из каталога backend/)
"""
import os
import sys
import tempfile
import datetime as dt
from urllib.parse import quote

import jwt as pyjwt

TMP = tempfile.mkdtemp(prefix="netops_internal_ext_")
os.environ["NETOPS_DATABASE_URL"] = f"sqlite:///{TMP}/internal_ext_test.db".replace("\\", "/")
os.environ["NETOPS_MOCK_MODE"] = "true"
os.environ["NETOPS_DEV_MODE"] = "true"
os.environ["NETOPS_INTERNAL_SERVICE_TOKEN"] = "test-token"
os.environ["NETOPS_AD_DOMAIN"] = "mock.local"
os.environ.setdefault("NETOPS_BOOTSTRAP_ADMIN", "admin@mock.local")
os.environ.setdefault("NETOPS_JWT_SECRET", "internal-ext-test-secret")
os.environ["NETOPS_LLM_BASE_URL"] = "http://localhost:1234/v1"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import User, Conversation  # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, ok: bool, detail: str = ""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"PASS: {name}")
    else:
        FAIL += 1
        print(f"FAIL: {name} {detail}")


def main():
    with TestClient(app) as client:
        r = client.post("/api/auth/login",
                        json={"username": "admin@mock.local", "password": "x"})
        assert r.status_code == 200, f"dev-логин: {r.status_code} {r.text}"
        token = r.json()["token"]

        with SessionLocal() as db:
            admin = db.query(User).filter_by(role="admin").first()
            admin_id = admin.id
            admin_username = admin.username
            admin_display_name = admin.display_name

        headers_ok = {"X-Internal-Service-Token": "test-token",
                      "X-User-Id": str(admin_id)}

        # ---------- Фаза 6: X-Conversation-Id ----------

        # (1) новый диалог: заголовок присутствует и числовой
        r = client.post("/internal/chat/stream",
                        json={"content": "Сколько времени?"},
                        headers=headers_ok)
        check("1: новый диалог 200", r.status_code == 200,
              f"got {r.status_code} {r.text[:200]}")
        cid_hdr = r.headers.get("x-conversation-id", "")
        check("1: X-Conversation-Id присутствует и числовой",
              cid_hdr.isdigit(), f"hdr={cid_hdr!r}")

        # (2) совпадает с последним диалогом пользователя в БД
        with SessionLocal() as db:
            conv = db.query(Conversation).filter_by(
                user_id=admin_id).order_by(Conversation.id.desc()).first()
            check("2: заголовок == id диалога в БД",
                  conv is not None and cid_hdr == str(conv.id),
                  f"hdr={cid_hdr!r} db={conv.id if conv else None}")
            conv_id = conv.id

        # (3) продолжение диалога: тот же id в заголовке
        r = client.post("/internal/chat/stream",
                        json={"content": "Ещё раз", "conversation_id": conv_id},
                        headers=headers_ok)
        check("3: продолжение 200", r.status_code == 200,
              f"got {r.status_code}")
        check("3: X-Conversation-Id тот же при продолжении",
              r.headers.get("x-conversation-id", "") == str(conv_id),
              f"hdr={r.headers.get('x-conversation-id')!r}")

        # (4) чужой/несуществующий диалог -> 404, без заголовка
        r = client.post("/internal/chat/stream",
                        json={"content": "взлом", "conversation_id": 999999},
                        headers=headers_ok)
        check("4: чужой диалог -> 404", r.status_code == 404,
              f"got {r.status_code}")
        check("4: заголовка нет при 404",
              "x-conversation-id" not in r.headers)

        # ---------- Фаза 7a: /internal/auth-check + /me id ----------

        def auth_check(headers=None, cookies=None):
            return client.get("/internal/auth-check",
                              headers=headers, cookies=cookies)

        # (5) без сервисного токена -> 401
        r = auth_check()
        check("5: auth-check без сервисного токена -> 401",
              r.status_code == 401, f"got {r.status_code}")

        # (6) с токеном, без пользовательского токена -> 401
        r = auth_check(headers={"X-Internal-Service-Token": "test-token"})
        check("6: auth-check без cookie/Bearer -> 401",
              r.status_code == 401, f"got {r.status_code}")

        # (7) garbage-токен -> 401
        r = auth_check(headers={"X-Internal-Service-Token": "test-token",
                                "Authorization": "Bearer garbage"})
        check("7: auth-check garbage-токен -> 401",
              r.status_code == 401, f"got {r.status_code}")

        # (8) валидный Bearer -> 204 + все 4 заголовка
        r = auth_check(headers={"X-Internal-Service-Token": "test-token",
                                "Authorization": f"Bearer {token}"})
        check("8: auth-check Bearer -> 204", r.status_code == 204,
              f"got {r.status_code} {r.text[:200]}")
        # display_name с Фазы 10 percent-encoded (latin-1-safe заголовки)
        check("8: все 4 заголовка пользователя",
              r.headers.get("x-user-id") == str(admin_id)
              and r.headers.get("x-user-email") == admin_username
              and r.headers.get("x-user-role") == "admin"
              and r.headers.get("x-user-display-name")
              == quote(admin_display_name or "", safe=""),
              f"id={r.headers.get('x-user-id')!r} "
              f"email={r.headers.get('x-user-email')!r} "
              f"role={r.headers.get('x-user-role')!r} "
              f"dn={r.headers.get('x-user-display-name')!r}")

        # (9) валидная cookie netops_token -> 204
        r = auth_check(headers={"X-Internal-Service-Token": "test-token"},
                       cookies={"netops_token": token})
        check("9: auth-check cookie -> 204", r.status_code == 204,
              f"got {r.status_code}")

        # (10) деактивированный юзер -> 403 (потом восстановить)
        with SessionLocal() as db:
            db.get(User, admin_id).is_active = False
            db.commit()
        r = auth_check(headers={"X-Internal-Service-Token": "test-token",
                                "Authorization": f"Bearer {token}"})
        check("10: auth-check деактивированный -> 403",
              r.status_code == 403, f"got {r.status_code}")
        with SessionLocal() as db:
            db.get(User, admin_id).is_active = True
            db.commit()

        # (11) /api/auth/me возвращает id == admin_id
        r = client.get("/api/auth/me",
                       headers={"Authorization": f"Bearer {token}"})
        check("11: /me возвращает id", r.status_code == 200
              and r.json().get("id") == admin_id,
              f"got {r.status_code} {r.text[:200]}")

        # (12) набор полей /me == {id, username, display_name, role}
        check("12: /me поля == {id, username, display_name, role}",
              set(r.json().keys()) == {"id", "username", "display_name",
                                      "role"},
              f"keys={sorted(r.json().keys())}")

        # ---------- Фаза 10: encoding display_name + нечисловой sub ----------

        # (13) юзер с кириллическим display_name -> 204, заголовок
        # percent-encoded (latin-1), без UnicodeEncodeError (500)
        with SessionLocal() as db:
            db.get(User, admin_id).display_name = "Шатов А.В."
            db.commit()
        r = auth_check(headers={"X-Internal-Service-Token": "test-token",
                                "Authorization": f"Bearer {token}"})
        dn_hdr = r.headers.get("x-user-display-name", "")
        check("13: кириллица в display_name -> 204 (не 500)",
              r.status_code == 204, f"got {r.status_code} {r.text[:200]}")
        check("13: X-User-Display-Name percent-encoded ASCII",
              dn_hdr == quote("Шатов А.В.", safe="")
              and dn_hdr.isascii(),
              f"dn={dn_hdr!r} expected={quote('Шатов А.В.', safe='')!r}")
        with SessionLocal() as db:
            db.get(User, admin_id).display_name = admin_display_name
            db.commit()

        # (14) JWT с нечисловым sub -> 401, не 500 (int() guard)
        def _jwt(payload: dict) -> str:
            payload = {"exp": dt.datetime.now(dt.UTC) + dt.timedelta(hours=1),
                       **payload}
            return pyjwt.encode(payload, os.environ["NETOPS_JWT_SECRET"],
                                algorithm="HS256")

        bad_sub = _jwt({"sub": "abc", "username": "x", "role": "admin"})
        r = auth_check(headers={"X-Internal-Service-Token": "test-token",
                                "Authorization": f"Bearer {bad_sub}"})
        check("14: auth-check нечисловой sub -> 401 (не 500)",
              r.status_code == 401, f"got {r.status_code} {r.text[:200]}")

        # (15) JWT без sub -> 401, не 500
        no_sub = _jwt({"username": "x", "role": "admin"})
        r = auth_check(headers={"X-Internal-Service-Token": "test-token",
                                "Authorization": f"Bearer {no_sub}"})
        check("15: auth-check без sub -> 401 (не 500)",
              r.status_code == 401, f"got {r.status_code} {r.text[:200]}")

        # (16) регресс deps.py: нечисловой sub в Bearer -> 401, не 500
        r = client.get("/api/auth/me",
                       headers={"Authorization": f"Bearer {bad_sub}"})
        check("16: /api/auth/me нечисловой sub -> 401 (не 500)",
              r.status_code == 401, f"got {r.status_code} {r.text[:200]}")

        # (17) регресс deps.py: валидный токен по-прежнему работает
        r = client.get("/api/auth/me",
                       headers={"Authorization": f"Bearer {token}"})
        check("17: /api/auth/me валидный токен -> 200",
              r.status_code == 200, f"got {r.status_code} {r.text[:200]}")

    print(f"\nИтого: PASS={PASS} FAIL={FAIL}")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
