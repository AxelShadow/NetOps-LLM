"""SNMP-инструменты агента (Этап SNMP, PLAN.md §9): моки, RBAC, логика.

Стиль test_agent_tools_rbac.py: временная sqlite, env задаётся ДО
импорта app.*, рабочая netops.db не затрагивается.

Проверяет:
  1) все 4 инструмента (snmp_info/snmp_interfaces/snmp_walk/printer_info)
     зарегистрированы со схемами; cache_ttl у snmp_info/printer_info =120,
     у snmp_walk =0;
  2) мок-режим: все 4 отдают фейковые данные (single + all);
  3) реальная логика printer_info (тонер-проценты, битмаска ошибок,
     статус) — на подменённых walk-данных, мок-режим временно выключен;
  4) RBAC: snmp-инструменты допускают viewer;
  5) маркер mock-ошибка -> status="error" (триггер _check_error в моке);
  6) VMware-устройство -> понятная ошибка «не поддерживает SNMP-опрос»
     (мок выключен, до сетевого вызова не доходит);
  7) устройство не из инвентаря -> ошибка со списком доступных.

Запуск: .venv/Scripts/python.exe tests/test_snmp_tools.py  (из backend/)
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

# Windows-консоль по умолчанию cp1251: кириллица в PASS/FAIL-строках
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TMP = tempfile.mkdtemp(prefix="netops_snmp_tools_")
os.environ["NETOPS_DATABASE_URL"] = f"sqlite:///{TMP}/snmp_tools_test.db".replace("\\", "/")
os.environ["NETOPS_MOCK_MODE"] = "true"
os.environ["NETOPS_DEV_MODE"] = "true"
os.environ["NETOPS_JWT_SECRET"] = "snmp-tools-test-secret"
os.environ["NETOPS_LLM_BASE_URL"] = "http://127.0.0.1:9/v1"  # ничего не слушает

import json  # noqa: E402

from app.agent.tools import (  # noqa: E402
    TOOLS_SCHEMA, _registry, execute_tool,
)
from app.agent.mock import MOCK_TOOLS  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import Device, DeviceType  # noqa: E402
import app.agent.tools as T  # noqa: E402

# Схема БД для теста (без полного lifespan приложения)
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


def _seed_devices():
    with SessionLocal() as db:
        db.add(Device(name="hq-printer-1", type=DeviceType.printer,
                      host="192.0.2.150", port=161, username="", password="",
                      description="Принтер ХО", group="Принтеры",
                      snmp_version="2c", snmp_community="public"))
        db.add(Device(name="branch-printer-2", type=DeviceType.printer,
                      host="192.0.2.151", port=161, username="", password="",
                      description="Принтер филиала", group="Принтеры",
                      snmp_version="2c", snmp_community="public"))
        db.add(Device(name="core-sw", type=DeviceType.eltex,
                      host="192.0.2.10", port=22, username="op", password="x",
                      description="Ядро сети", group="Сеть",
                      snmp_version="2c", snmp_community="public"))
        db.add(Device(name="vcenter-1", type=DeviceType.vcenter,
                      host="192.0.2.5", port=0, username="u", password="p",
                      description="vCenter", group="VMware"))
        db.commit()


def scenario_registry():
    """1) Регистрация, схемы, cache_ttl."""
    expected = {"snmp_info", "snmp_interfaces", "snmp_walk", "printer_info"}
    registered = {s["function"]["name"] for s in TOOLS_SCHEMA}
    check("все 4 SNMP-инструмента в TOOLS_SCHEMA",
          expected <= registered, f"нет: {expected - registered}")

    f_info = _registry["snmp_info"]
    f_walk = _registry["snmp_walk"]
    f_printer = _registry["printer_info"]
    check("cache_ttl: snmp_info=120, snmp_walk=0, printer_info=120",
          f_info.tool_cache_ttl == 120 and f_walk.tool_cache_ttl == 0
          and f_printer.tool_cache_ttl == 120,
          f"got {f_info.tool_cache_ttl}/{f_walk.tool_cache_ttl}"
          f"/{f_printer.tool_cache_ttl}")

    expected_mocks = {"snmp_info", "snmp_interfaces", "snmp_walk",
                      "printer_info"}
    check("все 4 мока в MOCK_TOOLS",
          expected_mocks <= set(MOCK_TOOLS.keys()),
          f"нет: {expected_mocks - set(MOCK_TOOLS.keys())}")


def scenario_mocks():
    """2) Мок-диспетчеризация: wrapper отдаёт фейковые данные."""
    res, status = execute_tool("snmp_info", {"device": "core-sw"},
                               None, None, "viewer")
    data = json.loads(res)
    check("мок snmp_info: sysDescr + uptime",
          status == "ok" and "Mock SNMP device" in str(data.get("sysDescr"))
          and data.get("uptime") == "3 д 4 ч 5 мин",
          f"status={status} data={data}")

    res, status = execute_tool("snmp_interfaces", {"device": "core-sw"},
                               None, None, "viewer")
    data = json.loads(res)
    ifaces = data.get("interfaces") or []
    check("мок snmp_interfaces: eth0 + lo",
          status == "ok" and len(ifaces) == 2
          and {i["name"] for i in ifaces} == {"eth0", "lo"},
          f"status={status} ifaces={ifaces}")

    res, status = execute_tool("snmp_walk",
                               {"device": "core-sw", "oid": "1.3.6.1.2.1.1"},
                               None, None, "viewer")
    data = json.loads(res)
    check("мок snmp_walk: 3 строки",
          status == "ok" and len(data.get("rows") or []) == 3,
          f"status={status} rows={data.get('rows')}")

    res, status = execute_tool("printer_info", {"device": "hq-printer-1"},
                               None, None, "viewer")
    data = json.loads(res)
    unit = (data.get("units") or [{}])[0]
    check("мок printer_info (single): serial/pages/toner/status",
          status == "ok" and unit.get("serial") == "MOCK12345"
          and unit.get("pages_printed") == 12345
          and unit.get("toner_level_percent") == 42.0
          and unit.get("status") == "printing",
          f"status={status} unit={unit}")
    check("мок printer_info: счётчик сканов честно None + note",
          data.get("pages_scanned") is None
          and bool(data.get("note_scanned")),
          f"scanned={data.get('pages_scanned')}")

    res, status = execute_tool("printer_info", {"device": "all"},
                               None, None, "viewer")
    data = json.loads(res)
    check("мок printer_info (all): 2 принтера",
          status == "ok"
          and set(data.keys()) == {"hq-printer-1", "branch-printer-2"},
          f"status={status} keys={set(data.keys())}")


def scenario_printer_logic():
    """3) Реальная логика printer_info на подменённых walk-данных.

    Мок-режим временно выключаем (иначе wrapper перехватит вызов),
    _snmp_walk подменяем таблицей Printer-MIB. Сетевых вызовов нет.
    """
    def fake_walk(host, oid, community="public", timeout=2.0,
                  max_repetitions=64, port=161):
        # hrPrinterDetectedErrorState = 0x1800 (RFC 2790, MSB-first):
        # первый байт 0x18 -> бит 3 noToner + бит 4 doorOpen
        tables = {
            "1.3.6.1.2.1.43.5.1.1.17": [("1.3.6.1.2.1.43.5.1.1.17.1", "SN-XYZ")],
            "1.3.6.1.2.1.25.3.5.1.1": [("1.3.6.1.2.1.25.3.5.1.1.1", 4)],
            "1.3.6.1.2.1.25.3.5.1.2": [("1.3.6.1.2.1.25.3.5.1.2.1", "0x1800")],
            "1.3.6.1.2.1.43.10.2.1.4": [("1.3.6.1.2.1.43.10.2.1.4.1", 9876)],
            "1.3.6.1.2.1.43.11.1.1.8": [("1.3.6.1.2.1.43.11.1.1.8.1", 10000)],
            "1.3.6.1.2.1.43.11.1.1.9": [("1.3.6.1.2.1.43.11.1.1.9.1", 2500)],
        }
        return tables.get(oid, [])

    real_walk = T._snmp_walk
    real_mock_mode = T.settings.mock_mode
    T._snmp_walk = fake_walk
    T.settings.mock_mode = False
    try:
        res, status = _registry["printer_info"](device="hq-printer-1")
        data = json.loads(res)
        unit = (data.get("units") or [{}])[0]
        supply = (data.get("toner_supplies") or [{}])[0]
        check("printer_info логика: serial/pages/status",
              status == "ok" and unit.get("serial") == "SN-XYZ"
              and unit.get("pages_printed") == 9876
              and unit.get("status") == "printing",
              f"status={status} unit={unit}")
        check("printer_info логика: тонер 2500/10000 -> 25.0%",
              unit.get("toner_level_percent") == 25.0
              and supply.get("percent") == 25.0,
              f"unit={unit} supply={supply}")
        check("printer_info логика: битмаска 0x1800 -> noToner+doorOpen (RFC 2790)",
              unit.get("errors") == ["noToner", "doorOpen"],
              f"errors={unit.get('errors')}")
        check("printer_info логика: pages_scanned=None + note",
              data.get("pages_scanned") is None
              and bool(data.get("note_scanned")),
              f"data_keys={sorted(data.keys())}")

        # Декодер BITS по RFC 2790 напрямую: MSB-first порядок битов.
        # бит i первого байта выставлен <=> byte & (0x80 >> i):
        # 0x80 -> бит 0 lowPaper; 0x06 -> биты 5,6 jammed+offline;
        # 0x1800 -> первый байт 0x18 -> биты 3,4 noToner+doorOpen.
        d = T._decode_printer_errors
        check("декодер BITS: 0x80 -> lowPaper (бит 0 = старший бит)",
              d("0x80") == ["lowPaper"], f"got {d('0x80')}")
        check("декодер BITS: 0x06 -> jammed+offline",
              d("0x06") == ["jammed", "offline"], f"got {d('0x06')}")
        check("декодер BITS: '' -> [] (нет ошибок)",
              d("") == [] and d("0x") == [], f"got {d('')!r}/{d('0x')!r}")
        check("декодер BITS: 0x1800 -> noToner+doorOpen",
              d("0x1800") == ["noToner", "doorOpen"],
              f"got {d('0x1800')}")
        check("декодер BITS: мусорный hex -> []",
              d("zz") == [], f"got {d('zz')}")
    finally:
        T._snmp_walk = real_walk
        T.settings.mock_mode = real_mock_mode


def scenario_rbac_and_errors():
    """4-7) RBAC-инвариант, маркер ошибки, тип-гвард, инвентарь."""
    f = _registry["snmp_info"]
    check("RBAC: viewer допущен ко всем SNMP-инструментам",
          all("viewer" in _registry[n].tool_roles
              for n in ("snmp_info", "snmp_interfaces", "snmp_walk",
                        "printer_info")),
          f"roles={f.tool_roles}")

    # Маркер ошибки: мок бросает Exception -> execute_tool -> error
    res, status = execute_tool("snmp_info", {"device": "mock-ошибка"},
                               None, None, "viewer")
    check("маркер mock-ошибка -> error",
          status == "error" and "Ошибка выполнения" in res,
          f"status={status} res={res[:80]!r}")

    # Тип-гвард и «нет в инвентаре» — реальный код, мок временно выключаем
    real_mock_mode = T.settings.mock_mode
    T.settings.mock_mode = False
    try:
        res, status = execute_tool("snmp_info", {"device": "vcenter-1"},
                                   None, None, "viewer")
        check("snmp на VMware-устройстве -> понятная ошибка",
              status == "error" and "не поддерживает SNMP-опрос" in res,
              f"status={status} res={res[:120]!r}")

        res, status = execute_tool("snmp_info", {"device": "no-such-dev"},
                                   None, None, "viewer")
        check("snmp на отсутствующем устройстве -> ошибка со списком",
              status == "error" and "не найдено в инвентаре" in res
              and "hq-printer-1" in res,
              f"status={status} res={res[:120]!r}")
    finally:
        T.settings.mock_mode = real_mock_mode


def main():
    _seed_devices()
    scenario_registry()
    scenario_mocks()
    scenario_printer_logic()
    scenario_rbac_and_errors()

    print()
    print(f"Итог: PASS={PASS} FAIL={FAIL}")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
