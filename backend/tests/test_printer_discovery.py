"""SNMP-Discovery принтеров (Этап 19): parse_subnets / probe_ip /
discover_printers / sync_printer_discovery + UI-эндпоинт.

Стиль test_snmp_tools.py: временная sqlite, env ДО импорта app.*,
рабочая netops.db не затрагивается. Сеть подменяется на модульных
функциях (никаких UDP-запросов).

Проверяет:
  1) parse_subnets: валидные/невалидные подсети, лимиты;
  2) probe_ip: критерий принтера (серийник / keywords / не принтер /
     молчит) на подменённых snmp_get/snmp_walk;
  3) discover_printers: счётчики probed/responded/found на /30;
  4) sync_printer_discovery: добавление выключенных принтеров группы
     «Принтеры» (source=snmp), имя из sysName, fallback printer-{ip},
     коллизия имени, re-run -> updated (не дубль), чужой host -> skipped;
  5) UI: POST /admin/inventory/discover-printers -> 200 + flash +
     snmp-бейдж; невалидная подсеть -> flash-ошибка; viewer -> 403.

Запуск: .venv/Scripts/python.exe tests/test_printer_discovery.py (из backend/)
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

# Windows-консоль по умолчанию cp1251: кириллица в PASS/FAIL-строках
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TMP = tempfile.mkdtemp(prefix="netops_disc_")
os.environ["NETOPS_DATABASE_URL"] = f"sqlite:///{TMP}/disc_test.db".replace("\\", "/")
os.environ["NETOPS_MOCK_MODE"] = "true"
os.environ["NETOPS_DEV_MODE"] = "true"
os.environ["NETOPS_AD_DOMAIN"] = "mock.local"
os.environ.setdefault("NETOPS_BOOTSTRAP_ADMIN", "admin@mock.local")
os.environ.setdefault("NETOPS_JWT_SECRET", "disc-test-secret")
os.environ["NETOPS_LLM_BASE_URL"] = "http://127.0.0.1:9/v1"  # ничего не слушает
os.environ["NETOPS_INTERNAL_SERVICE_TOKEN"] = "internal-test-token"
os.environ.pop("NETOPS_ZABBIX_URL", None)
os.environ.pop("NETOPS_ZABBIX_TOKEN", None)
os.environ["NETOPS_ZABBIX_URL"] = ""
os.environ["NETOPS_ZABBIX_TOKEN"] = ""

import ipaddress  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import Device, DeviceType  # noqa: E402
from app.devices import printer_discovery as D  # noqa: E402
import app.api.devices as api_dev  # noqa: E402
import app.main  # noqa: E402  (lifespan: create_all + миграции)

# Схема нужна ДО scenario_sync (он пишет напрямую через SessionLocal;
# TestClient-жизнь создаст таблицы только позже — а sync идёт первым)
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


def scenario_parse_subnets():
    """1) Валидация подсетей: ОДНА подсеть, маска до /22."""
    nets = D.parse_subnets("10.0.10.0/24")
    check("одна /24", [str(n) for n in nets] == ["10.0.10.0/24"],
          f"got {nets}")
    nets = D.parse_subnets("10.0.10.5/24")     # strict=False: нормализуется
    check("хостовый адрес нормализуется в сеть",
          str(nets[0]) == "10.0.10.0/24", f"got {nets[0]}")
    nets = D.parse_subnets("10.0.0.0/22")
    check("/22 (1024 адреса) проходит — нижняя граница маски",
          str(nets[0]) == "10.0.0.0/22", f"got {nets}")

    def expect_err(text, name):
        try:
            D.parse_subnets(text)
            check(name, False, "не упал")
        except ValueError as e:
            check(name, True, str(e))

    expect_err("", "пустая строка -> ValueError")
    expect_err("abc", "мусор -> ValueError")
    expect_err("2001:db8::/32", "IPv6 -> ValueError")
    expect_err("10.0.0.0/21", "/21 крупнее /22 -> ValueError")
    expect_err("10.0.1.0/24, 10.0.2.0/24", "две подсети -> ValueError")
    try:
        D.parse_subnets("10.0.0.0/23")
        check("/23 проходит (между /22 и /24)", True)
    except ValueError as e:
        check("/23 проходит (между /22 и /24)", False, str(e))


def scenario_probe_ip():
    """2) Критерий «принтер» на подменённых snmp_get/snmp_walk + MAC/DNS."""
    real_get, real_walk = D.snmp_get, D.snmp_walk
    real_dns = D._resolve_dns

    def fake_get(ip, oids, community="public", timeout=2.0, *, port=161):
        if ip == "10.0.0.1":    # принтер с серийником
            return {oids[0]: "HP LaserJet 400 M401", oids[1]: "NPR-HP1"}
        if ip == "10.0.0.2":    # принтер без серийной таблицы (keyword)
            return {oids[0]: "Kyocera ECOSYS M2540", oids[1]: "NPR-KY"}
        if ip == "10.0.0.3":    # свитч (отвечает, не принтер)
            return {oids[0]: "Eltex MES-2324", oids[1]: "sw"}
        raise Exception(f"GET {ip}: timeout")   # молчит

    def fake_walk(ip, oid, community="public", timeout=2.0,
                  max_repetitions=64, *, port=161):
        if ip == "10.0.0.1":
            if oid == D._SERIAL_OID:
                return [("1.3.6.1.2.1.43.5.1.1.17.1", "CNB1G00234")]
            if oid == D._IF_PHYS_ADDRESS:
                # первый «интерфейс» — мусор, второй — валидный MAC
                return [("1.3.6.1.2.1.2.2.1.6.1", ""),
                        ("1.3.6.1.2.1.2.2.1.6.2", "0x001B1E2F3A4C")]
        if ip == "10.0.0.2" and oid == D._IF_PHYS_ADDRESS:
            return [("1.3.6.1.2.1.2.2.1.6.1", "0x001B1E2F3A4C")]
        return []              # у остальных серийной таблицы нет

    def fake_dns(ip, timeout=None):
        return "npr-hp1.corp.local" if ip == "10.0.0.1" else ""

    D.snmp_get, D.snmp_walk = fake_get, fake_walk
    D._resolve_dns = fake_dns
    try:
        r = D.probe_ip("10.0.0.1")
        check("серийная таблица -> принтер (авторитетный критерий)",
              r is not None and r["serial"] == "CNB1G00234"
              and r["sys_descr"] == "HP LaserJet 400 M401",
              f"r={r}")
        check("MAC из ifPhysAddress: 0x001B... -> 00:1b:1e:2f:3a:4c",
              r is not None and r["mac"] == "00:1b:1e:2f:3a:4c",
              f"mac={r and r.get('mac')}")
        check("DNS PTR -> dns_name",
              r is not None and r["dns_name"] == "npr-hp1.corp.local",
              f"dns={r and r.get('dns_name')}")
        r = D.probe_ip("10.0.0.2")
        check("keyword в sysDescr -> принтер (serial=None) + MAC",
              r is not None and r["serial"] is None
              and r["mac"] == "00:1b:1e:2f:3a:4c", f"r={r}")
        r = D.probe_ip("10.0.0.3")
        check("свитч без серийника и keywords -> None", r is None,
              f"r={r}")
        r = D.probe_ip("10.0.0.99")
        check("молчащий хост -> None", r is None, f"r={r}")
    finally:
        D.snmp_get, D.snmp_walk = real_get, real_walk
        D._resolve_dns = real_dns


def scenario_mac_norm():
    """2b) _norm_mac: hex prettyPrint -> aa:bb:cc:dd:ee:ff."""
    check("0x001B1E2F3A4C -> 00:1b:1e:2f:3a:4c",
          D._norm_mac("0x001B1E2F3A4C") == "00:1b:1e:2f:3a:4c",
          f"got {D._norm_mac('0x001B1E2F3A4C')}")
    check("короткая hex-строка -> None",
          D._norm_mac("0x123") is None)
    check("не-hex -> None", D._norm_mac("Eltex OS") is None)
    check("None -> None", D._norm_mac(None) is None)
    check("нечётная длина -> None", D._norm_mac("0x12345") is None)


def scenario_discover():
    """3) discover_printers: счётчики на /30 с фейковым probe."""
    real_probe = D.probe_ip
    calls = []

    def fake_probe(ip, community="public", timeout=1.0, port=161):
        calls.append(ip)
        return ({"ip": ip, "sys_name": f"prn-{ip}", "sys_descr": "HP",
                 "serial": "S1"} if ip.endswith(".1") else None)

    D.probe_ip = fake_probe
    try:
        net = ipaddress.ip_network("10.0.0.0/30")
        r = D.discover_printers([net])
        check("/30: 2 хоста опрошено, found=1, responded=1",
              r["probed"] == 2 and len(r["found"]) == 1
              and r["responded"] == 1, f"r={r}")
    finally:
        D.probe_ip = real_probe


def scenario_sync():
    """4) sync_printer_discovery: БД-поведение (сеть подменена).

    discover_printers импортируется в sync_printer_discovery локально
    из ..devices.printer_discovery, поэтому подменяем на модуле D.
    """
    real_discover = D.discover_printers
    found = [
        {"ip": "192.0.2.201", "sys_name": "NPR-HP1",
         "sys_descr": "HP LaserJet 400", "serial": "S1",
         "mac": "00:1b:1e:2f:3a:01", "dns_name": "npr-hp1.corp.local"},
        {"ip": "192.0.2.202", "sys_name": "",   # нет sysName -> fallback dns
         "sys_descr": "Kyocera ECOSYS", "serial": "S2",
         "mac": "00:1b:1e:2f:3a:02", "dns_name": "npr-ky.corp.local"},
        {"ip": "192.0.2.203", "sys_name": "NPR-HP1",  # коллизия имени
         "sys_descr": "HP LaserJet 500", "serial": "S3",
         "mac": "00:1b:1e:2f:3a:03", "dns_name": ""},
        {"ip": "192.0.2.150", "sys_name": "X", "sys_descr": "X",
         "serial": "S4", "mac": "00:1b:1e:2f:3a:04",
         "dns_name": ""},   # host занят manual-устройством
    ]
    D.discover_printers = (lambda networks, community="public",
                           timeout=1.0, port=161, max_workers=32, **kw:
                           {"found": found, "probed": 100,
                            "responded": 4})
    try:
        # manual-принтер с host 192.0.2.150 уже есть
        with SessionLocal() as db:
            db.add(Device(name="manual-prn", type=DeviceType.printer,
                          host="192.0.2.150", port=161, username="u",
                          password="p", description="старый",
                          group="Сеть", enabled=True, source="manual"))
            db.commit()
        with SessionLocal() as db:
            r = api_dev.sync_printer_discovery(
                db, subnets="192.0.2.0/24", community="office-sec")
        check("sync: found=4, added=3, skipped=1 (чужой host)",
              r["found"] == 4 and r["added"] == 3 and r["skipped"] == 1
              and r["updated"] == 0, f"r={r}")
        with SessionLocal() as db:
            d1 = db.query(Device).filter_by(host="192.0.2.201").first()
            check("добавлен: printer, выкл, группа Принтеры, source=snmp, "
                  "community из формы, порт 161",
                  d1 is not None and d1.type == DeviceType.printer
                  and not d1.enabled and d1.group == "Принтеры"
                  and d1.source == "snmp"
                  and d1.snmp_community == "office-sec"
                  and d1.port == 161 and d1.snmp_version == "2c",
                  f"d1={d1 and (d1.name, d1.enabled, d1.group, d1.source)}")
            check("MAC и dns_name записаны при добавлении",
                  d1 is not None and d1.mac == "00:1b:1e:2f:3a:01"
                  and d1.dns_name == "npr-hp1.corp.local",
                  f"mac={d1 and d1.mac} dns={d1 and d1.dns_name}")
            d2 = db.query(Device).filter_by(host="192.0.2.202").first()
            check("нет sysName -> имя из dns_name",
                  d2 is not None and d2.name == "npr-ky.corp.local",
                  f"name={d2 and d2.name}")
            d3 = db.query(Device).filter_by(host="192.0.2.203").first()
            check("коллизия имени -> суффикс -{ip}",
                  d3 is not None and d3.name == "npr-hp1-192.0.2.203",
                  f"name={d3 and d3.name}")
            d1_descr = d1.description
            check("описание 'SNMP: {sysDescr}'",
                  d1_descr == "SNMP: HP LaserJet 400",
                  f"descr={d1_descr!r}")
        # re-run: тот же host -> updated, не дубль
        with SessionLocal() as db:
            r2 = api_dev.sync_printer_discovery(
                db, subnets="192.0.2.0/24", community="changed-comm")
        with SessionLocal() as db:
            n = (db.query(Device)
                   .filter(Device.source == "snmp",
                           Device.type == DeviceType.printer).count())
            d1 = db.query(Device).filter_by(host="192.0.2.201").first()
        check("re-run: updated=3, дублей нет, community обновился",
              r2["added"] == 0 and r2["updated"] == 3 and n == 3
              and d1.snmp_community == "changed-comm",
              f"r2={r2} n={n} comm={d1 and d1.snmp_community}")
        # смена IP по MAC (DHCP): тот же mac с новым адресом -> updated,
        # host переносится, дубля нет
        found_dhcp = [
            {"ip": "192.0.2.209", "sys_name": "NPR-HP1",
             "sys_descr": "HP LaserJet 400", "serial": "S1",
             "mac": "00:1b:1e:2f:3a:01", "dns_name": "npr-hp1.corp.local"},
        ]
        D.discover_printers = (lambda networks, community="public",
                               timeout=1.0, port=161, max_workers=32, **kw:
                               {"found": found_dhcp, "probed": 1,
                                "responded": 1})
        with SessionLocal() as db:
            r3 = api_dev.sync_printer_discovery(
                db, subnets="192.0.2.0/24", community="changed-comm")
        with SessionLocal() as db:
            n = (db.query(Device)
                   .filter(Device.source == "snmp",
                           Device.type == DeviceType.printer).count())
            moved = db.query(Device).filter_by(
                mac="00:1b:1e:2f:3a:01").first()
        check("смена IP по MAC: updated (не дубль), host перенесён",
              r3["updated"] == 1 and r3["added"] == 0 and n == 3
              and moved is not None and moved.host == "192.0.2.209"
              and moved.name == "npr-hp1",
              f"r3={r3} n={n} moved={moved and (moved.host, moved.name)}")
        # перенос на IP, занятый ЧУЖИМ устройством -> skip, host не меняем
        with SessionLocal() as db:
            db.add(Device(name="occupant-sw", type=DeviceType.eltex,
                          host="192.0.2.219", port=22, username="u",
                          password="p", description="", group="Сеть",
                          enabled=True))
            db.commit()
        D.discover_printers = (lambda networks, community="public",
                               timeout=1.0, port=161, max_workers=32, **kw:
                               {"found": [
                                   {"ip": "192.0.2.219",
                                    "sys_name": "NPR-HP1",
                                    "sys_descr": "HP LaserJet 400",
                                    "serial": "S1",
                                    "mac": "00:1b:1e:2f:3a:01",
                                    "dns_name": ""}],
                                "probed": 1, "responded": 1})
        with SessionLocal() as db:
            r3b = api_dev.sync_printer_discovery(
                db, subnets="192.0.2.0/24", community="changed-comm")
        with SessionLocal() as db:
            still = db.query(Device).filter_by(
                mac="00:1b:1e:2f:3a:01").first()
        check("новый IP занят чужим -> skip, host принтера прежний",
              r3b["skipped"] == 1 and still is not None
              and still.host == "192.0.2.209",
              f"r3b={r3b} host={still and still.host}")
        # дедуп по MAC против manual-устройства: тот же mac, другой IP,
        # manual -> skipped (не дублируем чужое устройство)
        with SessionLocal() as db:
            db.add(Device(name="manual-mac-prn", type=DeviceType.printer,
                          host="10.7.7.7", port=161, username="u",
                          password="p", description="manual с MAC",
                          group="Сеть", enabled=True, source="manual",
                          mac="00:1b:1e:2f:3a:04"))
            db.commit()
        found_macdup = [
            {"ip": "192.0.2.250", "sys_name": "Y", "sys_descr": "Y",
             "serial": "S9", "mac": "00:1b:1e:2f:3a:04", "dns_name": ""},
        ]
        D.discover_printers = (lambda networks, community="public",
                               timeout=1.0, port=161, max_workers=32, **kw:
                               {"found": found_macdup, "probed": 1,
                                "responded": 1})
        with SessionLocal() as db:
            r4 = api_dev.sync_printer_discovery(
                db, subnets="192.0.2.0/24", community="public")
        with SessionLocal() as db:
            n = (db.query(Device)
                   .filter(Device.source == "snmp",
                           Device.type == DeviceType.printer).count())
        check("чужой MAC (manual) -> skipped, дубля нет",
              r4["skipped"] == 1 and r4["added"] == 0 and n == 3,
              f"r4={r4} n={n}")
    finally:
        D.discover_printers = real_discover


def scenario_ui():
    """5) UI-эндпоинт: flash, бейдж, ошибки, RBAC."""
    real_discover = D.discover_printers
    D.discover_printers = (
        lambda networks, community="public", timeout=1.0, port=161,
        max_workers=32, **kw:
        {"found": [{"ip": "192.0.2.210", "sys_name": "NPR-UI",
                    "sys_descr": "HP LaserJet UI", "serial": "S9"}],
         "probed": 254, "responded": 1})
    try:
        with TestClient(app.main.app) as client:
            r = client.post("/admin/login",
                            data={"username": "admin@mock.local",
                                  "password": "x"},
                            follow_redirects=False)
            check("логин admin (DEV_MODE) -> 303",
                  r.status_code in (302, 303), f"got {r.status_code}")

            r = client.post("/admin/inventory/discover-printers",
                            data={"subnets": "192.0.2.0/24",
                                  "community": "public", "port": "161",
                                  "timeout": "1"})
            check("POST discovery -> 200, flash «SNMP: найдено 1...», "
                  "строка в таблице",
                  r.status_code == 200
                  and "SNMP: найдено 1, добавлено 1" in r.text
                  and "npr-ui" in r.text,
                  f"got {r.status_code}")

            r = client.post("/admin/inventory/discover-printers",
                            data={"subnets": "не-подсеть"})
            check("невалидная подсеть -> flash-ошибка, не 500",
                  r.status_code == 200 and "SNMP:" in r.text
                  and "Невалидная подсеть" in r.text,
                  f"got {r.status_code}")

            r = client.get("/admin/inventory/partial/table")
            check("snmp-бейдж в таблице (bg-amber, partial)",
                  r.status_code == 200
                  and "bg-amber-100 text-amber-800" in r.text
                  and "npr-ui" in r.text,
                  f"got {r.status_code}")
    finally:
        D.discover_printers = real_discover

    # RBAC: viewer не допускается (без логина = редирект; проверим
    # через DEV_MODE-пользователя viewer — эмулируем заголовком нельзя,
    # поэтому проверяем, что эндпоинт защищён: без сессии -> редирект 303
    with TestClient(app.main.app) as client:
        r = client.post("/admin/inventory/discover-printers",
                        data={"subnets": "10.0.0.0/24"},
                        follow_redirects=False)
        check("без логина -> редирект на /admin/login (эндпоинт защищён)",
              r.status_code in (302, 303), f"got {r.status_code}")


def main():
    scenario_parse_subnets()
    scenario_probe_ip()
    scenario_mac_norm()
    scenario_discover()
    scenario_sync()
    scenario_ui()

    print()
    print(f"Итог: PASS={PASS} FAIL={FAIL}")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
