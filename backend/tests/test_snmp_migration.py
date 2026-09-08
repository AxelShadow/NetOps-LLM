"""Авто-миграция SNMP-колонок devices + CRUD-форма (этап SNMP, PLAN.md §9).

Стиль test_admin_ui_inventory.py / test_agent_tools_rbac.py: временная
sqlite, env задаётся ДО импорта app.*, рабочая netops.db не затрагивается.
Покрывает:
  1) свежая БД: create_all создаёт колонки, SELECT возвращает дефолты;
  2) _ensure_device_columns идемпотентен (повтор — не падает);
  3) «старая» БД без SNMP-колонок: миграция добавляет, данные на месте,
     DEFAULT применяется к существующим строкам;
  4) POST /admin/inventory создаёт printer с snmp_community из формы;
     GET-форма содержит SNMP-секцию;
  5) DeviceType.printer попадает в _INV_DEVICE_TYPES (select формы).

Запуск: .venv/Scripts/python.exe tests/test_snmp_migration.py  (из backend/)
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

# Windows-консоль по умолчанию cp1251: кириллица в PASS/FAIL-строках
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TMP = tempfile.mkdtemp(prefix="netops_snmp_")
os.environ["NETOPS_DATABASE_URL"] = f"sqlite:///{TMP}/snmp_test.db".replace("\\", "/")
os.environ["NETOPS_MOCK_MODE"] = "true"
os.environ["NETOPS_DEV_MODE"] = "true"
os.environ["NETOPS_AD_DOMAIN"] = "mock.local"
os.environ.setdefault("NETOPS_BOOTSTRAP_ADMIN", "admin@mock.local")
os.environ.setdefault("NETOPS_JWT_SECRET", "snmp-test-secret")
os.environ["NETOPS_LLM_BASE_URL"] = "http://127.0.0.1:9/v1"  # ничего не слушает
os.environ["NETOPS_INTERNAL_SERVICE_TOKEN"] = "internal-test-token"
os.environ.pop("NETOPS_ZABBIX_URL", None)
os.environ.pop("NETOPS_ZABBIX_TOKEN", None)
os.environ["NETOPS_ZABBIX_URL"] = ""     # перекрывает backend/.env (если есть)
os.environ["NETOPS_ZABBIX_TOKEN"] = ""

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

from app.main import _ensure_device_columns  # noqa: E402
from app.models import Device, DeviceType  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
import app.main  # noqa: E402  (lifespan с _ensure_device_columns)

PASS, FAIL = 0, 0


def check(name: str, ok: bool, detail: str = ""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"PASS: {name}")
    else:
        FAIL += 1
        print(f"FAIL: {name} {detail}")


def _columns(eng) -> set:
    """Имена колонок devices через PRAGMA (sqlite) — независимая проверка."""
    from sqlalchemy import inspect
    return {c["name"] for c in inspect(eng).get_columns("devices")}


def scenario_fresh_db():
    """1) Свежая БД: create_all уже создаёт SNMP-колонки с дефолтами."""
    Base.metadata.create_all(engine)
    cols = _columns(engine)
    check("свежая БД: колонки snmp_version/snmp_community существуют",
          {"snmp_version", "snmp_community"} <= cols,
          f"cols={sorted(cols)}")
    with engine.connect() as conn:
        conn.execute(text(
            "INSERT INTO devices (name, type, host, port, username, "
            "password, enabled, description, source, group_name) "
            "VALUES ('fresh-rtr', 'eltex', '10.0.0.1', 22, 'u', 'p', 1, "
            "'', 'manual', '')"))
        conn.commit()
        ver, comm = conn.execute(text(
            "SELECT snmp_version, snmp_community FROM devices "
            "WHERE name = 'fresh-rtr'")).fetchone()
    check("свежая БД: raw-INSERT без SNMP-полей получает дефолты 2c/public",
          ver == "2c" and comm == "public", f"got {ver}/{comm}")


def scenario_idempotent():
    """2) Повторный вызов _ensure_device_columns не падает."""
    try:
        _ensure_device_columns()
        _ensure_device_columns()   # второй раз: колонки уже есть
        check("_ensure_device_columns идемпотентен (повтор — не падает)",
              True)
    except Exception as e:
        check("_ensure_device_columns идемпотентен (повтор — не падает)",
              False, f"exception: {e}")


def scenario_old_db():
    """3) «Старая» БД: devices БЕЗ SNMP-колонок → миграция добавляет."""
    # Отдельный движок: «прод» образца прошлой версии (мигрировать на нём
    # глобальный engine нельзя — таблица уже создана create_all с новыми
    # колонками).
    old_url = f"sqlite:///{TMP}/old_dev.db".replace("\\", "/")
    old = create_engine(old_url)
    with old.connect() as conn:
        # DDL по модели минус SNMP-поля (как в старой версии models.py)
        conn.execute(text(
            "CREATE TABLE devices (id INTEGER NOT NULL PRIMARY KEY, "
            "name VARCHAR(64) NOT NULL, type VARCHAR(8) NOT NULL, "
            "host VARCHAR(128) NOT NULL, port INTEGER NOT NULL, "
            "username VARCHAR(64) NOT NULL, password VARCHAR(128) NOT NULL, "
            "enabled BOOLEAN NOT NULL, description VARCHAR(256) NOT NULL, "
            "source VARCHAR(16) NOT NULL, zabbix_hostid VARCHAR(32), "
            "group_name VARCHAR(128) NOT NULL, "
            "UNIQUE (name), UNIQUE (zabbix_hostid))"))
        # Строка ДО миграции: её данные должны пережить ALTER TABLE
        conn.execute(text(
            "INSERT INTO devices (name, type, host, port, username, "
            "password, enabled, description, source, group_name) "
            "VALUES ('legacy-rtr', 'eltex', '10.10.10.1', 22, 'op', "
            "'secret', 1, 'Ядро сети', 'manual', 'Сеть')"))
        conn.commit()
    before = _columns(old)
    check("старая БД: колонок snmp_* нет до миграции",
          not {"snmp_version", "snmp_community"} & before,
          f"cols={sorted(before)}")

    _ensure_device_columns(old)     # eng-параметр для отдельного движка

    after = _columns(old)
    check("старая БД: миграция добавила snmp_version/snmp_community",
          {"snmp_version", "snmp_community"} <= after,
          f"cols={sorted(after)}")
    with old.connect() as conn:
        name, ver, comm = conn.execute(text(
            "SELECT name, snmp_version, snmp_community FROM devices "
            "WHERE name = 'legacy-rtr'")).fetchone()
        n = conn.execute(text("SELECT COUNT(*) FROM devices")).scalar()
    check("старая БД: данные на месте, DEFAULT применился к старой строке",
          name == "legacy-rtr" and ver == "2c" and comm == "public"
          and n == 1, f"got {name}/{ver}/{comm}/count={n}")
    # Повтор на «старой» — тоже идемпотентен
    _ensure_device_columns(old)
    check("старая БД: повтор миграции идемпотентен",
          {"snmp_version", "snmp_community"} <= _columns(old))
    old.dispose()


def scenario_crud_form():
    """4) Форма: POST создаёт printer с SNMP-полями; GET-форма имеет секцию."""
    with TestClient(app.main.app) as client:
        # lifespan прогнал create_all + все _ensure_* на тестовой sqlite
        r = client.post("/admin/login",
                        data={"username": "admin@mock.local",
                              "password": "x"},
                        follow_redirects=False)
        check("POST /admin/login admin (DEV_MODE) -> 303",
              r.status_code in (302, 303), f"got {r.status_code}")

        # GET-форма: SNMP-секция присутствует
        r = client.get("/admin/inventory/new")
        check("GET /admin/inventory/new -> 200, SNMP-секция в HTML",
              r.status_code == 200 and 'name="snmp_version"' in r.text
              and 'name="snmp_community"' in r.text,
              f"got {r.status_code}")
        check("GET-форма: тип printer в select",
              'value="printer"' in r.text, "")

        # POST: принтер с кастомным community
        r = client.post("/admin/inventory",
                        data={"name": "hq-printer-1", "type": "printer",
                              "host": "192.0.2.150", "port": "161",
                              "username": "", "password": "",
                              "snmp_version": "2c",
                              "snmp_community": "office-sec",
                              "enabled": "on", "group": "Принтеры"})
        check("POST /admin/inventory printer -> 200 (таблица в ответе)",
              r.status_code == 200 and "hq-printer-1" in r.text
              and "Устройство добавлено" in r.text,
              f"got {r.status_code}")
        with SessionLocal() as db:
            d = db.query(Device).filter_by(name="hq-printer-1").first()
            check("printer в БД: type=printer, snmp_community=office-sec",
                  d is not None and d.type == DeviceType.printer
                  and d.snmp_community == "office-sec"
                  and d.snmp_version == "2c",
                  f"got {d and (d.type, d.snmp_version, d.snmp_community)}")

        # PUT-редактирование: пустой community не должен затирать — нет,
        # по ТЗ пусто = дефолт public. Проверяем апдейт значения.
        r = client.put(f"/admin/inventory/{d.id}",
                       data={"name": "hq-printer-1", "type": "printer",
                             "host": "192.0.2.150", "port": "161",
                             "snmp_version": "2c",
                             "snmp_community": "changed-comm",
                             "enabled": "on"})
        check("PUT printer -> 200, flash «обновлено»",
              r.status_code == 200 and "Устройство обновлено" in r.text,
              f"got {r.status_code}")
        with SessionLocal() as db:
            d_upd = db.query(Device).filter_by(name="hq-printer-1").first()
            check("PUT: snmp_community обновился в БД",
                  d_upd is not None and d_upd.snmp_community == "changed-comm",
                  f"got {d_upd and d_upd.snmp_community}")

        # POST без SNMP-полей (старый клиент/минимальная форма) → дефолты
        r = client.post("/admin/inventory",
                        data={"name": "bare-sw", "type": "mikrotik",
                              "host": "10.7.0.9", "port": "22"})
        with SessionLocal() as db:
            d2 = db.query(Device).filter_by(name="bare-sw").first()
            check("POST без SNMP-полей -> дефолты 2c/public",
                  d2 is not None and d2.snmp_version == "2c"
                  and d2.snmp_community == "public",
                  f"got {d2 and (d2.snmp_version, d2.snmp_community)}")


def scenario_router_types():
    """5) DeviceType.printer есть в _INV_DEVICE_TYPES роутера админки."""
    from app.ui.router import _INV_DEVICE_TYPES
    check("DeviceType.printer в _INV_DEVICE_TYPES",
          "printer" in _INV_DEVICE_TYPES,
          f"types={_INV_DEVICE_TYPES}")


def main():
    scenario_fresh_db()
    scenario_idempotent()
    scenario_old_db()
    scenario_crud_form()
    scenario_router_types()

    print()
    print(f"Итог: PASS={PASS} FAIL={FAIL}")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
