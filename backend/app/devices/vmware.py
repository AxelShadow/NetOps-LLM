"""Read-only адаптер VMware vCenter / standalone ESXi (pyvmomi)."""
import ssl
import datetime as dt
from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim


class VMwareAdapter:
    def __init__(self, host, username, password, port=443):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE   # самоподписанные сертификаты vCenter
        self.si = SmartConnect(host=host, user=username, pwd=password,
                               port=port, sslContext=ctx)
        self.content = self.si.RetrieveContent()

    def _view(self, obj_type):
        return self.content.viewManager.CreateContainerView(
            self.content.rootFolder, [obj_type], True)

    def get_vms(self, host_name: str | None = None) -> list[dict]:
        view = self._view(vim.VirtualMachine)
        out = []
        for vm in view.view:
            host = vm.runtime.host.name if vm.runtime.host else ""
            if not _name_match(host, host_name):
                continue
            qs = vm.summary.quickStats
            out.append({
                "name": vm.name,
                "power": str(vm.runtime.powerState),
                "host": host,
                "guest_ip": getattr(vm.guest, "ipAddress", "") or "",
                "os": getattr(vm.guest, "guestFullName", "") or "",
                "cpu_mhz": getattr(qs, "overallCpuUsage", None),
                "mem_mb": getattr(qs, "guestMemoryUsage", None),
            })
        view.Destroy()
        return out

    def get_hosts(self) -> list[dict]:
        view = self._view(vim.HostSystem)
        out = []
        for h in view.view:
            qs = h.summary.quickStats
            hw = h.summary.hardware
            out.append({
                "name": h.name,
                "state": str(h.runtime.connectionState),
                "cpu_used_mhz": getattr(qs, "overallCpuUsage", None),
                "mem_used_mb": getattr(qs, "overallMemoryUsage", None),
                "mem_total_mb": round(hw.memorySize / 1048576)
                if hw and hw.memorySize else None,
            })
        view.Destroy()
        return out

    def get_snapshots(self) -> list[dict]:
        """ВМ со снапшотами — частая причина проблем с производительностью."""
        view = self._view(vim.VirtualMachine)
        out = []
        for vm in view.view:
            if not vm.snapshot:
                continue
            snaps = _walk(vm.snapshot.rootSnapshotList)
            if snaps:
                out.append({"vm": vm.name, "count": len(
                    snaps), "snapshots": snaps})
        view.Destroy()
        return out

    def get_datastores(self, host_name: str | None = None) -> list[dict]:
        view = self._view(vim.Datastore)
        out = []
        for ds in view.view:
            hosts = [hm.key.name for hm in (ds.host or [])]
            if host_name and not any(_name_match(h, host_name) for h in hosts):
                continue
            cap = ds.summary.capacity
            free = ds.summary.freeSpace
            out.append({
                "name": ds.summary.name,
                "type": ds.summary.type,
                "capacity_gb": round(cap / 1024**3, 1) if cap else None,
                "free_gb": round(free / 1024**3, 1) if free is not None else None,
                "used_gb": round((cap - free) / 1024**3, 1)
                if cap and free is not None else None,
                "hosts": hosts,
            })
        view.Destroy()
        return out

    def get_vm_disks(self, vm_name: str | None = None, host_name: str | None = None) -> list[dict]:
        """Диски ВМ: провизированный VMDK + использование внутри гостя."""
        view = self._view(vim.VirtualMachine)
        out = []
        for vm in view.view:
            if vm_name and vm.name.lower() != vm_name.lower():
                continue
            host = vm.runtime.host.name if vm.runtime.host else ""
            if not _name_match(host, host_name):
                continue
            vmdks = []
            if vm.config:
                for dev in vm.config.hardware.device:
                    if isinstance(dev, vim.vm.device.VirtualDisk):
                        vmdks.append({
                            "label": dev.deviceInfo.label,
                            "provisioned_gb": round(dev.capacityInKB / 1024**2, 1),
                            "file": dev.backing.fileName if dev.backing else "",
                        })
            guest = []
            for gd in (getattr(vm.guest, "disk", None) or []):
                cap, free = gd.capacity or 0, gd.freeSpace or 0
                guest.append({
                    "path": gd.diskPath,
                    "capacity_gb": round(cap / 1024**3, 1),
                    "free_gb": round(free / 1024**3, 1),
                    "used_gb": round((cap - free) / 1024**3, 1),
                })
            out.append({"vm": vm.name, "vmdk": vmdks, "guest_disks": guest})
        view.Destroy()
        return out

    def get_host_networks(self) -> list[dict]:
        """Сетевые интерфейсы ESXi: физические vmnic, VMkernel vmk, vSwitch."""
        view = self._view(vim.HostSystem)
        out = []
        for h in view.view:
            net = h.config.network if h.config else None
            if not net:
                continue
            pnics = []
            for p in (net.pnic or []):
                link = getattr(p, "linkSpeed", None)
                pnics.append({
                    "name": p.device,
                    "mac": p.mac,
                    "speed_mbps": link.speedMb if link else None,
                })
            vnics = []
            for v in (net.vnic or []):
                ip = v.spec.ip if v.spec else None
                vnics.append({
                    "name": v.device,
                    "ip": ip.ipAddress if ip else "",
                    "netmask": ip.subnetMask if ip else "",
                    "portgroup": v.portgroup,
                })
            vswitches = []
            for vs in (net.vswitch or []):
                vswitches.append({
                    "name": vs.name,
                    "pnics": list(vs.pnic or []),
                    "portgroups": list(vs.portgroup or []),
                })
            out.append({"host": h.name, "pnics": pnics,
                        "vmk": vnics, "vswitches": vswitches})
        view.Destroy()
        return out

    def get_vm_networks(self, vm_name: str | None = None, host_name: str | None = None) -> list[dict]:
        """Сетевые интерфейсы ВМ: MAC, сеть, IP (IP — если запущен VMware Tools)."""
        view = self._view(vim.VirtualMachine)
        out = []
        for vm in view.view:
            if vm_name and vm.name.lower() != vm_name.lower():
                continue
            host = vm.runtime.host.name if vm.runtime.host else ""
            if not _name_match(host, host_name):
                continue
            nics = []
            for g in (getattr(vm.guest, "net", None) or []):
                nics.append({
                    "mac": g.macAddress,
                    "network": g.network,
                    "ips": list(g.ipAddress or []),
                    "connected": g.connected,
                })
            if not nics and vm.config:   # Tools не отвечает — берём хотя бы MAC
                for dev in vm.config.hardware.device:
                    if isinstance(dev, vim.vm.device.VirtualEthernetCard):
                        net = getattr(dev.backing, "network", None)
                        nics.append({
                            "mac": dev.macAddress,
                            "network": net.name if net else "",
                            "ips": [],
                            "note": "VMware Tools не отвечает — IP недоступны",
                        })
            out.append({"vm": vm.name, "nics": nics})
        view.Destroy()
        return out

    def get_host_sensors(self, host_name: str | None = None) -> list[dict]:
        """Аппаратные сенсоры ESXi: статусы дисков/контроллеров + только
        проблемные датчики (не green)."""
        view = self._view(vim.HostSystem)
        out = []
        for h in view.view:
            if not _name_match(h.name, host_name):
                continue
            item = {"host": h.name, "storage": [], "problems": []}
            try:
                hs = h.configManager.healthStatusSystem
                if hs and hs.runtime and hs.runtime.hardwareStatusInfo:
                    for st in (hs.runtime.hardwareStatusInfo.storageStatusInfo or []):
                        item["storage"].append({
                            "name": st.name,
                            "status": str(st.status.summary),
                        })
                if hs and hs.runtime and hs.runtime.systemHealthInfo:
                    for s in (hs.runtime.systemHealthInfo.numericSensorInfo or []):
                        if str(s.healthState) == "green":
                            continue
                        item["problems"].append({
                            "name": s.name,
                            "type": s.sensorType,
                            "state": str(s.healthState),
                            "reading": f"{s.currentReading} {s.baseUnits or ''}",
                        })
            except Exception as e:
                item["error"] = f"Нет данных о сенсорах: {e}"
            out.append(item)
        view.Destroy()
        return out

    def get_events(self, hours: int = 24, entity_name: str | None = None,
                   max_count: int = 100) -> list[dict]:
        """Журнал событий за последние hours часов (vCenter отдаёт их по UTC,
        выводим в локальное время сервера)."""
        em = self.content.eventManager
        flt = vim.event.EventFilterSpec()
        flt.time = vim.event.EventFilterSpec.ByTime(
            beginTime=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours))
        flt.maxCount = max_count
        if entity_name:
            entity = self._find_entity(entity_name)
            if entity:
                flt.entity = vim.event.EventFilterSpec.ByEntity(
                    entity=entity,
                    recursion=vim.event.EventFilterSpec.RecursionOption.self)
        events = em.QueryEvents(flt)
        out = []
        for e in reversed(events):            # самые свежие первыми
            ent = getattr(e, "vm", None) or getattr(e, "host", None)
            out.append({
                "time": e.createdTime.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
                "type": type(e).__name__,
                "entity": getattr(ent, "name", "") or "",
                "user": getattr(e, "userName", "") or "",
                "message": (getattr(e, "fullFormattedMessage", "") or "").strip(),
            })
        return out

    def _find_entity(self, name: str):
        """Ищет ВМ или хост по имени для фильтра событий."""
        for obj_type in (vim.VirtualMachine, vim.HostSystem):
            view = self._view(obj_type)
            for obj in view.view:
                if obj.name.lower() == name.strip().lower():
                    view.Destroy()
                    return obj
            view.Destroy()
        return None


