"""Инструменты агента: реестр, схемы для LLM + выполнение + аудит."""
import json
import logging
import platform
import re
import subprocess
import datetime as dt
from typing import Optional, Any
from collections import defaultdict

from cachetools import TTLCache

from ..devices.zabbix import ZabbixClient
from ..db import SessionLocal
from ..models import Device, DeviceType, AuditLog
from ..devices.vmware import get_adapter, drop_adapter

log = logging.getLogger(__name__)

MAX_RESULT = 20000          # обрезаем слишком длинные выводы
_HOST_RE = re.compile(r"^[a-zA-Z0-9.\-:_]+$")
_SEVERITY = ["Not classified", "Information",
             "Warning", "Average", "High", "Disaster"]


# ============================================================
# Реестр инструментов (Tool Registry)
# ============================================================

_registry = {}
_caches = {}


def register_tool(
    name: str,
    description: str,
    parameters: dict,
    cache_ttl: int = 0,
    roles: list[str] = None,
    is_composite: bool = False
):
    """Декоратор для регистрации инструмента с кэшем и RBAC."""
    def decorator(func):
        if cache_ttl > 0:
            _caches[name] = TTLCache(maxsize=100, ttl=cache_ttl)

        def wrapper(**kwargs):
            # Проверка кэша
            if cache_ttl > 0:
                try:
                    cache_key = json.dumps(
                        {"k": kwargs}, sort_keys=True, default=str)
                except TypeError:
                    cache_key = str(kwargs)

                if cache_key in _caches[name]:
                    return _caches[name][cache_key]

            result = func(**kwargs)

            # Запись в кэш только успешных результатов
            if cache_ttl > 0:
                if isinstance(result, tuple) and len(result) == 2:
                    _, status = result
                    if status not in ("error", "denied", "exception"):
                        _caches[name][cache_key] = result
                else:
                    _caches[name][cache_key] = result
            return result

        wrapper.tool_name = name
        wrapper.tool_description = description
        wrapper.tool_parameters = parameters
        wrapper.tool_roles = roles or ["viewer", "engineer", "admin"]
        wrapper.tool_cache_ttl = cache_ttl
        wrapper.is_composite = is_composite

        _registry[name] = wrapper
        return wrapper
    return decorator


def get_tools_schema() -> list[dict]:
    """Генерирует JSON Schema для OpenAI API из реестра."""
    schema = []
    for name, func in _registry.items():
        schema.append({
            "type": "function",
            "function": {
                "name": name,
                "description": func.tool_description,
                "parameters": func.tool_parameters
            }
        })
    return schema


# ============================================================
# Вспомогательные функции
# ============================================================

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

    for d in devices:
        if d.name.lower() == wanted:
            return d
    for d in devices:
        if d.host.lower() == wanted:
            return d
    short = wanted.split(".")[0]
    for d in devices:
        if d.name.lower() == short:
            return d

    raise Exception(f"Устройство '{name}' не найдено в инвентаре. "
                    f"Доступные устройства: {available}. "
                    f"Если это ESXi-хост или ВМ под управлением vCenter — "
                    f"укажите device=vcenter и используйте фильтры host/vm/entity.")


def _get_zbx_hostid(name: str) -> str:
    d = _get_device(name)
    if not d.zabbix_hostid:
        raise Exception(f"'{d.name}' не является устройством из Zabbix")
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
        raise Exception(
            f"Метрика '{query}' не найдена — уточните имя через zabbix_items")
    return found[0]


def _format_history(rows: list, units: str) -> list:
    out = [{"time": dt.datetime.fromtimestamp(int(r["clock"])).strftime("%Y-%m-%d %H:%M"),
            "value": (r["value"] + (" " + units if units else "")).strip()}
           for r in rows]
    return list(reversed(out))


def _ping_cmd(host: str) -> str:
    if not _HOST_RE.match(host or ""):
        raise Exception("Запрещено: некорректный адрес")
    flag = "-n" if platform.system() == "Windows" else "-c"
    r = subprocess.run(["ping", flag, "4", host],
                       capture_output=True, text=True, timeout=60)
    return (r.stdout + r.stderr).strip()


