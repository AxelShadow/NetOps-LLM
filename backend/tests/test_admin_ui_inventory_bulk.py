"""Интеграционный тест массового вкл/выкл устройств /admin/inventory/bulk
(паритет старого SPA с PATCH /api/devices/bulk).

Стиль test_admin_ui_inventory.py: TestClient на временной sqlite (env
задаётся ДО импорта app), dev-логин bootstrap-админа, устройства —
напрямую в БД. Рабочая netops.db не затрагивается.

Запуск: .venv/Scripts/python.exe tests/test_admin_ui_inventory_bulk.py  (из backend/)
"""
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

# Кириллица в консоли Windows: cp1251 ломает и вывод, и «Итог»-строку
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TMP = tempfile.mkdtemp(prefix="netops_invbulk_")
os.environ["NETOPS_DATABASE_URL"] = f"sqlite:///{TMP}/invbulk_test.db".replace("\\", "/")
os.environ["NETOPS_MOCK_MODE"] = "true"
os.environ["NETOPS_DEV_MODE"] = "true"
os.environ["NETOPS_AD_DOMAIN"] = "mock.local"
os.environ.setdefault("NETOPS_BOOTSTRAP_ADMIN", "admin@mock.local")
os.environ.setdefault("NETOPS_JWT_SECRET", "invbulk-test-secret")
os.environ["NETOPS_LLM_BASE_URL"] = "http://localhost:1234/v1"
os.environ.pop("NETOPS_ZABBIX_URL", None)
os.environ.pop("NETOPS_ZABBIX_TOKEN", None)
os.environ["NETOPS_ZABBIX_URL"] = ""   # перекрывает backend/.env (если есть)
os.environ["NETOPS_ZABBIX_TOKEN"] = ""

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.auth.jwt_utils import create_token  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import Device, User  # noqa: E402
import app.devices.vmware as vmw_mod  # noqa: E402  (FIX-03: кэш сессий)

PASS, FAIL = 0, 0


def check(name: str, ok: bool, detail: str = ""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"PASS: {name}")
    else:
        FAIL += 1
        print(f"FAIL: {name} {detail}")


def seed() -> tuple[int, int, int, int]:
    """3 устройства + viewer; возвращает id устройств и viewer."""
    with SessionLocal() as db:
        viewer = User(username="view1@mock.local", display_name="Вьюер",
                      role="viewer", is_active=True, granted_by="test")
        db.add(viewer)
        db.flush()
        ids = []
        for i in (1, 2, 3):
            d = Device(name=f"bulk-{i}", type="eltex", host=f"10.3.0.{i}",
                       port=22, username="u", password="p", enabled=True,
                       source="manual")
            db.add(d)
            db.flush()
            ids.append(d.id)
        db.commit()
        return ids[0], ids[1], ids[2], viewer.id


