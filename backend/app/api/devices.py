"""CRUD инвентаря устройств."""
import re
from ..config import get_settings
from ..devices.zabbix import ZabbixClient
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session


from ..db import get_db
from ..models import Device, DeviceType, User
from ..auth.deps import get_current_user, require_admin
from ..devices.vmware import clear_cache

router = APIRouter(prefix="/api/devices")


class DeviceIn(BaseModel):
    name: str
    type: DeviceType
    host: str
    port: int = 0
    username: str = ""
    password: str = ""
    enabled: bool = True
    description: str = ""


class DevicePatch(BaseModel):
    name: str | None = None
    type: DeviceType | None = None
    host: str | None = None
    port: int | None = None
    username: str | None = None
    password: str | None = None    # не задан или пуст = оставить старый пароль
    enabled: bool | None = None
    description: str | None = None


class BulkPatch(BaseModel):
    enabled: bool
    ids: list[int] | None = None
    group: str | None = None


def _public(d: Device) -> dict:
    """Сериализация БЕЗ пароля — он никогда не должен уходить в клиент."""
    return {"id": d.id, "name": d.name, "type": d.type.value,
            "host": d.host, "port": d.port, "username": d.username,
            "enabled": d.enabled, "description": d.description,
            "source": d.source, "group": d.group}


@router.get("")
def list_devices(db: Session = Depends(get_db),
                 _user: User = Depends(get_current_user)):
    devices = db.query(Device).order_by(Device.name).all()
    return [_public(d) for d in devices]


@router.post("")
def create_device(data: DeviceIn, db: Session = Depends(get_db),
                  _admin: User = Depends(require_admin)):
    name = data.name.strip().lower()
    source = "manual"
    if db.query(Device).filter(Device.name == name).first():
        raise HTTPException(409, "Устройство с таким именем уже есть")
    d = Device(name=name, type=data.type, host=data.host.strip(),
               port=data.port, username=data.username.strip(),
               password=data.password, enabled=data.enabled,
               description=data.description)
    db.add(d)
    db.commit()
    db.refresh(d)
    clear_cache()
    return _public(d)


@router.patch("/bulk")
def bulk_update(data: BulkPatch, db: Session = Depends(get_db),
                _admin: User = Depends(require_admin)):
    q = db.query(Device)
    if data.ids:
        q = q.filter(Device.id.in_(data.ids))
    elif data.group:
        q = q.filter(Device.group == data.group)
    else:
        raise HTTPException(400, "Укажите ids или group")
    n = q.update({Device.enabled: data.enabled}, synchronize_session=False)
    db.commit()
    clear_cache()
    return {"updated": n}


def _norm_name(s: str) -> str:
    name = re.sub(r"[^0-9A-Za-zА-Яа-яЁё._-]+", "-", s.strip().lower())
    return name.strip("-") or "host"


def _primary_address(interfaces: list) -> str:
    if not interfaces:
        return ""
    mains = [i for i in interfaces if str(i.get("main")) == "1"]
    chosen = (mains or interfaces)[0]
    if str(chosen.get("useip")) == "1" and chosen.get("ip"):
        return chosen["ip"]
    return chosen.get("dns") or chosen.get("ip") or ""


@router.post("/sync-zabbix")
def sync_zabbix(db: Session = Depends(get_db),
                _admin: User = Depends(require_admin)):
    s = get_settings()
    if not s.zabbix_url or not s.zabbix_token:
        raise HTTPException(
            400, "Zabbix не настроен: NETOPS_ZABBIX_URL и NETOPS_ZABBIX_TOKEN")
    try:
        hosts = ZabbixClient().get_hosts()
    except Exception as e:
        raise HTTPException(502, f"Ошибка запроса к Zabbix: {e}")

    zbx_devices = {d.zabbix_hostid: d for d in
                   db.query(Device).filter(Device.source == "zabbix").all()}
    taken_names = {d.name: d.id for d in db.query(Device).all()}
    seen: set[str] = set()
    added = updated = 0

    for h in hosts:
        hid = str(h["hostid"])
        seen.add(hid)
        groups = h.get("hostgroups") or h.get("groups") or []
        group = ", ".join(g["name"] for g in groups)
        display = h.get("name") or h["host"]
        name = _norm_name(h["host"])
        owner = taken_names.get(name)
        existing = zbx_devices.get(hid)
        if owner is not None and (existing is None or existing.id != owner):
            name = f"{name}-{hid}"
        address = _primary_address(h.get("interfaces") or []) or h["host"]
        fields = dict(name=name, host=address, source="zabbix",
                      zabbix_hostid=hid, group=group,
                      description=f"Zabbix: {display}")
        if existing:
            for k, v in fields.items():
                setattr(existing, k, v)
            updated += 1
        else:
            db.add(Device(type=DeviceType.other, enabled=False, **fields))
            taken_names[name] = -1
            added += 1

    disabled = 0
    for hid, d in zbx_devices.items():
        if hid not in seen and d.enabled:
            d.enabled = False
            disabled += 1

    db.commit()
    clear_cache()
    return {"added": added, "updated": updated,
            "disabled_gone": disabled, "total_zabbix": len(hosts)}


