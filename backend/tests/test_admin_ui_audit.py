"""Интеграционный тест страницы аудита /admin/audit (Фаза 4 миграции).

Стиль admin_ui_test.py: TestClient на временной sqlite (env задаётся ДО
импорта app), dev-логин bootstrap-админа, записи аудита — напрямую в БД.
Рабочая netops.db не затрагивается.

Запуск: .venv/Scripts/python.exe tests/test_admin_ui_audit.py  (из backend/)
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

TMP = tempfile.mkdtemp(prefix="netops_audit_")
os.environ["NETOPS_DATABASE_URL"] = f"sqlite:///{TMP}/audit_test.db".replace("\\", "/")
os.environ["NETOPS_MOCK_MODE"] = "true"
os.environ["NETOPS_DEV_MODE"] = "true"
os.environ["NETOPS_AD_DOMAIN"] = "mock.local"
os.environ.setdefault("NETOPS_BOOTSTRAP_ADMIN", "admin@mock.local")
os.environ.setdefault("NETOPS_JWT_SECRET", "audit-test-secret")
os.environ["NETOPS_LLM_BASE_URL"] = "http://localhost:1234/v1"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.auth.jwt_utils import create_token  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import AuditLog, User  # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, ok: bool, detail: str = ""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"PASS: {name}")
    else:
        FAIL += 1
        print(f"FAIL: {name} {detail}")


def seed_audit():
    """Создаёт пользователей и записи аудита, возвращает id записи с длинным result."""
    with SessionLocal() as db:
        viewer = User(username="view1@mock.local", display_name="Вьюер",
                     role="viewer", is_active=True, granted_by="test")
        db.add(viewer)
        db.flush()
        viewer_id = viewer.id

        long_result = "R" * 1000  # > 800 символов: REST-версия обрезает, UI — нет
        big_ids = []
        for i in range(30):  # > 25 для пагинации
            db.add(AuditLog(
                user_id=viewer_id, conversation_id=100 + i // 15,
                tool="check_ping" if i % 2 else "get_uptime",
                arguments='{"host": "srv-' + str(i) + '"}',
                result=long_result if i == 0 else f"результат {i}",
                status="ok" if i % 3 else "error",
                duration_ms=1500 if i == 0 else (None if i == 1 else 123),
                created_at=datetime.utcnow() + timedelta(minutes=i),
            ))
        db.flush()
        first = db.query(AuditLog).filter_by(result=long_result).first()
        big_ids = [r.id for r in db.query(AuditLog).order_by(AuditLog.id).limit(3)]
        db.commit()
        return viewer_id, first.id, big_ids


def main():
    with TestClient(app) as client:
        viewer_id, long_id, _ = seed_audit()  # после lifespan (create_all)
        # --- RBAC ---
        r = client.get("/admin/audit", follow_redirects=False)
        check("GET /admin/audit без cookie -> редирект на логин",
              r.status_code in (302, 303)
              and "/admin/login" in r.headers.get("location", ""),
              f"got {r.status_code}")

        r = client.post("/admin/login",
                        data={"username": "admin@mock.local", "password": "x"},
                        follow_redirects=False)
        check("POST /admin/login admin -> 303",
              r.status_code in (302, 303), f"got {r.status_code}")
        admin_token = r.cookies.get("netops_token")

        from app.auth.jwt_utils import create_token
        client.cookies.set("netops_token",
                           create_token(viewer_id, "view1@mock.local", "viewer"))
        for path in ("/admin/audit", "/admin/audit/partial/table",
                     f"/admin/audit/{long_id}/details"):
            r = client.get(path, follow_redirects=False)
            check(f"GET {path} (viewer) -> 403 или редирект",
                  r.status_code == 403
                  or (r.status_code in (302, 303)
                      and "/admin/login" in r.headers.get("location", "")),
                  f"got {r.status_code}")
        # Возврат в сессию admin: перезаписываем cookie напрямую
        client.cookies.set("netops_token", admin_token)

        r = client.get("/admin/audit")
        check("GET /admin/audit (admin) -> 200, заголовок",
              r.status_code == 200 and "Аудит" in r.text, f"got {r.status_code}")

        # --- Таблица: фильтры ---
        r = client.get("/admin/audit/partial/table")
        check("partial/table без фильтров -> 200, счётчик 30",
              r.status_code == 200 and "Всего: 30" in r.text, f"got {r.status_code}")

        r = client.get("/admin/audit/partial/table",
                       params={"tool": "check_ping"})
        check("фильтр по инструменту: только check_ping",
              r.status_code == 200 and "get_uptime" not in r.text
              and "check_ping" in r.text, f"got {r.status_code}")

        r = client.get("/admin/audit/partial/table",
                       params={"user": "view1"})
        check("фильтр по пользователю (view1)",
              r.status_code == 200 and "view1@mock.local" in r.text,
              f"got {r.status_code}")

        r = client.get("/admin/audit/partial/table",
                       params={"user": "nosuch"})
        check("фильтр по пользователю: нет совпадений",
              r.status_code == 200 and "Записей нет" in r.text,
              f"got {r.status_code}")

        r = client.get("/admin/audit/partial/table",
                       params={"status": "error"})
        check("фильтр по статусу error",
              r.status_code == 200
              and "Всего: 10" in r.text  # i % 3 == 0 из 30
              , f"got {r.status_code}")

        r = client.get("/admin/audit/partial/table",
                       params={"dialog": 100})
        check("фильтр по диалогу (100)",
              r.status_code == 200 and "Всего: 15" in r.text,
              f"got {r.status_code}")

        # --- Пагинация ---
        r = client.get("/admin/audit/partial/table", params={"page": 2})
        check("пагинация: page=2, «из 2»",
              r.status_code == 200 and "Страница 2 из 2" in r.text,
              f"got {r.status_code}")
        r = client.get("/admin/audit/partial/table", params={"page": 99})
        check("пагинация: page=99 клампится в диапазон",
              r.status_code == 200 and "Страница 2 из 2" in r.text,
              f"got {r.status_code}")

        # --- Длительность ---
        # Запись с duration_ms=1500 — самая старая (id минимален), попадает
        # на стр.2 без фильтров, поэтому ищем её через фильтр по диалогу 100
        r = client.get("/admin/audit/partial/table", params={"dialog": 100})
        check("длительность 1500 мс -> «1.5 с», null -> «—»",
              "1.5 с" in r.text and "—" in r.text, "")

        # --- Детали: полное содержимое, без обрезки ---
        r = client.get(f"/admin/audit/{long_id}/details")
        check("детали -> 200, полный result (>800 симв., без обрезки)",
              r.status_code == 200 and "R" * 900 in r.text,
              f"got {r.status_code}, len={len(r.text)}")
        check("детали: полные arguments",
              'srv-0' in r.text and "Аргументы" in r.text, "")
        check("детали: мета (инструмент, статус, длительность)",
              "get_uptime" in r.text and "1.5 с" in r.text, "")

        r = client.get("/admin/audit/999999/details")
        check("детали: 404 для несуществующей записи",
              r.status_code == 404, f"got {r.status_code}")

        # --- Фильтры по датам ---
        r = client.get("/admin/audit/partial/table",
                       params={"date_from": "2000-01-01",
                               "date_to": "2100-01-01"})
        check("даты-фильтры: глухие границы -> все записи",
              r.status_code == 200 and "Всего: 30" in r.text,
              f"got {r.status_code}")
        r = client.get("/admin/audit/partial/table",
                       params={"date_from": "2100-01-01"})
        check("даты-фильтры: будущий date_from -> пусто",
              r.status_code == 200 and "Записей нет" in r.text,
              f"got {r.status_code}")
        r = client.get("/admin/audit/partial/table",
                       params={"date_from": "битая-дата"})
        check("битые значения дат игнорируются",
              r.status_code == 200, f"got {r.status_code}")

        # --- Старый REST-эндпоинт не сломан ---
        r = client.get("/api/audit?limit=5",
                       headers={"Authorization": f"Bearer {admin_token}"})
        check("GET /api/audit не сломан",
              r.status_code == 200 and len(r.json()) == 5,
              f"got {r.status_code}")

    print()
    print(f"Итог: PASS={PASS} FAIL={FAIL}")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
