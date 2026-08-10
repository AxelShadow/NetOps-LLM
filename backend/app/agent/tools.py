"""Инструменты агента: схемы для LLM + выполнение + аудит."""
import json
import logging
import platform
import re
import subprocess
import datetime as dt

from ..devices.zabbix import ZabbixClient

from ..db import SessionLocal
from ..models import Device, DeviceType, AuditLog
from ..devices.vmware import get_adapter, drop_adapter

log = logging.getLogger(__name__)

MAX_RESULT = 20000          # обрезаем слишком длинные выводы
_HOST_RE = re.compile(r"^[a-zA-Z0-9.\-:_]+$")


class ToolError(Exception):
    pass


VMWARE_TOOLS = ("vmware_vms", "vmware_hosts", "vmware_snapshots",
                "vmware_datastores", "vmware_vm_disks",
                "vmware_host_networks", "vmware_vm_networks",
                "vmware_host_sensors", "vmware_events")

TOOLS_SCHEMA = [
    {"type": "function", "function": {
        "name": "ping",
        "description": "Проверить доступность хоста по ICMP (4 пакета)",
        "parameters": {"type": "object",
                       "properties": {"host": {"type": "string",
                                               "description": "IP-адрес или имя"}},
                       "required": ["host"]}}},
    {"type": "function", "function": {
        "name": "vmware_vms",
        "description": "Список виртуальных машин: питание, IP, CPU, память",
        "parameters": {"type": "object",
                       "properties": {"device": {"type": "string",
                                                 "description": "Имя vCenter/ESXi из инвентаря или 'all' — все VMware-устройства"},
                                      "host": {"type": "string",
                                               "description": "Имя ESXi-хоста — фильтр (необязательно)"}, },
                       "required": ["device"]}}},
    {"type": "function", "function": {
        "name": "vmware_hosts",
        "description": "ESXi-хосты: состояние, загрузка CPU и памяти",
        "parameters": {"type": "object",
                       "properties": {"device": {"type": "string", "description": "Имя vCenter/ESXi из инвентаря или 'all' — все VMware-устройства"}},
                       "required": ["device"]}}},
    {"type": "function", "function": {
        "name": "vmware_snapshots",
        "description": "ВМ, у которых есть снапшоты (имена, количество, даты)",
        "parameters": {"type": "object",
                       "properties": {"device": {"type": "string"}},
                       "required": ["device"]}}},
    {"type": "function", "function": {
        "name": "vmware_datastores",
        "description": "Датасторы: ёмкость, занято, свободно, подключённые хосты",
        "parameters": {"type": "object",
                       "properties": {"device": {"type": "string", "description": "Имя vCenter/ESXi из инвентаря или 'all' — все VMware-устройства"},
                                      "host": {"type": "string",
                                               "description": "Имя ESXi-хоста — фильтр (необязательно)"}, },
                       "required": ["device"]}}},
    {"type": "function", "function": {
        "name": "vmware_vm_disks",
        "description": "Диски ВМ: провизированный размер VMDK и занятость внутри "
                       "гостевой ОС. Можно для одной ВМ",
        "parameters": {"type": "object",
                       "properties": {
                           "device": {"type": "string", "description": "Имя vCenter/ESXi из инвентаря или 'all' — все VMware-устройства"},
                           "vm": {"type": "string",
                                  "description": "Имя ВМ (необязательно; по умолчанию все)"},
                           "host": {"type": "string",
                                    "description": "Имя ESXi-хоста — фильтр (необязательно)"}, },
                       "required": ["device"]}}},
    {"type": "function", "function": {
        "name": "vmware_host_networks",
        "description": "Сетевые интерфейсы ESXi-хостов: vmnic (скорость/MAC), "
                       "vmk (IP), vSwitch и портгруппы",
        "parameters": {"type": "object",
                       "properties": {"device": {"type": "string", "description": "Имя vCenter/ESXi из инвентаря или 'all' — все VMware-устройства"},
                                      "host": {"type": "string",
                                               "description": "Имя vCenter/ESXi-хоста — фильтр (необязательно)"}, },
                       "required": ["device"]}}},
    {"type": "function", "function": {
        "name": "vmware_vm_networks",
        "description": "Сетевые интерфейсы ВМ: MAC, портгруппа, IP-адреса. "
                       "Можно для одной ВМ",
        "parameters": {"type": "object",
                       "properties": {
                           "device": {"type": "string", "description": "Имя vCenter/ESXi из инвентаря или 'all' — все VMware-устройства"},
                           "vm": {"type": "string",
                                  "description": "Имя ВМ (необязательно)"}},
                       "required": ["device"]}}},
    {"type": "function", "function": {
        "name": "vmware_events",
        "description": "Журнал событий vCenter/ESXi: включения ВМ, отключения "
                       "хостов, ошибки, действия пользователей. Можно фильтровать "
                       "по ВМ или хосту",
        "parameters": {"type": "object",
                       "properties": {
                           "device": {"type": "string", "description": "Имя vCenter/ESXi из инвентаря или 'all' — все VMware-устройства"},
                           "hours": {"type": "integer",
                                     "description": "Глубина просмотра в часах, по умолчанию 24"},
                           "entity": {"type": "string",
                                      "description": "Имя ВМ или хоста (необязательно)"},
                           "max_count": {"type": "integer",
                                         "description": "Максимум событий, по умолчанию 100"}},
                       "required": ["device"]}}},
    {"type": "function", "function": {
        "name": "vmware_host_sensors",
        "description": "Аппаратные сенсоры ESXi: статусы дисков и контроллеров, "
                       "проблемные датчики. Показывает, какой именно диск/компонент "
                       "в ошибке",
        "parameters": {"type": "object",
                       "properties": {
                           "device": {"type": "string", "description": "Имя vCenter/ESXi из инвентаря или 'all' — все VMware-устройства"},
                           "host": {"type": "string",
                                    "description": "Имя ESXi-хоста (необязательно)"}},
                       "required": ["device"]}}},
    {"type": "function", "function": {
        "name": "zabbix_problems",
        "description": "Активные проблемы из Zabbix (сработавшие триггеры): время, "
                       "хост, описание, важность. Без device — по всей инфраструктуре",
        "parameters": {"type": "object",
                       "properties": {
                           "device": {"type": "string",
                                      "description": "Имя устройства из инвентаря (необязательно)"},
                           "limit": {"type": "integer"}},
                       "required": []}}},
    {"type": "function", "function": {
        "name": "zabbix_items",
        "description": "Последние значения метрик Zabbix для устройства. Можно "
                       "фильтровать по ключевому слову: CPU, память, интерфейс, temperature",
        "parameters": {"type": "object",
                       "properties": {
                           "device": {"type": "string"},
                           "search": {"type": "string",
                                      "description": "Поиск по имени метрики (необязательно)"},
                           "limit": {"type": "integer"}},
                       "required": ["device"]}}},
    {"type": "function", "function": {
        "name": "zabbix_history",
        "description": "История значений метрики Zabbix за период (динамика, тренд)",
        "parameters": {"type": "object",
                       "properties": {
                           "device": {"type": "string"},
                           "item": {"type": "string",
                                    "description": "Имя или itemid метрики (узнать через zabbix_items)"},
                           "hours": {"type": "integer",
                                     "description": "Глубина в часах, по умолчанию 24"}},
                       "required": ["device", "item"]}}},
]

