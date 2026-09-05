"""Интеграционный тест инвентаря /admin/inventory (Фаза 3 миграции).

Стиль test_admin_ui_audit.py: TestClient на временной sqlite (env задаётся
ДО импорта app), dev-логин bootstrap-админа, устройства — напрямую в БД.
Рабочая netops.db не затрагивается.

Запуск: .venv/Scripts/python.exe tests/test_admin_ui_inventory.py  (из backend/)
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

TMP = tempfile.mkdtemp(prefix="netops_inv_")
os.environ["NETOPS_DATABASE_URL"] = f"sqlite:///{TMP}/inv_test.db".replace("\\", "/")
os.environ["NETOPS_MOCK_MODE"] = "true"
os.environ["NETOPS_DEV_MODE"] = "true"
os.environ["NETOPS_AD_DOMAIN"] = "mock.local"
os.environ.setdefault("NETOPS_BOOTSTRAP_ADMIN", "admin@mock.local")
os.environ.setdefault("NETOPS_JWT_SECRET", "inv-test-secret")
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


def seed_devices() -> tuple[int, int, int, int]:
    """25 устройств (пагинация) + спец-устройства; возвращает id."""
    with SessionLocal() as db:
        eng = User(username="eng1@mock.local", display_name="Инженер",
                   role="engineer", is_active=True, granted_by="test")
        viewer = User(username="view1@mock.local", display_name="Вьюер",
                     role="viewer", is_active=True, granted_by="test")
        db.add_all([eng, viewer])
        db.flush()
        for i in range(25):
            db.add(Device(
                name=f"srv-{i:02d}", type="eltex", host=f"10.0.0.{i}",
                port=22, username="u", password="p", enabled=(i % 2 == 0),
                source="manual", group="grp-a" if i < 5 else "grp-b",
            ))
        zdev = Device(name="zbx-host", type="other", host="10.1.0.1", port=10050,
                      username="", password="", enabled=True, source="zabbix",
                      zabbix_hostid="1001")
        db.add(zdev)
        db.flush()
        zid = zdev.id
        keeper = Device(name="keep-pass", type="mikrotik", host="10.2.0.1",
                        port=22, username="u", password="secret-old",
                        enabled=True, source="manual")
        db.add(keeper)
        db.flush()
        kid = keeper.id
        db.commit()
        return eng.id, viewer.id, zid, kid


def main():
    with TestClient(app) as client:
        eng_id, viewer_id, zid, kid = seed_devices()  # после lifespan (create_all)

        # --- RBAC ---
        r = client.get("/admin/inventory", follow_redirects=False)
        check("GET /admin/inventory без cookie -> редирект на логин",
              r.status_code in (302, 303)
              and "/admin/login" in r.headers.get("location", ""),
              f"got {r.status_code}")

        r = client.post("/admin/login",
                        data={"username": "admin@mock.local", "password": "x"},
                        follow_redirects=False)
        check("POST /admin/login admin -> 303",
              r.status_code in (302, 303), f"got {r.status_code}")
        admin_token = r.cookies.get("netops_token")

        client.cookies.set("netops_token",
                           create_token(viewer_id, "view1@mock.local", "viewer"))
        r = client.get("/admin/inventory", follow_redirects=False)
        check("GET /admin/inventory (viewer) -> 403 или редирект",
              r.status_code == 403
              or (r.status_code in (302, 303)
                  and "/admin/login" in r.headers.get("location", "")),
              f"got {r.status_code}")

        client.cookies.set("netops_token",
                           create_token(eng_id, "eng1@mock.local", "engineer"))
        r = client.get("/admin/inventory")
        check("GET /admin/inventory (engineer) -> 200",
              r.status_code == 200 and "Инвентарь" in r.text,
              f"got {r.status_code}")
        r = client.post("/admin/inventory",
                         data={"name": "eng-try", "type": "eltex", "host": "x"})
        check("POST /admin/inventory (engineer) -> 403",
              r.status_code == 403, f"got {r.status_code}")

        # Возврат в сессию admin
        client.cookies.set("netops_token", admin_token)

        # --- Страница и таблица ---
        r = client.get("/admin/inventory")
        check("GET /admin/inventory (admin) -> 200, заголовок",
              r.status_code == 200 and "Инвентарь" in r.text,
              f"got {r.status_code}")

        r = client.get("/admin/inventory/partial/table")
        check("partial/table без фильтров -> 200, счётчик 27",
              r.status_code == 200 and "Всего: 27" in r.text,
              f"got {r.status_code}")
        check("пароль не утекает в HTML", "secret-old" not in r.text, "")

        r = client.get("/admin/inventory/partial/table",
                       params={"q": "srv-0"})
        check("фильтр q=srv-0: только имена с «srv-0»",
              r.status_code == 200 and "srv-0" in r.text
              and "srv-1" not in r.text and "zbx-host" not in r.text,
              f"got {r.status_code}")

        r = client.get("/admin/inventory/partial/table",
                       params={"type": "mikrotik"})
        check("фильтр type=mikrotik",
              r.status_code == 200 and "keep-pass" in r.text
              and "srv-00" not in r.text, f"got {r.status_code}")

        r = client.get("/admin/inventory/partial/table",
                       params={"source": "zabbix"})
        check("фильтр source=zabbix",
              r.status_code == 200 and "zbx-host" in r.text
              and "keep-pass" not in r.text, f"got {r.status_code}")

        r = client.get("/admin/inventory/partial/table",
                       params={"status": "off"})
        check("фильтр status=off: только выключенные",
              r.status_code == 200 and "srv-01" in r.text
              and "srv-00" not in r.text, f"got {r.status_code}")

        r = client.get("/admin/inventory/partial/table",
                       params={"group": "grp-a"})
        check("фильтр group=grp-a",
              r.status_code == 200 and "Всего: 5" in r.text,
              f"got {r.status_code}")

        # --- Пагинация ---
        r = client.get("/admin/inventory/partial/table", params={"page": 2})
        check("пагинация: page=2, «из 2»",
              r.status_code == 200 and "Страница 2 из 2" in r.text,
              f"got {r.status_code}")
        r = client.get("/admin/inventory/partial/table", params={"page": 99})
        check("пагинация: page=99 клампится",
              r.status_code == 200 and "Страница 2 из 2" in r.text,
              f"got {r.status_code}")

        # --- Создание ---
        r = client.post("/admin/inventory",
                        data={"name": "new-device", "type": "eltex",
                              "host": "10.9.0.1", "port": "22",
                              "username": "op", "password": "np",
                              "enabled": "on", "group": "grp-c"})
        check("POST /admin/inventory: создано, таблица в ответе",
              r.status_code == 200 and "new-device" in r.text
              and "Устройство добавлено" in r.text,
              f"got {r.status_code}")
        with SessionLocal() as db:
            d = db.query(Device).filter_by(name="new-device").first()
            check("устройство в БД, пароль сохранён",
                  d is not None and d.password == "np", "")
            check("enabled из формы «on» -> True", d.enabled is True, "")

        r = client.post("/admin/inventory",
                        data={"name": "new-device", "type": "eltex",
                              "host": "x"})
        check("POST дубль имени -> 400 + текст ошибки в форме",
              r.status_code == 400 and "занято" in r.text,
              f"got {r.status_code}")

        r = client.post("/admin/inventory",
                        data={"name": "bad-type", "type": "фантастика",
                              "host": "x"})
        check("POST битый type -> 400/422",
              r.status_code in (400, 422), f"got {r.status_code}")

        # --- Редактирование ---
        r = client.put(f"/admin/inventory/{kid}",
                       data={"name": "keep-pass-2", "type": "mikrotik",
                             "host": "10.2.0.1", "port": "22",
                             "username": "u", "password": "",
                             "enabled": "on"})
        check("PUT: имя изменено, таблица в ответе",
              r.status_code == 200 and "keep-pass-2" in r.text
              and "Устройство обновлено" in r.text,
              f"got {r.status_code}")
        with SessionLocal() as db:
            d = db.get(Device, kid)
            check("PUT с пустым паролем: пароль не сброшен",
                  d.name == "keep-pass-2" and d.password == "secret-old",
                  f"name={d.name}, pwd={d.password}")

        r = client.put(f"/admin/inventory/{zid}",
                       data={"name": "zbx-renamed", "type": "other",
                             "host": "10.1.0.1", "enabled": "on"})
        check("PUT zabbix-устройства с новым именем -> 400",
              r.status_code == 400, f"got {r.status_code}")

        # toggle enabled для zabbix-устройства (единственно разрешённое)
        r = client.put(f"/admin/inventory/{zid}", data={"enabled": ""})
        check("PUT zabbix toggle enabled -> 200",
              r.status_code == 200, f"got {r.status_code}")

        # --- Формы ---
        r = client.get("/admin/inventory/new")
        check("GET new -> форма без значения пароля",
              r.status_code == 200 and 'type="password"' in r.text
              and "secret-old" not in r.text, f"got {r.status_code}")
        r = client.get(f"/admin/inventory/{kid}/edit")
        check("GET edit -> форма, пароль НЕ предзаполнен",
              r.status_code == 200 and "keep-pass-2" in r.text
              and "secret-old" not in r.text, f"got {r.status_code}")

        # --- Удаление ---
        r = client.delete(f"/admin/inventory/{kid}")
        body = r.text
        table_zone = body[body.find("<table"):] if "<table" in body else body
        check("DELETE -> таблица + flash",
              r.status_code == 200 and "Устройство удалено" in body
              and "keep-pass-2" not in table_zone, f"got {r.status_code}")
        with SessionLocal() as db:
            check("устройство удалено из БД",
                  db.get(Device, kid) is None, "")

        r = client.delete(f"/admin/inventory/{zid}")
        check("DELETE zabbix-устройства -> отказ (только синхронизацией)",
              r.status_code == 200 and "синхронизацией" in r.text,
              f"got {r.status_code}")

        # --- sync-zabbix без настроек ---
        r = client.post("/admin/inventory/sync-zabbix")
        check("sync-zabbix без настроек -> flash «не настроен» (не 500)",
              r.status_code == 200 and "не настроен" in r.text,
              f"got {r.status_code}")

        # --- FIX-03: инвалидация кэша VMware при create/update/delete ---
        # «Устройство» vCenter-типа создаётся как обычное manual; кэш
        # сессий общий, поэтому проверяем сам факт сброса _adapters
        # при любом изменении инвентаря (реальный vCenter в тесте не нужен).
        vmw_mod._adapters["10.9.0.9:443:u"] = object()   # stale-сессия
        r = client.post("/admin/inventory",
                        data={"name": "vc-01", "type": "other",
                              "host": "10.9.0.9", "port": "443",
                              "username": "u", "password": "p",
                              "enabled": "on"})
        # «vc-01» в таблицу page=1 не попадает (пагинация, 30 устройств),
        # поэтому успешность create проверяем flash + БД, а не таблицей
        with SessionLocal() as db:
            vc = db.query(Device).filter_by(name="vc-01").first()
            vc_id = vc.id if vc else None
        check("FIX-03: POST create -> 200, кэш _adapters сброшен",
              r.status_code == 200 and vc_id is not None
              and "Устройство добавлено" in r.text
              and not vmw_mod._adapters,
              f"status={r.status_code}, vc_id={vc_id}, "
              f"cache={dict(vmw_mod._adapters)!r}")

        vmw_mod._adapters["10.9.0.9:443:u"] = object()   # снова stale
        r = client.put(f"/admin/inventory/{vc_id}",
                       data={"name": "vc-01", "type": "other",
                             "host": "10.9.0.9", "port": "443",
                             "username": "u2", "password": "p2",
                             "enabled": "on"})
        check("FIX-03: PUT update -> 200, кэш _adapters сброшен",
              r.status_code == 200 and not vmw_mod._adapters,
              f"status={r.status_code}, cache={dict(vmw_mod._adapters)!r}")

        vmw_mod._adapters["10.9.0.9:443:u2"] = object()  # stale перед DELETE
        r = client.delete(f"/admin/inventory/{vc_id}")
        check("FIX-03: DELETE -> 200, кэш _adapters сброшен (хелпер)",
              r.status_code == 200 and not vmw_mod._adapters,
              f"status={r.status_code}, cache={dict(vmw_mod._adapters)!r}")

        # upsert не прошёл (дубль существующего имени) -> кэш НЕ трогаем
        vmw_mod._adapters["x:443:u"] = object()
        r = client.post("/admin/inventory",
                        data={"name": "new-device", "type": "other",
                              "host": "y"})
        check("FIX-03: дубль имени (upsert fail) -> кэш НЕ сброшен",
              r.status_code == 400 and len(vmw_mod._adapters) == 1,
              f"status={r.status_code}, cache={dict(vmw_mod._adapters)!r}")
        vmw_mod._adapters.clear()

        # --- Существующий REST API не сломан ---
        r = client.get("/api/devices",
                       headers={"Authorization": f"Bearer {admin_token}"})
        check("GET /api/devices не сломан",
              r.status_code == 200, f"got {r.status_code}")

    print()
    print(f"Итог: PASS={PASS} FAIL={FAIL}")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