def sync_printer_discovery(db: Session, *, subnets: str,
                           community: str = "public", timeout: float = 1.0,
                           port: int = 161) -> dict:
    """SNMP-Discovery принтеров (Этап 19): поиск по подсетям, добавление
    в инвентарь выключенными (source="snmp", группа «Принтеры») —
    зеркало sync_zabbix. Пропавшие НЕ отключаем: принтер мог просто
    выключиться; discovery = «найти новое и обновить найденное».
    Чистая функция (без Depends): вызывается из UI-роутера.
    """
    from ..devices.printer_discovery import discover_printers, parse_subnets

    networks = parse_subnets(subnets)
    try:
        disc = discover_printers(networks, community=community,
                                 timeout=timeout, port=port)
    except Exception as e:
        raise HTTPException(502, f"Ошибка SNMP-обзора сети: {e}")

    snmp_existing = {d.host: d for d in db.query(Device).filter(
        Device.source == "snmp",
        Device.type == DeviceType.printer).all()}
    snmp_by_mac = {d.mac: d for d in snmp_existing.values() if d.mac}
    taken_names = {d.name: d.id for d in db.query(Device).all()}
    added = updated = skipped = 0

    for p in disc["found"]:
        ip = p["ip"]
        sys_descr = p["sys_descr"] or ""
        mac = p.get("mac")
        dns_name = p.get("dns_name") or ""
        existing = snmp_existing.get(ip) or (snmp_by_mac.get(mac)
                                             if mac else None)
        if existing is not None:
            # Найдено прошлым discovery (по IP или MAC — DHCP сменил
            # адрес). Перенос на новый IP только если адрес свободен:
            # чужое устройство с этим host не затираем.
            if existing.host != ip:
                occupant = db.query(Device).filter(
                    Device.host == ip,
                    Device.id != existing.id).first()
                if occupant is not None:
                    skipped += 1   # новый IP занят чужим устройством
                    continue
                snmp_existing.pop(existing.host, None)
                existing.host = ip
                snmp_existing[ip] = existing
            # Имя в инвентаре НЕ трогаем — его мог переименовать
            # пользователь.
            existing.snmp_community = community
            existing.description = f"SNMP: {sys_descr[:100]}"
            existing.mac = mac or existing.mac
            existing.dns_name = dns_name
            updated += 1
            continue
        if (db.query(Device).filter(Device.host == ip).first() is not None
                or (mac and db.query(Device).filter(
                    Device.mac == mac).first() is not None)):
            skipped += 1   # чужой host или MAC — не дублируем
            continue
        name = _norm_name(p["sys_name"] or dns_name or f"printer-{ip}")
        if name in taken_names:
            name = f"{name}-{ip}"
        db.add(Device(
            type=DeviceType.printer, enabled=False,
            host=ip, port=port, group="Принтеры", source="snmp",
            snmp_version="2c", snmp_community=community,
            name=name, mac=mac, dns_name=dns_name,
            description=f"SNMP: {sys_descr[:100]}"))
        taken_names[name] = -1
        added += 1

    db.commit()
    clear_cache()
    return {"found": len(disc["found"]), "probed": disc["probed"],
            "responded": disc["responded"], "added": added,
            "updated": updated, "skipped": skipped}


@router.patch("/{device_id}")
def update_device(device_id: int, data: DevicePatch,
                  db: Session = Depends(get_db),
                  _admin: User = Depends(require_admin)):
    d = db.get(Device, device_id)
    if d.source == "zabbix" and data.enabled is None:
        raise HTTPException(
            400, "Устройства из Zabbix можно только включать/выключать")
    if not d:
        raise HTTPException(404, "Устройство не найдено")
    if data.name is not None:
        name = data.name.strip().lower()
        busy = db.query(Device).filter(Device.name == name,
                                       Device.id != device_id).first()
        if busy:
            raise HTTPException(409, "Имя уже занято")
        d.name = name
    if data.type is not None:
        d.type = data.type
    if data.host is not None:
        d.host = data.host.strip()
    if data.port is not None:
        d.port = data.port
    if data.username is not None:
        d.username = data.username.strip()
    if data.password:                     # пароль меняем только если задан
        d.password = data.password
    if data.enabled is not None:
        d.enabled = data.enabled
    if data.description is not None:
        d.description = data.description
    db.commit()
    clear_cache()
    return _public(d)


@router.delete("/{device_id}")
def delete_device(device_id: int, db: Session = Depends(get_db),
                  _admin: User = Depends(require_admin)):
    d = db.get(Device, device_id)
    if d.source == "zabbix":
        raise HTTPException(
            400, "Устройства из Zabbix удаляются только синхронизацией")
    if not d:
        raise HTTPException(404, "Устройство не найдено")
    db.delete(d)
    db.commit()
    clear_cache()
    return {"ok": True}