def main():
    with TestClient(app) as client:
        d1, d2, d3, viewer_id = seed()  # после lifespan (create_all)

        # --- RBAC ---
        r = client.post("/admin/inventory/bulk",
                        data={"ids": [str(d1)], "enabled": "off"},
                        follow_redirects=False)
        check("POST /admin/inventory/bulk без cookie -> редирект на логин",
              r.status_code in (302, 303)
              and "/admin/login" in r.headers.get("location", ""),
              f"got {r.status_code}, loc={r.headers.get('location')!r}")

        r = client.post("/admin/login",
                        data={"username": "admin@mock.local", "password": "x"},
                        follow_redirects=False)
        check("POST /admin/login admin -> 303",
              r.status_code in (302, 303), f"got {r.status_code}")
        admin_token = r.cookies.get("netops_token")

        client.cookies.set("netops_token",
                           create_token(viewer_id, "view1@mock.local", "viewer"))
        r = client.post("/admin/inventory/bulk",
                        data={"ids": [str(d1)], "enabled": "off"},
                        follow_redirects=False)
        check("POST bulk (viewer) -> 403",
              r.status_code == 403, f"got {r.status_code}")

        client.cookies.set("netops_token", admin_token)

        # --- Страница и таблица с чекбоксами (admin) ---
        r = client.get("/admin/inventory")
        check("GET /admin/inventory (admin) -> панель массовых действий",
              r.status_code == 200 and "Выбрано:" in r.text
              and "Включить выбранные" in r.text
              and "Выключить выбранные" in r.text,
              f"got {r.status_code}")

        r = client.get("/admin/inventory/partial/table")
        check("partial/table -> чекбоксы в строках и «выбрать все»",
              r.status_code == 200 and 'class="device-check"' in r.text
              and 'id="select-all"' in r.text,
              f"got {r.status_code}")
        check("partial/table: name=ids на чекбоксах (форма соберёт список)",
              'name="ids"' in r.text, "")

        # --- Валидация ---
        r = client.post("/admin/inventory/bulk",
                        data={"ids": "", "enabled": "off"},
                        follow_redirects=False)
        check("POST bulk пустые ids -> 400",
              r.status_code == 400, f"got {r.status_code}")

        r = client.post("/admin/inventory/bulk",
                        data={"ids": ["", "  "], "enabled": "off"},
                        follow_redirects=False)
        check("POST bulk только пустые значения -> 400 «Выберите устройства»",
              r.status_code == 400, f"got {r.status_code}")

        r = client.post("/admin/inventory/bulk",
                        data={"ids": [str(d1), "abc"], "enabled": "off"},
                        follow_redirects=False)
        check("POST bulk мусорный id -> 400",
              r.status_code == 400, f"got {r.status_code}")

        r = client.post("/admin/inventory/bulk",
                        data={"ids": [str(d1)], "enabled": "maybe"},
                        follow_redirects=False)
        check("POST bulk enabled=maybe -> 400",
              r.status_code == 400, f"got {r.status_code}")

        # --- Массовое выключение (строка «1,3» + многократное поле) ---
        r = client.post("/admin/inventory/bulk",
                        data={"ids": f"{d1},{d2}", "enabled": "off"},
                        follow_redirects=False)
        check("POST bulk off (строка «id1,id2») -> 303 redirect",
              r.status_code == 303
              and "/admin/inventory" in r.headers.get("location", ""),
              f"got {r.status_code}, loc={r.headers.get('location')}")
        loc = r.headers.get("location", "")
        flash_msg = unquote(loc.split("flash=")[-1]) if "flash=" in loc else ""
        check("redirect содержит flash «Обновлено 2 устройства»",
              "Обновлено 2 устройства" in flash_msg,
              f"loc={loc}")

        with SessionLocal() as db:
            states = {d.id: d.enabled for d in
                      db.query(Device).filter(Device.id.in_([d1, d2, d3]))}
        check("БД: устройства 1,2 выключены, 3 нетронут",
              states == {d1: False, d2: False, d3: True},
              f"states={states}")

        # GET по редиректу: flash отображается
        r = client.get("/admin/inventory", params={"flash": "Обновлено 2 устройства"})
        check("GET /admin/inventory?flash= -> flash на странице",
              r.status_code == 200 and "Обновлено 2 устройства" in r.text,
              f"got {r.status_code}")

        # --- Массовое включение (многократное поле ids) ---
        r = client.post("/admin/inventory/bulk",
                        data={"ids": [str(d1), str(d2)], "enabled": "on"},
                        follow_redirects=False)
        check("POST bulk on (многократное ids) -> 303 redirect",
              r.status_code == 303, f"got {r.status_code}")
        with SessionLocal() as db:
            states = {d.id: d.enabled for d in
                      db.query(Device).filter(Device.id.in_([d1, d2, d3]))}
        check("БД: устройства 1,2 снова включены (3 всё ещё True)",
              states == {d1: True, d2: True, d3: True},
              f"states={states}")

        # --- Несуществующие id: 303 + «не найдены», без изменений ---
        r = client.post("/admin/inventory/bulk",
                        data={"ids": ["9999"], "enabled": "off"},
                        follow_redirects=False)
        check("POST bulk несуществующий id -> 303 (обновлено 0)",
              r.status_code == 303, f"got {r.status_code}")
        loc = r.headers.get("location", "")
        flash_msg = unquote(loc.split("flash=")[-1]) if "flash=" in loc else ""
        check("несуществующий id: flash «Устройства не найдены»",
              "Устройства не найдены" in flash_msg, f"loc={loc}")

        # --- FIX-03: сброс кэша VMware после bulk ---
        vmw_mod._adapters["10.3.0.1:22:u"] = object()  # stale-сессия
        r = client.post("/admin/inventory/bulk",
                        data={"ids": [str(d1)], "enabled": "off"},
                        follow_redirects=False)
        check("FIX-03: bulk -> 303, кэш _adapters сброшен",
              r.status_code == 303 and not vmw_mod._adapters,
              f"status={r.status_code}, cache={dict(vmw_mod._adapters)!r}")
        vmw_mod._adapters.clear()

        # --- PATCH /api/devices/bulk не сломан ---
        r = client.patch("/api/devices/bulk",
                         json={"ids": [d1], "enabled": True},
                         headers={"Authorization": f"Bearer {admin_token}"})
        check("PATCH /api/devices/bulk не сломан",
              r.status_code == 200, f"got {r.status_code}")

    print()
    print(f"Итог: PASS={PASS} FAIL={FAIL}")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