_SEVERITY = ["Not classified", "Information", "Warning", "Average",
             "High", "Disaster"]


def _get_zbx_hostid(name: str) -> str:
    d = _get_device(name)
    if not d.zabbix_hostid:
        raise ToolError(f"'{d.name}' не является устройством из Zabbix")
    return d.zabbix_hostid


def _format_problems(problems: list) -> list:
    out = []
    for p in problems:
        out.append({
            "time": dt.datetime.fromtimestamp(int(p["clock"])).strftime("%Y-%m-%d %H:%M"),
            "host": p.get("host", ""),
            "problem": p.get("name", ""),
            "severity": _SEVERITY[int(p.get("severity", 0))],
            "ack": bool(int(p.get("acknowledged", 0))),
        })
    return out


def _format_items(items: list) -> list:
    out = []
    for i in items:
        value = i.get("lastvalue") or ""
        if i.get("units"):
            value = f"{value} {i['units']}".strip()
        out.append({
            "name": i.get("name", ""),
            "value": value,
            "itemid": i.get("itemid", ""),
            "changed": dt.datetime.fromtimestamp(int(i.get("clock", 0))).strftime("%Y-%m-%d %H:%M")
            if i.get("clock") else "",
        })
    return out


def _find_item(hostid: str, query: str) -> dict:
    zc = ZabbixClient()
    query = (query or "").strip()
    if query.isdigit():
        found = zc.call("item.get", {
            "output": ["itemid", "name", "value_type", "units"],
            "itemids": [query]})
    else:
        found = zc.get_items([hostid], search=query, limit=5)
    if not found:
        raise ToolError(
            f"Метрика '{query}' не найдена — уточните имя через zabbix_items")
    return found[0]


