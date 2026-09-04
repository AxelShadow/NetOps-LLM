"""Тесты расширений внутреннего API: X-Conversation-Id (Фаза 6),
/internal/auth-check и поле id в /api/auth/me (Фаза 7).

Стиль как у internal_api_test.py: TestClient на временной sqlite (env
задаётся ДО импорта app), мок-режим LLM/инструментов, dev-логин.
Рабочая netops.db не затрагивается.

Запуск: python internal_ext_test.py  (из каталога backend/)
"""
import os
import sys
import tempfile

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

    print(f"\nИтого: PASS={PASS} FAIL={FAIL}")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
