"""Отчёт по страницам принтеров (Этап 20): get_printers_pages_report.

Проверяет:
  1) Регистрация: TOOLS_SCHEMA, схема без параметров, TTL=120,
     is_composite, viewer;
  2) Мок: summary (моно/цветной/total_pages), cartridges CMYK,
     unreachable, длина < MAX_RESULT;
  3) Реальная логика (мок off + подмена _snmp_walk): моно-принтер
     (color=False, cartridges пуст), цветной CMYK (roles 3,1,1,1 —
     black + 3 color; percent расчёт), total_pages сумма;
  4) Цветной фолбэк: нет roles-таблицы, но 2 supplies -> color=True;
  5) Недоступный -> unreachable, остальные целы;
  6) Бюджет: 130 цветных с картриджами -> JSON валиден < MAX_RESULT,
     часть уходит в remaining (деградация cartridges -> remaining);
  7) Пустой инвентарь -> Exception; eltex не попадает (фильтр типа).

Стиль test_printers_report.py: temp-sqlite env ДО импорта, PASS/FAIL,
подмена T._snmp_walk + T.settings.mock_mode + чистка T._caches.

Запуск: .venv/Scripts/python.exe tests/test_printers_pages_report.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TMP = tempfile.mkdtemp(prefix="netops_ppages_")
os.environ["NETOPS_DATABASE_URL"] = f"sqlite:///{TMP}/ppages_test.db".replace("\\", "/")
os.environ["NETOPS_MOCK_MODE"] = "true"
os.environ["NETOPS_DEV_MODE"] = "true"
os.environ["NETOPS_AD_DOMAIN"] = "mock.local"
os.environ.setdefault("NETOPS_BOOTSTRAP_ADMIN", "admin@mock.local")
os.environ.setdefault("NETOPS_JWT_SECRET", "ppages-test-secret")
os.environ["NETOPS_LLM_BASE_URL"] = "http://127.0.0.1:9/v1"
os.environ["NETOPS_INTERNAL_SERVICE_TOKEN"] = "test-token"
os.environ["NETOPS_ZABBIX_URL"] = ""
os.environ["NETOPS_ZABBIX_TOKEN"] = ""

from app.agent.tools import (  # noqa: E402
    TOOLS_SCHEMA, _registry, execute_tool, MAX_RESULT,
)
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import Device, DeviceType  # noqa: E402
import app.agent.tools as T  # noqa: E402

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


def _add_printers(names_hosts, enabled=True):
    with SessionLocal() as db:
        for name, host in names_hosts:
            db.add(Device(name=name, type=DeviceType.printer, host=host,
                          port=161, username="", password="",
                          description="", group="Принтеры",
                          enabled=enabled, snmp_version="2c",
                          snmp_community="public"))
        db.commit()


def _clear_devices():
    """Сценарии независимы: каждый сидит свой набор устройств."""
    with SessionLocal() as db:
        db.query(Device).delete()
        db.commit()


def _with_real_logic(fake_walk, cache_name="get_printers_pages_report"):
    """Контекст: мок off + подмена walk + чистый кэш инструмента."""
    old_walk, old_mock = T._snmp_walk, T.settings.mock_mode
    T._snmp_walk = fake_walk
    T.settings.mock_mode = False
    if cache_name in T._caches:
        T._caches[cache_name].clear()

    def restore():
        T._snmp_walk = old_walk
        T.settings.mock_mode = old_mock
    return restore, lambda: _registry[cache_name]()


def scenario_registry():
    """1) Регистрация, схема, TTL, композит-флаг, роли."""
    names = {s["function"]["name"] for s in TOOLS_SCHEMA}
    check("get_printers_pages_report в TOOLS_SCHEMA",
          "get_printers_pages_report" in names)
    f = _registry["get_printers_pages_report"]
    check("схема без параметров",
          f.tool_parameters == {"type": "object", "properties": {},
                                "required": []},
          f"got {f.tool_parameters}")
    check("TTL=120, is_composite=True, viewer допущен",
          f.tool_cache_ttl == 120 and f.is_composite
          and "viewer" in f.tool_roles,
          f"ttl={f.tool_cache_ttl} composite={f.is_composite} "
          f"roles={f.tool_roles}")


def scenario_mock():
    """2) Мок-форма: summary, CMYK-картриджи, unreachable, < MAX_RESULT."""
    res, status = execute_tool("get_printers_pages_report", {},
                               None, None, "viewer")
    data = json.loads(res)
    ok = (status == "ok"
          and data["summary"] == {"total": 3, "reachable": 2,
                                  "unreachable": 1, "mono": 1, "color": 1,
                                  "total_pages": 13322}
          and data["printers"][0]["color"] is False
          and data["printers"][1]["color"] is True
          and len(data["printers"][1]["cartridges"]) == 4
          and data["printers"][1]["cartridges"][0]["role"] == "black"
          and data["unreachable"][0]["name"] == "storage-printer-3")
    check("мок: summary/моно/цветной CMYK/unreachable", ok,
          f"status={status} data={data}")
    check("мок: результат короче MAX_RESULT", len(res) < MAX_RESULT,
          f"len={len(res)}")


def _walk_mono_color_dead():
    """Таблицы (RFC 3805): .150 — моно (тонер+waste-бокс!), .151 —
    цветной CMYK (описания картриджей в 43.11.1.1.6), .152 — мёртвый."""
    def fake_walk(h, oid, community="public", timeout=2.0,
                  max_repetitions=64, port=161):
        if h == "192.0.2.150":
            # моно: тонер + waste-бокс = 2 supplies — НЕ цветной!
            tables = {
                "1.3.6.1.2.1.43.10.2.1.4": [("x.1", 12345)],
                "1.3.6.1.2.1.43.11.1.1.6": [
                    ("x.1", "Black Toner Cartridge"),
                    ("x.2", "Waste Toner Box")],
                "1.3.6.1.2.1.43.11.1.1.8": [("x.1", 10000), ("x.2", 0)],
                "1.3.6.1.2.1.43.11.1.1.9": [("x.1", 4200), ("x.2", -3)],
            }
            return tables.get(oid, [])
        if h == "192.0.2.151":    # цветной: 4 картриджа CMYK
            tables = {
                "1.3.6.1.2.1.43.10.2.1.4": [("x.1", 977)],
                "1.3.6.1.2.1.43.11.1.1.6": [
                    ("x.1", "Black Toner Cartridge"),
                    ("x.2", "Cyan Toner Cartridge"),
                    ("x.3", "Magenta Toner Cartridge"),
                    ("x.4", "Yellow Toner Cartridge")],
                "1.3.6.1.2.1.43.11.1.1.8": [("x.1", 10000), ("x.2", 8000),
                                            ("x.3", 8000), ("x.4", 8000)],
                "1.3.6.1.2.1.43.11.1.1.9": [("x.1", 4200), ("x.2", 4000),
                                            ("x.3", 5000), ("x.4", 3000)],
            }
            return tables.get(oid, [])
        if h == "192.0.2.152":
            raise RuntimeError(f"WALK {h}: No SNMP response")
        return []
    return fake_walk


def scenario_logic():
    """3) Моно/цветной CMYK/total_pages/unreachable на подмене walk."""
    _clear_devices()
    _add_printers([("hq-printer-1", "192.0.2.150"),
                   ("color-printer-2", "192.0.2.151"),
                   ("dead-printer-3", "192.0.2.152")])
    restore, call = _with_real_logic(_walk_mono_color_dead())
    try:
        res, status = call()
        data = json.loads(res)
    finally:
        restore()
    rows = {r["name"]: r for r in data["printers"]}
    check("status ok + summary 3/2/1, mono=1 color=1, total=13322",
          status == "ok"
          and data["summary"] == {"total": 3, "reachable": 2,
                                 "unreachable": 1, "mono": 1, "color": 1,
                                 "total_pages": 12345 + 977},
          f"summary={data['summary']}")
    check("моно с waste-боксом (2 supplies): color=False, cartridges пуст",
          rows["hq-printer-1"]["color"] is False
          and rows["hq-printer-1"]["cartridges"] == []
          and rows["hq-printer-1"]["pages_printed"] == 12345,
          f"r={rows.get('hq-printer-1')}")
    c = rows["color-printer-2"]
    check("цветной: color=True, 4 картриджа, black первым, проценты",
          c["color"] is True and len(c["cartridges"]) == 4
          and c["cartridges"][0] == {"supply": "1", "role": "black",
                                     "percent": 42.0}
          and c["cartridges"][1] == {"supply": "2", "role": "color",
                                     "percent": 50.0}
          and c["cartridges"][3]["percent"] == 37.5,
          f"cartridges={c.get('cartridges')}")
    check("недоступный в unreachable, не в printers",
          data["unreachable"][0]["name"] == "dead-printer-3"
          and "dead-printer-3" not in rows,
          f"unreach={data['unreachable']}")


def scenario_fallback_color():
    """4) Описаний картриджей нет: color=False, роли не выдумываем."""
    def fake_walk(h, oid, community="public", timeout=2.0,
                  max_repetitions=64, port=161):
        if h == "192.0.2.160":
            tables = {
                "1.3.6.1.2.1.43.10.2.1.4": [("x.1", 500)],
                # описаний нет (пустой walk) — цветность не определена
                "1.3.6.1.2.1.43.11.1.1.8": [("x.1", 100), ("x.2", 100)],
                "1.3.6.1.2.1.43.11.1.1.9": [("x.1", 50), ("x.2", 30)],
            }
            return tables.get(oid, [])
        return []
    _clear_devices()
    _add_printers([("fb-printer", "192.0.2.160")])
    restore, call = _with_real_logic(fake_walk)
    try:
        res, _ = call()
        data = json.loads(res)
    finally:
        restore()
    r = data["printers"][0]
    check("нет описаний: color=False (не выдумываем), тонер считается",
          r["color"] is False and r["toner_percent"] == 50.0
          and data["summary"]["color"] == 0,
          f"r={r}")
    check("нет описаний: cartridges пуст",
          r["cartridges"] == [], f"got {r['cartridges']}")


def scenario_budget():
    """5) 130 цветных с картриджами: деградация бюджета, валидный JSON."""
    names = [(f"bulk-c-{i:03d}", f"10.90.{i // 250}.{i % 250 + 1}")
             for i in range(130)]

    def fake_walk(h, oid, community="public", timeout=2.0,
                  max_repetitions=64, port=161):
        tables = {
            "1.3.6.1.2.1.43.10.2.1.4": [("x.1", 1000)],
            "1.3.6.1.2.1.43.11.1.1.6": [("x.1", "Black Toner Cartridge"),
                                        ("x.2", "Cyan Toner Cartridge")],
            "1.3.6.1.2.1.43.11.1.1.8": [("x.1", 100), ("x.2", 100)],
            "1.3.6.1.2.1.43.11.1.1.9": [("x.1", 90), ("x.2", 80)],
        }
        return tables.get(oid, [])

    _clear_devices()
    _add_printers(names)
    restore, call = _with_real_logic(fake_walk)
    try:
        res, _ = call()
    finally:
        restore()
    check("бюджет: JSON валиден и < MAX_RESULT",
          len(res) < MAX_RESULT, f"len={len(res)}")
    data = json.loads(res)     # невалидный JSON бросит — проверка выше
    included_color = sum(1 for r in data["printers"] if r["color"])
    with_carts = sum(1 for r in data["printers"] if r["cartridges"])
    check("деградация: часть цветных без cartridges, часть в remaining",
          data["summary"]["color"] == 130
          and included_color + len(data["remaining"]) == 130
          and with_carts < 130,
          f"summary={data['summary']} included={len(data['printers'])} "
          f"remaining={len(data['remaining'])} with_carts={with_carts}")
    check("total_pages: сумма по всем 130 (считается до обрезки)",
          data["summary"]["total_pages"] == 130 * 1000,
          f"got {data['summary']['total_pages']}")


def scenario_empty_and_filter():
    """6) Пустой инвентарь -> Exception; eltex не попадает в отчёт."""
    _clear_devices()
    # eltex-устройство не должно опрашиваться (фильтр type==printer)
    with SessionLocal() as db:
        db.add(Device(name="sw-1", type=DeviceType.eltex, host="10.5.0.1",
                      port=22, username="u", password="p",
                      description="", group="Сеть", enabled=True))
        db.commit()

    def fake_walk(h, oid, community="public", timeout=2.0,
                  max_repetitions=64, port=161):
        raise AssertionError(f"SNMP не должен вызываться для {h}")

    restore, call = _with_real_logic(fake_walk)
    try:
        try:
            call()
            check("пустой инвентарь -> Exception", False, "не упал")
        except Exception as e:
            check("пустой инвентарь -> Exception «нет включённых принтеров»",
                  "нет включённых принтеров" in str(e), f"e={e}")
    finally:
        restore()
    # инвентарь чистим для следующих прогонов (одноразовая temp-БД,
    # но сценарии идут последовательно — на всякий случай)
    with SessionLocal() as db:
        db.query(Device).delete()
        db.commit()


def main():
    scenario_registry()
    scenario_mock()
    scenario_logic()
    scenario_fallback_color()
    scenario_budget()
    scenario_empty_and_filter()

    print()
    print(f"Итог: PASS={PASS} FAIL={FAIL}")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
