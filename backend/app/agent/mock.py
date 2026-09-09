"""Мок-данные инструментов для NETOPS_MOCK_MODE=true.

Возвращает фейковые реалистичные данные в том же формате, что и реальные
инструменты (кортеж (json-строка, status)), без сетевых вызовов.

Не мокаются (локальные, работают от БД): get_current_time, list_devices,
list_groups.

Сценарий ошибки для smoke-теста: если в аргументах device/host равен
"mock-ошибка" — бросаем Exception, чтобы execute_tool записал в аудит
status="error".
"""
import datetime as dt
import json

MOCK_ERROR_MARKER = "mock-ошибка"


def _json(data) -> tuple[str, str]:
    return json.dumps(data, ensure_ascii=False, default=str), "ok"


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def _check_error(args: dict):
    """Маркер ошибки: device/host == 'mock-ошибка' — имитируем сбой инструмента."""
    for key in ("device", "host", "vm", "item"):
        if str(args.get(key) or "").strip().lower() == MOCK_ERROR_MARKER:
            raise Exception("Mock-ошибка инструмента (запрошена тестом)")


# ------------------------------------------------------------
# ping
# ------------------------------------------------------------

def mock_ping(args: dict) -> tuple[str, str]:
    _check_error(args)
    host = args.get("host") or args.get("device") or "mock-host"
    return _json({
        "host": host,
        "reachable": True,
        "output": (
            f"Обмен пакетами с {host} по 32 байт:\n"
            "Ответ от 192.0.2.10: число байт=32 время=1мс TTL=63\n"
            "Ответ от 192.0.2.10: число байт=32 время=1мс TTL=63\n"
            "Ответ от 192.0.2.10: число байт=32 время=2мс TTL=63\n"
            "Ответ от 192.0.2.10: число байт=32 время=1мс TTL=63\n\n"
            "Статистика Ping для 192.0.2.10:\n"
            "Отправлено = 4, получено = 4, потеряно = 0 (0% потерь)")
    })


# ------------------------------------------------------------
# VMware
# ------------------------------------------------------------

def mock_vmware_vms(args: dict) -> tuple[str, str]:
    _check_error(args)
    return _json([
        {"name": "srv-app-01", "power": "poweredOn", "host": "esxi-01",
         "ip": "192.0.2.21", "cpu_count": 4,
         "memory_gb": 16, "used_gb": 10.5, "cpu_percent": 12.3},
        {"name": "srv-db-01", "power": "poweredOn", "host": "esxi-02",
         "ip": "192.0.2.22", "cpu_count": 8,
         "memory_gb": 32, "used_gb": 24.1, "cpu_percent": 41.7},
        {"name": "srv-web-01", "power": "poweredOn", "host": "esxi-01",
         "ip": "192.0.2.23", "cpu_count": 2,
         "memory_gb": 8, "used_gb": 3.9, "cpu_percent": 5.1},
        {"name": "test-vm-01", "power": "poweredOff", "host": "esxi-02",
         "ip": None, "cpu_count": 2, "memory_gb": 4, "used_gb": 0,
         "cpu_percent": 0},
    ])


def mock_vmware_hosts(args: dict) -> tuple[str, str]:
    _check_error(args)
    return _json([
        {"name": "esxi-01", "state": "connected", "cpu_percent": 18.2,
         "mem_used_gb": 96, "mem_total_gb": 256,
         "vms_count": 14, "uptime_days": 127},
        {"name": "esxi-02", "state": "connected", "cpu_percent": 35.4,
         "mem_used_gb": 178, "mem_total_gb": 256,
         "vms_count": 22, "uptime_days": 89},
        {"name": "esxi-03", "state": "connected", "cpu_percent": 7.9,
         "mem_used_gb": 51, "mem_total_gb": 128,
         "vms_count": 6, "uptime_days": 210},
    ])


def mock_vmware_snapshots(args: dict) -> tuple[str, str]:
    _check_error(args)
    return _json([
        {"name": "srv-db-01", "snapshots": [
            {"name": "Перед обновлением", "date": "2026-08-28 14:20",
             "size_gb": 12.4},
            {"name": "Ежедневный бэкап", "date": "2026-09-01 03:00",
             "size_gb": 3.1},
        ]},
        {"name": "srv-app-01", "snapshots": [
            {"name": "Тестовое изменение конфига", "date": "2026-08-30 09:45",
             "size_gb": 1.2},
        ]},
    ])