def _format_history(rows: list, units: str) -> list:
    out = [{"time": dt.datetime.fromtimestamp(int(r["clock"])).strftime("%Y-%m-%d %H:%M"),
            "value": (r["value"] + (" " + units if units else "")).strip()}
           for r in rows]
    return list(reversed(out))   # хронологический порядок


def _ping(host: str) -> str:
    if not _HOST_RE.match(host or ""):
        raise ToolError("Запрещено: некорректный адрес")
    flag = "-n" if platform.system() == "Windows" else "-c"
    r = subprocess.run(["ping", flag, "4", host],
                       capture_output=True, text=True, timeout=60)
    return (r.stdout + r.stderr).strip()


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _get_device(name: str) -> Device:
    wanted = (name or "").strip().lower()
    with SessionLocal() as db:
        devices = db.query(Device).filter(Device.enabled.is_(True)).all()

    available = ", ".join(d.name for d in devices) or "инвентарь пуст"

    for d in devices:                        # точное имя
        if d.name.lower() == wanted:
            return d
    for d in devices:                        # по хосту/IP
        if d.host.lower() == wanted:
            return d
    short = wanted.split(".")[0]             # первый ярлык FQDN
    for d in devices:
        if d.name.lower() == short:
            return d

    raise ToolError(f"Устройство '{name}' не найдено в инвентаре. "
                    f"Доступные устройства: {available}. "
                    f"Если это ESXi-хост или ВМ под управлением vCenter — "
                    f"укажите device=vcenter и используйте фильтры host/vm/entity.")


def _vmware(device: Device, name: str, args: dict) -> str:
    def call():
        ad = get_adapter(device)
        if name == "vmware_vms":
            data = ad.get_vms(args.get("host"))
        elif name == "vmware_hosts":
            data = ad.get_hosts()
        elif name == "vmware_snapshots":
            data = ad.get_snapshots()
        elif name == "vmware_datastores":
            data = ad.get_datastores(args.get("host"))
        elif name == "vmware_vm_disks":
            data = ad.get_vm_disks(args.get("vm"), args.get("host"))
        elif name == "vmware_host_networks":
            data = ad.get_host_networks()
        elif name == "vmware_vm_networks":
            data = ad.get_vm_networks(args.get("vm"), args.get("host"))
        elif name == "vmware_host_sensors":
            data = ad.get_host_sensors(args.get("host"))
        elif name == "vmware_events":
            data = ad.get_events(
                hours=min(_int(args.get("hours"), 24), 24 * 30),
                entity_name=args.get("entity"),
                max_count=min(_int(args.get("max_count"), 100), 500))
        else:
            raise ToolError(f"Неизвестный VMware-инструмент: {name}")
        return data

    try:
        data = call()
    except Exception as e:
        log.warning("VMware: %s — повторяю с новой сессией", e)
        drop_adapter(device)
        data = call()

    # Подсказка при пустом результате с фильтром host
    host = args.get("host")
    if host and isinstance(data, list) and not data:
        with SessionLocal() as db:
            others = db.query(Device).filter(
                Device.enabled.is_(True), Device.id != device.id).all()
        wanted = host.strip().lower()
        match = next((d for d in others
                      if d.name.lower() == wanted.split(".")[0]
                      or d.host.lower() == wanted), None)
        if match:
            return json.dumps({
                "result": [],
                "note": (f"Пусто: '{host}' не управляется '{device.name}'. "
                         f"В инвентаре есть самостоятельное устройство "
                         f"'{match.name}' — повтори вызов с device='{match.name}'.")
            }, ensure_ascii=False)

    return json.dumps(data, ensure_ascii=False, default=str)