def _walk(tree) -> list[dict]:
    out = []
    for node in tree or []:
        out.append({"name": node.name,
                    "created": node.createTime.strftime("%Y-%m-%d %H:%M")})
        out.extend(_walk(node.childSnapshotList))
    return out


def _name_match(actual: str | None, wanted: str | None) -> bool:
    """Сравнение имён с учётом FQDN: vmh05 == vmh05.samges.ru."""
    if not wanted:
        return True
    a = (actual or "").strip().lower()
    w = wanted.strip().lower()
    return a == w or a.split(".")[0] == w.split(".")[0]


# --- кэш сессий (переподключение при таймауте) ---
_adapters: dict[str, "VMwareAdapter"] = {}


def _key(device) -> str:
    return f"{device.host}:{device.port or 443}:{device.username}"


def get_adapter(device) -> VMwareAdapter:
    k = _key(device)
    if k not in _adapters:
        _adapters[k] = VMwareAdapter(device.host, device.username,
                                     device.password, device.port or 443)
    return _adapters[k]


def drop_adapter(device) -> None:
    ad = _adapters.pop(_key(device), None)
    if ad:
        try:
            Disconnect(ad.si)
        except Exception:
            pass


def clear_cache() -> None:
    for ad in list(_adapters.values()):
        try:
            Disconnect(ad.si)
        except Exception:
            pass
    _adapters.clear()