def mock_vmware_datastores(args: dict) -> tuple[str, str]:
    _check_error(args)
    return _json([
        {"name": "datastore-ssd-01", "capacity_gb": 1900, "free_gb": 812,
         "free_percent": 42.7, "type": "VMFS 6",
         "hosts": ["esxi-01", "esxi-02"]},
        {"name": "datastore-nas-01", "capacity_gb": 4000, "free_gb": 348,
         "free_percent": 8.7, "type": "NFS",
         "hosts": ["esxi-01", "esxi-02", "esxi-03"]},
        {"name": "datastore-local-01", "capacity_gb": 480, "free_gb": 156,
         "free_percent": 32.5, "type": "VMFS 6", "hosts": ["esxi-03"]},
    ])


def mock_vmware_vm_disks(args: dict) -> tuple[str, str]:
    _check_error(args)
    return _json([
        {"vm": "srv-db-01", "disk": "Hard disk 1", "vmdk_gb": 200,
         "guest_used_gb": 154, "guest_free_gb": 46},
        {"vm": "srv-app-01", "disk": "Hard disk 1", "vmdk_gb": 60,
         "guest_used_gb": 38, "guest_free_gb": 22},
    ])


def mock_vmware_host_networks(args: dict) -> tuple[str, str]:
    _check_error(args)
    return _json([
        {"host": "esxi-01",
         "vmnics": [
             {"name": "vmnic0", "speed_mb": 10000, "mac": "00:0C:29:AA:BB:01"},
             {"name": "vmnic1", "speed_mb": 10000, "mac": "00:0C:29:AA:BB:02"},
         ],
         "vmks": [{"name": "vmk0", "ip": "192.0.2.2",
                   "portgroup": "Management Network"}],
         "vswitches": [
             {"name": "vSwitch0", "vmnics": ["vmnic0", "vmnic1"],
              "portgroups": ["VM Network", "Management Network"]}]},
    ])


def mock_vmware_vm_networks(args: dict) -> tuple[str, str]:
    _check_error(args)
    return _json([
        {"vm": "srv-app-01", "mac": "00:50:56:AA:01:23",
         "portgroup": "VM Network", "ips": ["192.0.2.21"]},
        {"vm": "srv-db-01", "mac": "00:50:56:AA:01:24",
         "portgroup": "VM Network", "ips": ["192.0.2.22"]},
    ])


def mock_vmware_events(args: dict) -> tuple[str, str]:
    _check_error(args)
    return _json([
        {"time": _now(), "host": "esxi-02", "type": "info",
         "message": "esxi-02.local: Питание ВМ srv-db-01 включено"},
        {"time": _now(), "host": "esxi-01", "type": "warning",
         "message": "Датастор datastore-nas-01: свободно менее 10%"},
        {"time": _now(), "host": "esxi-03", "type": "error",
         "message": "esxi-03.local: потеря связи с датастором datastore-nas-01"},
    ])


def mock_vmware_host_sensors(args: dict) -> tuple[str, str]:
    _check_error(args)
    return _json([
        {"host": "esxi-02",
         "system": "green",
         "disks": [
             {"name": "Disk0", "status": "green"},
             {"name": "Disk1", "status": "red",
              "detail": "SMART error, predicted failure"},
         ],
         "sensors": [
             {"name": "System Board 1 Temp", "status": "green", "value": 28},
             {"name": "Power Supply 1", "status": "green"},
         ]},
    ])


# ------------------------------------------------------------
# Zabbix
# ------------------------------------------------------------

def mock_zabbix_problems(args: dict) -> tuple[str, str]:
    _check_error(args)
    return _json([
        {"time": "2026-09-02 08:14", "host": "esxi-02",
         "problem": "Высокая загрузка CPU (> 85% за 5 минут)",
         "severity": "Average", "ack": False},
        {"time": "2026-09-01 22:03", "host": "srv-db-01",
         "problem": "Свободное место на диске < 10%",
         "severity": "Warning", "ack": True},
    ])


def mock_zabbix_items(args: dict) -> tuple[str, str]:
    _check_error(args)
    return _json([
        {"name": "CPU utilization", "value": "41.7 %",
         "itemid": "10012", "changed": "2026-09-02 10:00"},
        {"name": "Available memory", "value": "12.3 GB",
         "itemid": "10013", "changed": "2026-09-02 10:00"},
        {"name": "Interface eth0: bits received", "value": "128.4 Kbps",
         "itemid": "10014", "changed": "2026-09-02 10:00"},
    ])