def _vmware(device: Device, name: str, args: dict) -> str:
    method_map = {
        "vmware_vms": lambda ad: ad.get_vms(args.get("host")),
        "vmware_hosts": lambda ad: ad.get_hosts(),
        "vmware_snapshots": lambda ad: ad.get_snapshots(),
        "vmware_datastores": lambda ad: ad.get_datastores(args.get("host")),
        "vmware_vm_disks": lambda ad: ad.get_vm_disks(args.get("vm"), args.get("host")),
        "vmware_host_networks": lambda ad: ad.get_host_networks(),
        "vmware_vm_networks": lambda ad: ad.get_vm_networks(args.get("vm"), args.get("host")),
        "vmware_host_sensors": lambda ad: ad.get_host_sensors(args.get("host")),
        "vmware_events": lambda ad: ad.get_events(
            hours=min(_int(args.get("hours"), 24), 24 * 30),
            entity_name=args.get("entity"),
            max_count=min(_int(args.get("max_count"), 100), 500))
    }

    def call():
        ad = get_adapter(device)
        if name not in method_map:
            raise Exception(f"Неизвестный VMware-инструмент: {name}")
        return method_map[name](ad)

    try:
        data = call()
    except Exception as e:
        log.warning("VMware: %s — повторяю с новой сессией", e)
        drop_adapter(device)
        data = call()

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


def _resolve_vmware_devices(device_query: str):
    if device_query.lower() in ("all", "все", "*"):
        with SessionLocal() as db:
            vm_devices = db.query(Device).filter(
                Device.enabled.is_(True),
                Device.type.in_([DeviceType.vcenter, DeviceType.esxi])).all()
        if not vm_devices:
            raise Exception("В инвентаре нет VMware-устройств")
        return vm_devices, True
    else:
        device = _get_device(device_query)
        if device.type not in (DeviceType.vcenter, DeviceType.esxi):
            raise Exception(f"'{device.name}' не является VMware-устройством")
        return [device], False


def _execute_vmware_tool(name: str, args: dict) -> tuple[str, str]:
    device_query = (args.get("device") or "").strip()
    vm_devices, is_all = _resolve_vmware_devices(device_query)

    if is_all:
        aggregated = {}
        for d in vm_devices:
            try:
                aggregated[d.name] = json.loads(_vmware(d, name, args))
            except Exception as e:
                aggregated[d.name] = {"error": str(e)}
        return json.dumps(aggregated, ensure_ascii=False, default=str), "ok"
    else:
        return _vmware(vm_devices[0], name, args), "ok"


def _extract_vmware_list(json_str: str) -> list:
    """Извлекает список из ответа VMware-инструмента (учитывает разные форматы)."""
    try:
        data = json.loads(json_str) if isinstance(json_str, str) else json_str
    except (json.JSONDecodeError, TypeError):
        return []

    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if "result" in data and isinstance(data["result"], list):
            return data["result"]
        if "data" in data and isinstance(data["data"], list):
            return data["data"]
        # Агрегированный формат device="all": {"vcenter": [...], "esxi": [...]}
        for v in data.values():
            if isinstance(v, list):
                return v
    return []


