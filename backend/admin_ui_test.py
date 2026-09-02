"""Интеграционный тест каркаса админки /admin/* (Фаза 2).

Стиль как у mock_smoke.py/internal_api_test.py: TestClient на временной
sqlite (env задаётся ДО импорта app), dev-логин bootstrap-админа.
Рабочая netops.db не затрагивается.

Запуск: python admin_ui_test.py  (из каталога backend/)
"""
import os
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="netops_admin_")
os.environ["NETOPS_DATABASE_URL"] = f"sqlite:///{TMP}/admin_test.db".replace("\\", "/")
os.environ["NETOPS_MOCK_MODE"] = "true"
os.environ["NETOPS_DEV_MODE"] = "true"
os.environ["NETOPS_AD_DOMAIN"] = "mock.local"
os.environ.setdefault("NETOPS_BOOTSTRAP_ADMIN", "admin@mock.local")
os.environ.setdefault("NETOPS_JWT_SECRET", "admin-test-secret")
os.environ["NETOPS_LLM_BASE_URL"] = "http://localhost:1234/v1"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import User  # noqa: E402

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
    # viewer-пользователь создаём в БД напрямую (пароль не нужен —
    # логин на страницы пойдёт через cookie-токен админа ниже).
    with TestClient(app) as client:
        # (a) без cookie -> редирект на /admin/login
        r = client.get("/admin/", follow_redirects=False)
        check("GET /admin/ без cookie -> 303 на /admin/login",
              r.status_code in (302, 303) and "/admin/login" in r.headers.get("location", ""),
              f"got {r.status_code} {r.headers.get('location')}")

        # (b) страница логина отдаётся
        r = client.get("/admin/login")
        check("GET /admin/login -> 200, форма",
              r.status_code == 200 and "password" in r.text and "/admin/login" in r.text)

        # (c) логин admin: dev-режим пропускает bootstrap-админа
        r = client.post("/admin/login",
                        data={"username": "admin@mock.local", "password": "x"},
                        follow_redirects=False)
        cookie_hdr = r.headers.get("set-cookie", "")
        check("POST /admin/login admin -> 303 + HttpOnly cookie",
              r.status_code in (302, 303) and "netops_token=" in cookie_hdr
              and "HttpOnly" in cookie_hdr,
              f"got {r.status_code}; cookie={cookie_hdr[:80]}")

        # (d) dashboard с cookie
        r = client.get("/admin/")
        admin_role = None
        with SessionLocal() as db:
            admin = db.query(User).filter_by(role="admin").first()
            admin_role = admin.role.value if admin else None
        check("GET /admin/ с cookie -> 200, имя/роль",
              r.status_code == 200 and "admin" in r.text.lower()
              and (admin_role or "admin") in r.text,
              f"got {r.status_code}")

        # (e) аудит доступен админу
        r = client.get("/admin/audit")
        check("GET /admin/audit (admin) -> 200",
              r.status_code == 200 and "Аудит" in r.text,
              f"got {r.status_code}")

        # (i) старый API не сломан: /api/auth/me по заголовку
        api_login = client.post("/api/auth/login",
                                json={"username": "admin@mock.local", "password": "x"})
        check("POST /api/auth/login -> 200 (старый SPA)",
              api_login.status_code == 200, f"got {api_login.status_code}")
        if api_login.status_code == 200:
            api_token = api_login.json().get("token")
            r = client.get("/api/auth/me",
                           headers={"Authorization": f"Bearer {api_token}"})
            check("GET /api/auth/me с Bearer -> 200",
                  r.status_code == 200, f"got {r.status_code}")

        # (j) статика htmx отдаётся
        r = client.get("/admin/static/js/htmx.min.js")
        check("GET /admin/static/js/htmx.min.js -> 200",
              r.status_code == 200 and len(r.content) > 1000,
              f"got {r.status_code}, {len(r.content)} bytes")

        # создаем viewer и проверяем RBAC на страницах
        with SessionLocal() as db:
            viewer = User(username="view1@mock.local", display_name="Вьюер",
                          role="viewer", is_active=True, granted_by="test")
            db.add(viewer)
            db.commit()
            db.refresh(viewer)
            viewer_id = viewer.id

        # Логин viewer через dev: bootstrap-админ пропускается только для
        # NETOPS_BOOTSTRAP_ADMIN, поэтому для viewer используем его cookie,
        # выпустив токен напрямую (как сделала бы форма после LDAP).
        from app.auth.jwt_utils import create_token
        viewer_token = create_token(viewer_id, "view1@mock.local", "viewer")
        client.cookies.set("netops_token", viewer_token)

        # dashboard для viewer: минимум (только чат), без инвентаря/аудита
        r = client.get("/admin/")
        check("GET /admin/ (viewer) -> 200, без ссылок на инвентарь/аудит",
              r.status_code == 200 and "/admin/inventory" not in r.text
              and "/admin/audit" not in r.text)

        # (g) viewer -> инвентарь 403
        r = client.get("/admin/inventory")
        check("GET /admin/inventory (viewer) -> 403",
              r.status_code == 403 and "403" in r.text,
              f"got {r.status_code}")

        # (f) viewer -> аудит 403
        r = client.get("/admin/audit")
        check("GET /admin/audit (viewer) -> 403",
              r.status_code == 403 and "403" in r.text,
              f"got {r.status_code}")

        # (h) logout: сервер помечает cookie истекшей (в браузере она
        # удаляется; httpx-банка в TestClient имеет известный баг с
        # quoted-value, поэтому проверяем Set-Cookie и поведение чистого клиента)
        client.cookies.set("netops_token", viewer_token)
        r = client.post("/admin/logout", follow_redirects=False)
        cookie_hdr = r.headers.get("set-cookie", "")
        check("POST /admin/logout -> 303, cookie удалена",
              r.status_code in (302, 303)
              and ('netops_token=""' in cookie_hdr or "max-age=0" in cookie_hdr.lower()),
              f"got {r.status_code}; {cookie_hdr[:80]}")
        fresh = TestClient(app)
        r = fresh.get("/admin/", follow_redirects=False)
        check("без cookie /admin/ -> редирект на логин (после logout)",
              r.status_code in (302, 303)
              and "/admin/login" in r.headers.get("location", ""))

        # engineer: доступ к инвентарю/диалогам, но не к аудиту
        with SessionLocal() as db:
            eng = User(username="eng1@mock.local", display_name="Инженер",
                       role="engineer", is_active=True, granted_by="test")
            db.add(eng)
            db.commit()
            db.refresh(eng)
            eng_id = eng.id
        eng_token = create_token(eng_id, "eng1@mock.local", "engineer")
        client.cookies.set("netops_token", eng_token)
        r = client.get("/admin/inventory")
        check("GET /admin/inventory (engineer) -> 200",
              r.status_code == 200, f"got {r.status_code}")
        r = client.get("/admin/conversations")
        check("GET /admin/conversations (engineer) -> 200",
              r.status_code == 200, f"got {r.status_code}")
        r = client.get("/admin/audit")
        check("GET /admin/audit (engineer) -> 403",
              r.status_code == 403, f"got {r.status_code}")

    print()
    print(f"Итог: PASS={PASS} FAIL={FAIL}")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