def mock_zabbix_history(args: dict) -> tuple[str, str]:
    _check_error(args)
    base = dt.datetime.now()
    rows = []
    for i in range(1, 13):
        t = base - dt.timedelta(hours=i)
        rows.append({
            "time": t.strftime("%Y-%m-%d %H:%M"),
            "value": f"{35 + (i % 7) * 3.2:.1f} %",
        })
    return _json(rows)


# ------------------------------------------------------------
# SNMP (Этап SNMP: snmp_info / snmp_interfaces / snmp_walk / printer_info)
# ------------------------------------------------------------

def mock_snmp_info(args: dict) -> tuple[str, str]:
    _check_error(args)
    return _json({
        "device": args.get("device") or "mock-sw",
        "host": "192.0.2.10",
        "sysDescr": "Mock SNMP device (Eltex OS 4.2.1)",
        "sysContact": "IT-отдел",
        "sysName": "mock-sw",
        "sysLocation": "Серверная, стойка 3",
        "uptime": "3 д 4 ч 5 мин",
    })


def mock_snmp_interfaces(args: dict) -> tuple[str, str]:
    _check_error(args)
    return _json({
        "device": args.get("device") or "mock-sw",
        "interfaces": [
            {"index": 1, "name": "eth0", "mtu": 1500, "speed_bps": 1000000000,
             "admin_status": "up", "oper_status": "up"},
            {"index": 2, "name": "lo", "mtu": 65536, "speed_bps": 0,
             "admin_status": "up", "oper_status": "up"},
        ],
    })


def mock_snmp_walk(args: dict) -> tuple[str, str]:
    _check_error(args)
    rows = [
        ["1.3.6.1.2.1.1.1.0", "Mock SNMP device"],
        ["1.3.6.1.2.1.1.3.0", 123456],
        ["1.3.6.1.2.1.1.5.0", "mock-sw"],
    ]
    return _json({"device": args.get("device") or "mock-sw", "rows": rows})


def mock_printer_info(args: dict) -> tuple[str, str]:
    _check_error(args)
    device = (args.get("device") or "").strip().lower()
    if device in ("all", "все", "*"):
        return _json({
            "hq-printer-1": {
                "device": "hq-printer-1", "host": "192.0.2.150",
                "units": [{"unit": 1, "serial": "MOCK12345",
                           "pages_printed": 12345,
                           "toner_level_percent": 42.0,
                           "status": "printing", "errors": []}],
                "toner_supplies": [{"supply": 1, "level": 4200,
                                    "max_capacity": 10000,
                                    "percent": 42.0}],
                "pages_scanned": None,
                "note_scanned": "Счётчик сканированных страниц вендорозависим — "
                                "стандартный Printer-MIB его не даёт; нужен "
                                "enterprise-MIB модели (HP/Kyocera и т.п.). "
                                "Распечатанные страницы — поле pages_printed "
                                "из prtMarkerLifeCount.",
            },
            "branch-printer-2": {
                "device": "branch-printer-2", "host": "192.0.2.151",
                "units": [{"unit": 1, "serial": "MOCK67890",
                           "pages_printed": 67890,
                           "toner_level_percent": 8.0,
                           "status": "idle", "errors": ["lowPaper"]}],
                "toner_supplies": [{"supply": 1, "level": 800,
                                    "max_capacity": 10000,
                                    "percent": 8.0}],
                "pages_scanned": None,
                "note_scanned": "Счётчик сканированных страниц вендорозависим — "
                                "стандартный Printer-MIB его не даёт; нужен "
                                "enterprise-MIB модели (HP/Kyocera и т.п.). "
                                "Распечатанные страницы — поле pages_printed "
                                "из prtMarkerLifeCount.",
            },
        })
    return _json({
        "device": args.get("device") or "hq-printer-1",
        "host": "192.0.2.150",
        "units": [{"unit": 1, "serial": "MOCK12345",
                   "pages_printed": 12345,
                   "toner_level_percent": 42.0,
                   "status": "printing", "errors": []}],
        "toner_supplies": [{"supply": 1, "level": 4200, "max_capacity": 10000,
                            "percent": 42.0}],
        "pages_scanned": None,
        "note_scanned": "Счётчик сканированных страниц вендорозависим — "
                        "стандартный Printer-MIB его не даёт; нужен "
                        "enterprise-MIB модели (HP/Kyocera и т.п.). "
                        "Распечатанные страницы — поле pages_printed "
                        "из prtMarkerLifeCount.",
    })