def execute_tool(name: str, args: dict,
                 user_id: int | None = None,
                 conversation_id: int | None = None) -> tuple[str, str]:
    status = "ok"
    try:
        if name == "ping":
            result = _ping(args.get("host", ""))
        elif name in VMWARE_TOOLS:
            device_query = (args.get("device") or "").strip()
            if device_query.lower() in ("all", "все", "*"):
                with SessionLocal() as db:
                    vm_devices = db.query(Device).filter(
                        Device.enabled.is_(True),
                        Device.type.in_([DeviceType.vcenter,
                                         DeviceType.esxi])).all()
                if not vm_devices:
                    raise ToolError("В инвентаре нет VMware-устройств")
                aggregated = {}
                for d in vm_devices:
                    try:
                        aggregated[d.name] = json.loads(_vmware(d, name, args))
                    except Exception as e:
                        aggregated[d.name] = {"error": str(e)}
                result = json.dumps(aggregated, ensure_ascii=False,
                                    default=str)

            else:
                device = _get_device(device_query)
                if device.type not in (DeviceType.vcenter, DeviceType.esxi):
                    raise ToolError(
                        f"'{device.name}' не является VMware-устройством")
                result = _vmware(device, name, args)
        elif name == "zabbix_problems":
            hostids = None
            if args.get("device"):
                hostids = [_get_zbx_hostid(args["device"])]
            problems = ZabbixClient().get_problems(
                hostids, limit=min(_int(args.get("limit"), 30), 200))
            result = json.dumps(_format_problems(problems),
                                ensure_ascii=False, default=str)
        elif name == "zabbix_items":
            hostid = _get_zbx_hostid(args.get("device", ""))
            items = ZabbixClient().get_items(
                [hostid], search=args.get("search"),
                limit=min(_int(args.get("limit"), 50), 200))
            result = json.dumps(_format_items(items),
                                ensure_ascii=False, default=str)
        elif name == "zabbix_history":
            hostid = _get_zbx_hostid(args.get("device", ""))
            item = _find_item(hostid, args.get("item", ""))
            hours = min(_int(args.get("hours"), 24), 24 * 7)
            rows = ZabbixClient().get_history(
                item["itemid"], int(item["value_type"]), hours)
            result = json.dumps(_format_history(rows, item.get("units") or ""),
                                ensure_ascii=False, default=str)
    except ToolError as e:
        result, status = str(e), "denied" if "Запрещено" in str(e) else "error"
    except Exception as e:
        log.exception("Ошибка инструмента %s", name)
        result, status = f"Ошибка выполнения: {e}", "error"

    result = result[:MAX_RESULT]
    _audit(user_id, conversation_id, name, args, result, status)
    return result, status


def _audit(user_id, conversation_id, tool, args, result, status):
    try:
        with SessionLocal() as db:
            db.add(AuditLog(user_id=user_id, conversation_id=conversation_id,
                            tool=tool,
                            arguments=json.dumps(args, ensure_ascii=False),
                            result=result[:4000], status=status))
            db.commit()
    except Exception:
        log.exception("Не удалось записать аудит")