def _safe_float(value) -> float | None:
    """Безопасное преобразование в float."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _get_free_percent(ds: dict) -> float | None:
    """Вычисляет процент свободного места на датасторе."""
    # Вариант 1: адаптер уже возвращает free_percent
    if "free_percent" in ds:
        return _safe_float(ds["free_percent"])
    # Вариант 2: считаем из capacity и free_space
    capacity = _safe_float(ds.get("capacity") or ds.get(
        "capacity_bytes") or ds.get("capacity_gb"))
    free = _safe_float(ds.get("free_space") or ds.get(
        "free_space_bytes") or ds.get("free_gb"))
    if capacity and free is not None and capacity > 0:
        return (free / capacity) * 100
    return None
# ============================================================
# Инструменты (обёрнутые в @register_tool)
# ============================================================


@register_tool(
    name="get_current_time",
    description="Возвращает текущие дату и время сервера. Использовать для расчёта длительности инцидентов.",
    parameters={"type": "object", "properties": {}, "required": []},
    cache_ttl=0
)
def get_current_time():
    now = dt.datetime.now()
    return json.dumps({
        "iso": now.isoformat(),
        "human_readable": now.strftime("%Y-%m-%d %H:%M:%S"),
        "timezone": str(now.astimezone().tzinfo)
    }, ensure_ascii=False), "ok"


@register_tool(
    name="ping",
    description="Проверить доступность хоста по ICMP (4 пакета)",
    parameters={"type": "object",
                "properties": {"host": {"type": "string", "description": "IP-адрес или имя"}},
                "required": ["host"]},
    cache_ttl=60
)
def ping(host: str):
    raw_output = _ping_cmd(host)
    # Эвристика доступности: ищем TTL (Windows) или 0% потерь (Linux)
    is_reachable = (
        "TTL=" in raw_output.upper() or
        "0% packet loss" in raw_output.lower() or
        "ttl=" in raw_output.lower()
    )
    return json.dumps({
        "host": host,
        "reachable": is_reachable,
        "output": raw_output
    }, ensure_ascii=False), "ok"


@register_tool(
    name="list_devices",
    description="Список всех устройств в инвентаре с их типами и описаниями",
    parameters={"type": "object", "properties": {}},
    cache_ttl=300
)
def list_devices():
    with SessionLocal() as db:
        rows = db.query(Device).filter(Device.enabled.is_(True)).all()
        return json.dumps(
            [{"name": d.name, "type": d.type.value,
              "description": d.description, "group": d.group}
             for d in rows],
            ensure_ascii=False), "ok"


@register_tool(
    name="list_groups",
    description="Список групп устройств и какие устройства входят в каждую группу. Помогает понять структуру инвентаря",
    parameters={"type": "object", "properties": {}},
    cache_ttl=300
)
def list_groups():
    with SessionLocal() as db:
        devices = db.query(Device).filter(Device.enabled.is_(True)).all()
        groups = defaultdict(list)
        for d in devices:
            groups[d.group or "Без группы"].append({
                "name": d.name,
                "type": d.type.value,
                "description": d.description or d.host,
            })
        return json.dumps(dict(groups), ensure_ascii=False), "ok"


# --- VMware Tools ---
vmware_props = {"device": {"type": "string",
                           "description": "Имя vCenter/ESXi из инвентаря или 'all' — все VMware-устройства"}}
vmware_host_filter = {"host": {"type": "string",
                               "description": "Имя ESXi-хоста — фильтр (необязательно)"}}
vmware_vm_filter = {"vm": {"type": "string",
                           "description": "Имя ВМ (необязательно; по умолчанию все)"}}


@register_tool(name="vmware_vms", description="Список виртуальных машин: питание, IP, CPU, память",
               parameters={"type": "object", "properties": {**vmware_props, **vmware_host_filter}, "required": ["device"]}, cache_ttl=120)
def vmware_vms(device: str, host: str = None):
    return _execute_vmware_tool("vmware_vms", {"device": device, "host": host})


@register_tool(name="vmware_hosts", description="ESXi-хосты: состояние, загрузка CPU и памяти",
               parameters={"type": "object", "properties": vmware_props, "required": ["device"]}, cache_ttl=120)
def vmware_hosts(device: str):
    return _execute_vmware_tool("vmware_hosts", {"device": device})


@register_tool(name="vmware_snapshots", description="ВМ, у которых есть снапшоты (имена, количество, даты)",
               parameters={"type": "object", "properties": vmware_props, "required": ["device"]}, cache_ttl=120)
def vmware_snapshots(device: str):
    return _execute_vmware_tool("vmware_snapshots", {"device": device})


@register_tool(name="vmware_datastores", description="Датасторы: ёмкость, занято, свободно, подключённые хосты",
               parameters={"type": "object", "properties": {**vmware_props, **vmware_host_filter}, "required": ["device"]}, cache_ttl=300)
def vmware_datastores(device: str, host: str = None):
    return _execute_vmware_tool("vmware_datastores", {"device": device, "host": host})


@register_tool(name="vmware_vm_disks", description="Диски ВМ: провизированный размер VMDK и занятость внутри гостевой ОС. Можно для одной ВМ",
               parameters={"type": "object", "properties": {**vmware_props, **vmware_vm_filter, **vmware_host_filter}, "required": ["device"]}, cache_ttl=300)
def vmware_vm_disks(device: str, vm: str = None, host: str = None):
    return _execute_vmware_tool("vmware_vm_disks", {"device": device, "vm": vm, "host": host})


@register_tool(name="vmware_host_networks", description="Сетевые интерфейсы ESXi-хостов: vmnic (скорость/MAC), vmk (IP), vSwitch и портгруппы",
               parameters={"type": "object", "properties": {**vmware_props, **vmware_host_filter}, "required": ["device"]}, cache_ttl=300)
def vmware_host_networks(device: str, host: str = None):
    return _execute_vmware_tool("vmware_host_networks", {"device": device, "host": host})


@register_tool(name="vmware_vm_networks", description="Сетевые интерфейсы ВМ: MAC, портгруппа, IP-адреса. Можно для одной ВМ",
               parameters={"type": "object", "properties": {**vmware_props, **vmware_vm_filter}, "required": ["device"]}, cache_ttl=300)
def vmware_vm_networks(device: str, vm: str = None):
    return _execute_vmware_tool("vmware_vm_networks", {"device": device, "vm": vm})


@register_tool(name="vmware_events", description="Журнал событий vCenter/ESXi: включения ВМ, отключения хостов, ошибки, действия пользователей. Можно фильтровать по ВМ или хосту",
               parameters={"type": "object", "properties": {
                   **vmware_props,
                   "hours": {"type": "integer", "description": "Глубина просмотра в часах, по умолчанию 24"},
                   "entity": {"type": "string", "description": "Имя ВМ или хоста (необязательно)"},
                   "max_count": {"type": "integer", "description": "Максимум событий, по умолчанию 100"}
               }, "required": ["device"]}, cache_ttl=60)
def vmware_events(device: str, hours: int = 24, entity: str = None, max_count: int = 100):
    return _execute_vmware_tool("vmware_events", {"device": device, "hours": hours, "entity": entity, "max_count": max_count})


@register_tool(name="vmware_host_sensors", description="Аппаратные сенсоры ESXi: статусы дисков и контроллеров, проблемные датчики. Показывает, какой именно диск/компонент в ошибке",
               parameters={"type": "object", "properties": {**vmware_props, **vmware_host_filter}, "required": ["device"]}, cache_ttl=120)
def vmware_host_sensors(device: str, host: str = None):
    return _execute_vmware_tool("vmware_host_sensors", {"device": device, "host": host})


# --- Zabbix Tools ---
@register_tool(name="zabbix_problems", description="Активные проблемы из Zabbix (сработавшие триггеры): время, хост, описание, важность. Без device — по всей инфраструктуре",
               parameters={"type": "object", "properties": {
                   "device": {"type": "string", "description": "Имя устройства из инвентаря (необязательно)"},
                   "limit": {"type": "integer"}
               }, "required": []}, cache_ttl=60)
def zabbix_problems(device: str = None, limit: int = 30):
    hostids = None
    if device:
        hostids = [_get_zbx_hostid(device)]
    problems = ZabbixClient().get_problems(hostids, limit=min(_int(limit, 30), 200))
    return json.dumps(_format_problems(problems), ensure_ascii=False, default=str), "ok"


@register_tool(name="zabbix_items", description="Последние значения метрик Zabbix для устройства. Можно фильтровать по ключевому слову: CPU, память, интерфейс, temperature",
               parameters={"type": "object", "properties": {
                   "device": {"type": "string"},
                   "search": {"type": "string", "description": "Поиск по имени метрики (необязательно)"},
                   "limit": {"type": "integer"}
               }, "required": ["device"]}, cache_ttl=120)
def zabbix_items(device: str, search: str = None, limit: int = 50):
    hostid = _get_zbx_hostid(device)
    items = ZabbixClient().get_items(
        [hostid], search=search, limit=min(_int(limit, 50), 200))
    return json.dumps(_format_items(items), ensure_ascii=False, default=str), "ok"


@register_tool(name="zabbix_history", description="История значений метрики Zabbix за период (динамика, тренд)",
               parameters={"type": "object", "properties": {
                   "device": {"type": "string"},
                   "item": {"type": "string", "description": "Имя или itemid метрики (узнать через zabbix_items)"},
                   "hours": {"type": "integer", "description": "Глубина в часах, по умолчанию 24"}
               }, "required": ["device", "item"]}, cache_ttl=60)
def zabbix_history(device: str, item: str, hours: int = 24):
    hostid = _get_zbx_hostid(device)
    item_data = _find_item(hostid, item)
    h = min(_int(hours, 24), 24 * 7)
    rows = ZabbixClient().get_history(
        item_data["itemid"], int(item_data["value_type"]), h)
    return json.dumps(_format_history(rows, item_data.get("units") or ""), ensure_ascii=False, default=str), "ok"


# --- Composite Tools ---
@register_tool(
    name="get_device_full_health",
    description="Полная диагностика одного устройства: ping, Zabbix-алерты, датасторы, CPU/RAM, снапшоты, события.",
    parameters={"type": "object", "properties": {
        "device": {"type": "string", "description": "Точное имя устройства из инвентаря"}
    }, "required": ["device"]},
    cache_ttl=120, is_composite=True
)
def get_device_full_health(device: str):
    dev = _get_device(device)
    results = {"ping": {}, "zabbix": {}, "vmware": {}}

    # 1. Ping
    try:
        res, status = ping(host=dev.host)
        results["ping"] = json.loads(res) if status == "ok" else {"error": res}
    except Exception as e:
        results["ping"] = {"error": str(e)}

    # 2. Zabbix: один вызов с фильтром по устройству
    try:
        if dev.zabbix_hostid:
            res, status = zabbix_problems(device=dev.name, limit=50)
            results["zabbix"] = json.loads(
                res) if status == "ok" else {"error": res}
        else:
            results["zabbix"] = {"note": "Не мониторится в Zabbix"}
    except Exception as e:
        results["zabbix"] = {"error": str(e)}

    # 3. VMware: только если это vCenter или ESXi
    if dev.type in (DeviceType.vcenter, DeviceType.esxi):
        # Датасторы
        try:
            res, _ = vmware_datastores(device=dev.name)
            ds_list = _extract_vmware_list(res)
            low_space = [
                {"name": d.get("name"), "free_percent": round(
                    _get_free_percent(d), 1)}
                for d in ds_list if _get_free_percent(d) is not None and _get_free_percent(d) < 15
            ]
            results["vmware"]["datastores_low_space"] = low_space
        except Exception as e:
            results["vmware"]["datastores_error"] = str(e)

        # Хосты (загрузка)
        try:
            res, _ = vmware_hosts(device=dev.name)
            hosts_list = _extract_vmware_list(res)
            high_load = [
                {"host": h.get("name"),
                 "cpu": _safe_float(h.get("cpu_usage_percent") or h.get("cpu_percent")),
                 "mem": _safe_float(h.get("memory_usage_percent") or h.get("memory_percent"))}
                for h in hosts_list
                if (_safe_float(h.get("cpu_usage_percent") or h.get("cpu_percent")) or 0) > 85
                or (_safe_float(h.get("memory_usage_percent") or h.get("memory_percent")) or 0) > 85
            ]
            results["vmware"]["hosts_high_load"] = high_load
        except Exception as e:
            results["vmware"]["hosts_error"] = str(e)

        # Снапшоты
        try:
            res, _ = vmware_snapshots(device=dev.name)
            snaps_list = _extract_vmware_list(res)
            vms_with_snaps = [
                {"vm": s.get("name"), "count": len(s.get("snapshots", []))}
                for s in snaps_list if s.get("snapshots")
            ]
            results["vmware"]["vms_with_snapshots"] = vms_with_snaps[:10]
        except Exception as e:
            results["vmware"]["snapshots_error"] = str(e)

        # События за 24ч (последние 15)
        try:
            res, _ = vmware_events(device=dev.name, hours=24, max_count=15)
            events = _extract_vmware_list(res)
            results["vmware"]["recent_events"] = events[:15]
        except Exception as e:
            results["vmware"]["events_error"] = str(e)

    # Итоговый статус
    status = "Green"
    if results["ping"].get("reachable") is False:
        status = "Red"
    elif isinstance(results["zabbix"], list) and len(results["zabbix"]) > 0:
        status = "Red"
    elif results["vmware"].get("datastores_low_space") or results["vmware"].get("hosts_high_load"):
        status = "Yellow"
    elif results["vmware"].get("vms_with_snapshots"):
        status = "Yellow"

    report = {
        "device": dev.name,
        "type": dev.type.value,
        "status": status,
        "details": results,
    }
    return json.dumps(report, ensure_ascii=False, default=str), "ok"

# --- Infrastructure-wide Health Check ---


@register_tool(
    name="get_infrastructure_health",
    description="Полный обзор проблем: алерты Zabbix (один запрос), свободное место на датасторах, перегрузка CPU/RAM хостов, старые снапшоты. Вызывать на 'что болит'.",
    parameters={"type": "object", "properties": {}, "required": []},
    cache_ttl=120,
    is_composite=True
)
def get_infrastructure_health():
    """
    Стратегия:
    1. Zabbix — ОДИН вызов problem.get без фильтра (все алерты инфраструктуры).
    2. VMware — опрашиваем ТОЛЬКО управляющие устройства из инвентаря.
       Если vCenter управляет хостами, standalone-хосты под ним НЕ опрашиваем отдельно.
    3. Инвентарь — только для определения какие VMware-устройства опрашивать.
    """
    report = {
        "timestamp": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "summary": {
            "total_active_devices": 0,
            "zabbix_alerts": 0,
            "datastore_low_space": 0,
            "host_high_load": 0,
            "vms_with_snapshots": 0,
        },
        "zabbix_problems": [],
        "vmware_issues": {
            "datastores_low_space": [],
            "hosts_high_load": [],
            "vms_with_snapshots": [],
        },
        "inventory_note": "",
    }

    # ============================================================
    # 1. Инвентарь: получаем активные устройства (БЕЗ опроса каждого)
    # ============================================================
    with SessionLocal() as db:
        all_devices = db.query(Device).filter(Device.enabled.is_(True)).all()
    report["summary"]["total_active_devices"] = len(all_devices)

    # Определяем VMware-устройства для опроса.
    # Логика: если есть vCenter, опрашиваем его (он отдаст данные по всем хостам).
    # Standalone ESXi опрашиваем отдельно только если они НЕ под vCenter.
    vmware_devices = [d for d in all_devices if d.type in (
        DeviceType.vcenter, DeviceType.esxi)]

    # Если есть vCenter, standalone-хосты под ним не опрашиваем отдельно
    has_vcenter = any(d.type == DeviceType.vcenter for d in vmware_devices)
    if has_vcenter:
        # Оставляем только vCenter + standalone ESXi (которые не управляются vCenter)
        # В вашей инфраструктуре: vcenter + vmh08 (standalone)
        # vmh03, vmh05 — под vCenter, их отдельно не опрашиваем
        vmware_devices = [
            d for d in vmware_devices if d.type == DeviceType.vcenter]
        # Добавляем standalone ESXi, которые НЕ в списке хостов vCenter
        # (в вашем случае vmh08 — standalone, его нужно опросить)
        standalone_esxi = [d for d in all_devices if d.type == DeviceType.esxi]
        vmware_devices.extend(standalone_esxi)

    # ============================================================
    # 2. Zabbix: ОДИН вызов, без device (все алерты инфраструктуры)
    # ============================================================
    try:
        problems_raw, status = zabbix_problems(limit=100)
        if status == "ok":
            problems = json.loads(problems_raw)
            report["zabbix_problems"] = problems
            report["summary"]["zabbix_alerts"] = len(problems)
    except Exception as e:
        report["zabbix_problems"] = [{"error": str(e)}]

    # ============================================================
    # 3. VMware: опрашиваем управляющие устройства (vCenter / standalone ESXi)
    # ============================================================
    for dev in vmware_devices:
        try:
            # --- Датасторы: ищем те, где мало места ---
            res, _ = vmware_datastores(device=dev.name)
            ds_data = _extract_vmware_list(res)
            for ds in ds_data:
                free_pct = _get_free_percent(ds)
                if free_pct is not None and free_pct < 15:
                    report["vmware_issues"]["datastores_low_space"].append({
                        "source": dev.name,
                        "datastore": ds.get("name", "unknown"),
                        "free_percent": round(free_pct, 1),
                    })
                    report["summary"]["datastore_low_space"] += 1
        except Exception as e:
            log.warning("datastores %s: %s", dev.name, e)

        try:
            # --- Хосты: ищем перегруженные по CPU/RAM ---
            res, _ = vmware_hosts(device=dev.name)
            hosts_data = _extract_vmware_list(res)
            for h in hosts_data:
                cpu = _safe_float(h.get("cpu_usage_percent") or h.get(
                    "cpu_percent") or h.get("cpu_usage"))
                mem = _safe_float(h.get("memory_usage_percent") or h.get(
                    "memory_percent") or h.get("mem_usage"))
                if (cpu and cpu > 85) or (mem and mem > 85):
                    report["vmware_issues"]["hosts_high_load"].append({
                        "source": dev.name,
                        "host": h.get("name", "unknown"),
                        "cpu_percent": round(cpu, 1) if cpu else None,
                        "mem_percent": round(mem, 1) if mem else None,
                    })
                    report["summary"]["host_high_load"] += 1
        except Exception as e:
            log.warning("hosts %s: %s", dev.name, e)

        try:
            # --- Снапшоты: ВМ со снапшотами (риск разрастания) ---
            res, _ = vmware_snapshots(device=dev.name)
            snaps_data = _extract_vmware_list(res)
            for vm in snaps_data:
                snap_list = vm.get("snapshots") or []
                if snap_list:
                    report["vmware_issues"]["vms_with_snapshots"].append({
                        "source": dev.name,
                        "vm": vm.get("name", "unknown"),
                        "snapshot_count": len(snap_list),
                        "oldest": snap_list[0].get("created", "") if snap_list else "",
                    })
                    report["summary"]["vms_with_snapshots"] += 1
        except Exception as e:
            log.warning("snapshots %s: %s", dev.name, e)

    # Финальная заметка для модели
    total_issues = sum(
        v for k, v in report["summary"].items() if k != "total_active_devices")
    if total_issues == 0:
        report["inventory_note"] = "Проблем не обнаружено. Инфраструктура в норме."
    else:
        report["inventory_note"] = f"Обнаружено проблемных областей: {total_issues}."

    return json.dumps(report, ensure_ascii=False, default=str), "ok"

# ============================================================
# Экспорт и execute_tool
# ============================================================


TOOLS_SCHEMA = get_tools_schema()


def execute_tool(name: str, args: dict, user_id: int | None = None,
                 conversation_id: int | None = None, user_role: str = "viewer") -> tuple[str, str]:
    """Единая точка входа с проверкой прав и аудитом."""
    status = "ok"
    func = _registry.get(name)

    if not func:
        result = f"Инструмент '{name}' не найден в системе."
        status = "error"
    elif user_role not in func.tool_roles:
        result = f"Недостаточно прав (роль: {user_role}) для выполнения '{name}'."
        status = "denied"
    else:
        try:
            result, status = func(**args)
            if len(result) > MAX_RESULT:
                result = result[:MAX_RESULT]
        except Exception as e:
            log.exception("Ошибка инструмента %s", name)
            if "Запрещено" in str(e):
                result, status = str(e), "denied"
            else:
                result, status = f"Ошибка выполнения: {e}", "error"

    # Аудит-лог
    try:
        with SessionLocal() as db:
            db.add(AuditLog(user_id=user_id, conversation_id=conversation_id,
                            tool=name,
                            arguments=json.dumps(args, ensure_ascii=False),
                            result=(result or "")[:4000], status=status))
            db.commit()
    except Exception:
        log.exception("Не удалось записать аудит")

    return result, status