# ------------------------------------------------------------
# Композитные
# ------------------------------------------------------------

def mock_get_device_full_health(args: dict) -> tuple[str, str]:
    _check_error(args)
    return _json({
        "device": args.get("device") or "mock-device",
        "type": "vcenter",
        "status": "Yellow",
        "details": {
            "ping": {"host": "192.0.2.1", "reachable": True},
            "zabbix": [
                {"time": "2026-09-02 08:14", "host": "esxi-02",
                 "problem": "Высокая загрузка CPU", "severity": "Average",
                 "ack": False},
            ],
            "vmware": {
                "datastores_low_space": [
                    {"name": "datastore-nas-01", "free_percent": 8.7}],
                "hosts_high_load": [],
                "vms_with_snapshots": [
                    {"vm": "srv-db-01", "count": 2}],
                "recent_events": [
                    {"time": _now(), "type": "warning",
                     "message": "Мало места на datastore-nas-01"}],
            },
        },
    })


def mock_get_infrastructure_health(args: dict) -> tuple[str, str]:
    _check_error(args)
    return _json({
        "timestamp": _now(),
        "summary": {
            "total_active_devices": 6,
            "zabbix_alerts": 2,
            "datastore_low_space": 1,
            "host_high_load": 1,
            "vms_with_snapshots": 2,
        },
        "zabbix_problems": [
            {"time": "2026-09-02 08:14", "host": "esxi-02",
             "problem": "Высокая загрузка CPU", "severity": "Average",
             "ack": False},
            {"time": "2026-09-01 22:03", "host": "srv-db-01",
             "problem": "Свободное место на диске < 10%", "severity": "Warning",
             "ack": True},
        ],
        "vmware_issues": {
            "datastores_low_space": [
                {"name": "datastore-nas-01", "free_percent": 8.7}],
            "hosts_high_load": [
                {"name": "esxi-02", "cpu_percent": 85.1,
                 "mem_used_gb": 178, "mem_total_gb": 256}],
            "vms_with_snapshots": [
                {"vm": "srv-db-01", "count": 2},
                {"vm": "srv-app-01", "count": 1}],
        },
        "inventory_note": "Мок-режим: данные сгенерированы без опроса инфраструктуры.",
    })


def mock_get_printers_report(args: dict) -> tuple[str, str]:
    return _json({
        "summary": {"total": 3, "healthy": 1, "with_problems": 1,
                    "unreachable": 1},
        "printers": [
            {"name": "hq-printer-1", "host": "192.0.2.150",
             "status": "printing", "pages_printed": 12345,
             "toner_percent": 42.0, "errors": [], "serial": "MOCK12345"},
            {"name": "branch-printer-2", "host": "192.0.2.151",
             "status": "idle", "pages_printed": 987,
             "toner_percent": 8.0, "errors": ["lowToner"],
             "serial": "MOCK67890"},
        ],
        "remaining_healthy": [],
        "unreachable": [
            {"name": "storage-printer-3", "error":
             "SnmpError: WALK 192.0.2.152: No SNMP response received "
             "before timeout"},
        ],
        "note": "Одна строка на принтер; подробности по конкретному — "
                "printer_info.",
    })


# Словарь: имя инструмента -> мок-функция (args: dict) -> (json-строка, status)
MOCK_TOOLS = {
    "ping": mock_ping,
    "vmware_vms": mock_vmware_vms,
    "vmware_hosts": mock_vmware_hosts,
    "vmware_snapshots": mock_vmware_snapshots,
    "vmware_datastores": mock_vmware_datastores,
    "vmware_vm_disks": mock_vmware_vm_disks,
    "vmware_host_networks": mock_vmware_host_networks,
    "vmware_vm_networks": mock_vmware_vm_networks,
    "vmware_events": mock_vmware_events,
    "vmware_host_sensors": mock_vmware_host_sensors,
    "zabbix_problems": mock_zabbix_problems,
    "zabbix_items": mock_zabbix_items,
    "zabbix_history": mock_zabbix_history,
    "snmp_info": mock_snmp_info,
    "snmp_interfaces": mock_snmp_interfaces,
    "snmp_walk": mock_snmp_walk,
    "printer_info": mock_printer_info,
    "get_device_full_health": mock_get_device_full_health,
    "get_infrastructure_health": mock_get_infrastructure_health,
    "get_printers_report": mock_get_printers_report,
}
