"""Композитный get_printers_report (Этап 19): все принтеры за один вызов.

Стиль test_snmp_tools.py: временная sqlite, env ДО импорта app.*, рабочая
netops.db не затрагивается. Сетевые вызовы подменяются (T._snmp_walk).

Проверяет:
  1) регистрация: схема пустая, TTL=120, is_composite, viewer в ролях;
  2) мок-диспетчеризация: компакт-форма, длина < MAX_RESULT;
  3) реальная логика (мок-режим off + подмена walk): полный набор полей,
     тонер-расчёт, ошибки через _decode_printer_errors;
  4) недоступный принтер -> unreachable, остальные целы;
  5) проблемный (тонер < _LOW_TONER) -> with_problems;
  6) 130 «здоровых» -> длина < 20000, remaining_healthy непуст;
  7) пустой инвентарь -> понятная ошибка; eltex не опрашивается.

Запуск: .venv/Scripts/python.exe tests/test_printers_report.py  (из backend/)
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

# Windows-консоль по умолчанию cp1251: кириллица в PASS/FAIL-строках
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TMP = tempfile.mkdtemp(prefix="netops_printers_report_")
os.environ["NETOPS_DATABASE_URL"] = f"sqlite:///{TMP}/report_test.db".replace("\\", "/")
os.environ["NETOPS_MOCK_MODE"] = "true"
os.environ["NETOPS_DEV_MODE"] = "true"
os.environ["NETOPS_JWT_SECRET"] = "report-test-secret"
os.environ["NETOPS_LLM_BASE_URL"] = "http://127.0.0.1:9/v1"  # ничего не слушает

import json  # noqa: E402

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


def scenario_registry():
    """1) Регистрация, схема, TTL, композит-флаг, роли."""
    names = {s["function"]["name"] for s in TOOLS_SCHEMA}
    check("get_printers_report в TOOLS_SCHEMA", "get_printers_report" in names)
    f = _registry["get_printers_report"]
    schema_ok = (f.tool_parameters == {"type": "object", "properties": {},
                                       "required": []})
    check("схема без параметров", schema_ok, f"got {f.tool_parameters}")
    check("TTL=120, is_composite=True, viewer допущен",
          f.tool_cache_ttl == 120 and f.is_composite
          and "viewer" in f.tool_roles,
          f"ttl={f.tool_cache_ttl} composite={f.is_composite} "
          f"roles={f.tool_roles}")


def scenario_mock():
    """2) Мок-диспетчеризация: компакт-форма, без превышения MAX_RESULT."""
    res, status = execute_tool("get_printers_report", {}, None, None, "viewer")
    data = json.loads(res)
    ok = (status == "ok"
          and data["summary"] == {"total": 3, "healthy": 1,
                                  "with_problems": 1, "unreachable": 1}
          and len(data["printers"]) == 2
          and data["printers"][1]["errors"] == ["lowToner"]
          and data["unreachable"][0]["name"] == "storage-printer-3")
    check("мок get_printers_report: summary/проблемный/недоступный",
          ok, f"status={status} data={data}")
    check("мок: результат короче MAX_RESULT", len(res) < MAX_RESULT,
          f"len={len(res)}")


def _fake_walk_tables(host):
    """Подмена walk по хосту: 192.0.2.150 — ok, .151 — тонер 8%, прочее — [].

    Возвращает функцию для T._snmp_walk.
    """
    def fake_walk(h, oid, community="public", timeout=2.0,
                  max_repetitions=64, port=161):
        if h == "192.0.2.150":
            tables = {
                "1.3.6.1.2.1.43.5.1.1.17": [("x.1", "SN-OK")],
                "1.3.6.1.2.1.43.10.2.1.4": [("x.1", 12345)],
                "1.3.6.1.2.1.43.11.1.1.8": [("x.1", 10000)],
                "1.3.6.1.2.1.43.11.1.1.9": [("x.1", 2500)],
                "1.3.6.1.2.1.25.3.5.1.1": [("x.1", 4)],
                "1.3.6.1.2.1.25.3.5.1.2": [("x.1", "")],
            }
            return tables.get(oid, [])
        if h == "192.0.2.151":
            tables = {
                "1.3.6.1.2.1.43.5.1.1.17": [("x.1", "SN-LOW")],
                "1.3.6.1.2.1.43.10.2.1.4": [("x.1", 987)],
                "1.3.6.1.2.1.43.11.1.1.8": [("x.1", 10000)],
                "1.3.6.1.2.1.43.11.1.1.9": [("x.1", 800)],
                "1.3.6.1.2.1.25.3.5.1.1": [("x.1", 3)],
                "1.3.6.1.2.1.25.3.5.1.2": [("x.1", "0x0600")],  # jammed+offline
            }
            return tables.get(oid, [])
        if h == "192.0.2.152":
            raise T.SnmpError(
                f"WALK {h}: No SNMP response received before timeout")
        return []
    return fake_walk


def _with_real_logic(fake_walk):
    """Контекст: мок-режим off + подмена walk + чистый кэш инструмента.

    Кэш wrapper'а (TTL 120s) между сценариями вернул бы результат
    прошлого прогона — очищаем _caches перед запуском.
    """
    old_walk, old_mock = T._snmp_walk, T.settings.mock_mode
    T._snmp_walk = fake_walk
    T.settings.mock_mode = False
    # Чистим закэшированный результат (ключ не удаляем: wrapper ждёт
    # существующий TTLCache в _caches — он создаётся один раз при
    # регистрации инструмента).
    if "get_printers_report" in T._caches:
        T._caches["get_printers_report"].clear()

    def restore():
        T._snmp_walk = old_walk
        T.settings.mock_mode = old_mock
    return restore, lambda: _registry["get_printers_report"]()


def scenario_logic():
    """3-5) Реальная логика: поля, тонер, ошибки, недоступный, проблемный."""
    _add_printers([("ok-prn", "192.0.2.150"),
                   ("low-prn", "192.0.2.151"),
                   ("dead-prn", "192.0.2.152")])
    restore, run = _with_real_logic(_fake_walk_tables(None))
    try:
        res, status = run()
        data = json.loads(res)
        rows = {r["name"]: r for r in data["printers"]}
        ok_row = rows.get("ok-prn") or {}
        check("логика: ok-принтер — все поля",
              status == "ok"
              and ok_row.get("status") == "printing"
              and ok_row.get("pages_printed") == 12345
              and ok_row.get("toner_percent") == 25.0
              and ok_row.get("errors") == []
              and ok_row.get("serial") == "SN-OK",
              f"row={ok_row}")
        low_row = rows.get("low-prn") or {}
        check("логика: тонер 800/10000 -> 8.0, ошибки 0x0600 -> jammed+offline",
              low_row.get("toner_percent") == 8.0
              and low_row.get("errors") == ["jammed", "offline"],
              f"row={low_row}")
        check("логика: недоступный в unreachable, не в printers",
              data["summary"]["unreachable"] == 1
              and data["unreachable"][0]["name"] == "dead-prn"
              and "dead-prn" not in rows,
              f"summary={data['summary']}")
        check("логика: сводка 3/1/1/1 (total/healthy/problems/unreach)",
              data["summary"] == {"total": 3, "healthy": 1,
                                  "with_problems": 1, "unreachable": 1},
              f"summary={data['summary']}")
        check("логика: проблемный отсортирован в начало",
              data["printers"][0]["name"] == "low-prn",
              f"first={data['printers'][0]['name']}")
    finally:
        restore()


def scenario_budget():
    """6) 130 здоровых принтеров: бюджет, remaining_healthy, < MAX_RESULT."""
    _add_printers([(f"bulk-p-{i:03d}", f"10.99.{i // 250}.{i % 250 + 1}")
                   for i in range(130)])
    restore, run = _with_real_logic(_fake_walk_tables(None))
    try:
        res, status = run()
        data = json.loads(res)
        check("130 принтеров: JSON < MAX_RESULT (без обрезки)",
              status == "ok" and len(res) < MAX_RESULT,
              f"len={len(res)} status={status}")
        bulk_in_printers = len([r for r in data["printers"]
                                if r["name"].startswith("bulk-p")])
        check("130 принтеров: remaining_healthy непуст, healthy не потеряны",
              len(data["remaining_healthy"]) > 0
              and bulk_in_printers + len(data["remaining_healthy"]) == 130
              and data["summary"]["healthy"]
              == bulk_in_printers + len(data["remaining_healthy"]) + 1,
              f"summary={data['summary']} in={bulk_in_printers} "
              f"remaining={len(data['remaining_healthy'])}")
    finally:
        restore()


def scenario_many_problems():
    """6b) 100 ПРОБЛЕМНЫХ принтеров: бюджет деградирует, JSON валиден.

    Верификатор Этапа 19: 76+ problem-строк полного формата сами по себе
    превышали бы MAX_RESULT -> битый JSON. Теперь problem-строки тоже
    в бюджете: без serial, затем remaining_problems.
    """
    # 100 «проблемных»: тонер 5% + ошибка (битмаска 0x0600 -> jammed+offline)
    def fake_walk_problems(h, oid, community="public", timeout=2.0,
                           max_repetitions=64, port=161):
        if h.startswith("10.98."):
            tables = {
                "1.3.6.1.2.1.43.5.1.1.17": [("x.1", "SN-VERY-LONG-SERIAL-123456")],
                "1.3.6.1.2.1.43.10.2.1.4": [("x.1", 111)],
                "1.3.6.1.2.1.43.11.1.1.8": [("x.1", 10000)],
                "1.3.6.1.2.1.43.11.1.1.9": [("x.1", 500)],   # 5% < 15
                "1.3.6.1.2.1.25.3.5.1.1": [("x.1", 3)],
                "1.3.6.1.2.1.25.3.5.1.2": [("x.1", "0x0600")],
            }
            return tables.get(oid, [])
        raise T.SnmpError(f"WALK {h}: timeout")

    _add_printers([(f"prob-p-{i:03d}", f"10.98.{i // 250}.{i % 250 + 1}")
                   for i in range(100)])
    old_walk, old_mock = T._snmp_walk, T.settings.mock_mode
    T._snmp_walk = fake_walk_problems
    T.settings.mock_mode = False
    T._caches["get_printers_report"].clear()
    try:
        res, status = T._registry["get_printers_report"]()
        data = json.loads(res)      # невалидный JSON упал бы здесь
        n_included = len(data["printers"])
        n_remaining = len(data["remaining_problems"])
        check("100 проблемных: JSON валиден и < MAX_RESULT",
              status == "ok" and len(res) < MAX_RESULT,
              f"len={len(res)}")
        check("100 проблемных: with_problems=100, ни один не потерян "
              "(included + remaining_problems)",
              data["summary"]["with_problems"] == 100
              and n_included + n_remaining == 100
              and (n_remaining == 0 or "remaining_problems" in data
                   and data["note_unlisted"]),
              f"included={n_included} remaining={n_remaining}")
        check("100 проблемных: healthy=0 (все проблемные)",
              data["summary"]["healthy"] == 0,
              f"summary={data['summary']}")
    finally:
        T._snmp_walk = old_walk
        T.settings.mock_mode = old_mock
        T._caches["get_printers_report"].clear()


def scenario_empty_and_filter():
    """7) Пустой инвентарь — ошибка; eltex не опрашивается."""
    # Отдельная чистая таблица невозможна (engine один), поэтому проверяем
    # на отдельной БД через новый bind: делаем простую подмену — запрос
    # с SessionLocal уже дал бы данные, поэтому проверяем фильтр типа:
    # eltex-устройство рядом с принтером не должно попасть в отчёт.
    with SessionLocal() as db:
        db.add(Device(name="core-sw", type=DeviceType.eltex,
                      host="192.0.2.10", port=22, username="op",
                      password="x", description="", group="Сеть",
                      enabled=True, snmp_version="2c",
                      snmp_community="public"))
        db.commit()
    restore, run = _with_real_logic(_fake_walk_tables(None))
    try:
        res, status = run()
        data = json.loads(res)
        names = [r["name"] for r in data["printers"]]
        check("eltex-устройство не опрашивается (фильтр type==printer)",
              "core-sw" not in names
              and "core-sw" not in [u["name"] for u in data["unreachable"]],
              f"names={names[:5]}")
    finally:
        restore()

    # Пустой инвентарь: отдельный движок с чистой таблицей + мок-режим off
    # (иначе wrapper вернёт мок, а не выполнит функцию)
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    old_local = T.SessionLocal
    old_mock = T.settings.mock_mode
    tmp_engine = create_engine(
        f"sqlite:///{TMP}/empty.db".replace("\\", "/"))
    Base.metadata.create_all(tmp_engine)
    T.SessionLocal = sessionmaker(bind=tmp_engine)
    if "get_printers_report" in T._caches:
        T._caches["get_printers_report"].clear()
    T.settings.mock_mode = False
    try:
        try:
            run_empty = _registry["get_printers_report"]
            res, st = run_empty()
            check("пустой инвентарь -> понятная ошибка", False,
                  f"не упал: {res[:60]!r}")
        except Exception as e:
            check("пустой инвентарь -> понятная ошибка",
                  "нет включённых принтеров" in str(e), f"exc={e}")
    finally:
        T.settings.mock_mode = old_mock
        T.SessionLocal = old_local
        tmp_engine.dispose()


def main():
    scenario_registry()
    scenario_mock()
    scenario_logic()
    scenario_budget()
    scenario_many_problems()
    scenario_empty_and_filter()

    print()
    print(f"Итог: PASS={PASS} FAIL={FAIL}")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
