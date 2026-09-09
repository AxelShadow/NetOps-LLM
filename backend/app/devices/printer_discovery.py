"""SNMP-Discovery принтеров (Этап 19): поиск по подсетям и опрос кандидатов.

Стиль snmp.py: синхронные функции (вызываются из админ-эндпоинта в
to_thread-контексте HTMX), только чтение (GET/WALK v2c). Критерий
«принтер»: непустая серийная таблица Printer-MIB (43.5.1.1.17) — её
реализуют только принтеры; фолбэк — printer-ключевые слова в sysDescr.
"""
import ipaddress
import re

from .snmp import snmp_get, snmp_walk


# Ключевые слова printer-моделей в sysDescr (фолбэк, если серийная
# таблица пуста — редкие вендоры её не отдают).
_PRINTER_KEYWORDS = re.compile(
    r"printer|laserjet|deskjet|officejet|jetdirect|mfp|ecosys|taskalfa|"
    r"bizhub|aficio|imagerunner|workcentre|workcenter|phaser|docuprint|"
    r"magicolor", re.IGNORECASE)

MAX_SUBNETS = 1        # одна подсеть за запуск формы
MIN_PREFIX = 22        # маска до /22 (1024 адреса) включительно
MAX_TOTAL_HOSTS = 1024  # суммарный лимит адресов (/22 = ровно 1024)
MAX_WORKERS = 32       # потоков параллельного опроса

_SYS_OIDS = ["1.3.6.1.2.1.1.1.0", "1.3.6.1.2.1.1.5.0"]  # sysDescr, sysName
_SERIAL_OID = "1.3.6.1.2.1.43.5.1.1.17"                  # prtGeneralSerialNumber


def parse_subnets(text: str) -> list:
    """'10.0.10.0/24' -> [IPv4Network]. ОДНА подсеть с маской до /22.

    ValueError с текстом для flash: пусто/невалидно/IPv6/маска крупнее
    /22/больше одной подсети/суммарно > MAX_TOTAL_HOSTS хостов.
    ip_network(s, strict=False): '10.0.10.5/24' нормализуется в /24.
    """
    if not text or not text.strip():
        raise ValueError("Укажите подсеть, напр. 10.0.10.0/24")
    nets = []
    for part in text.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            net = ipaddress.ip_network(part, strict=False)
        except ValueError:
            raise ValueError(f"Невалидная подсеть {part!r}") from None
        if net.version != 4:
            raise ValueError(f"{part}: только IPv4-подсети поддерживаются")
        if net.prefixlen < MIN_PREFIX:
            raise ValueError(
                f"{part}: маска крупнее /{MIN_PREFIX} не поддерживается "
                f"(слишком много адресов)")
        nets.append(net)
    if not nets:
        raise ValueError("Не найдено ни одной валидной подсети")
    if len(nets) > MAX_SUBNETS:
        raise ValueError(f"Только {MAX_SUBNETS} подсеть за раз "
                         f"(получено {len(nets)})")
    total = sum(net.num_addresses for net in nets)
    if total > MAX_TOTAL_HOSTS:
        raise ValueError(f"Суммарно больше {MAX_TOTAL_HOSTS} адресов "
                         f"(получилось {total})")
    return nets


def probe_ip(ip: str, community: str = "public", timeout: float = 1.0,
             port: int = 161) -> dict | None:
    """Один адрес -> данные принтера | None (не принтер / молчит).

    Критерий: (1) GET sysDescr/sysName — таймаут/ошибка -> None;
    (2) walk серийной таблицы Printer-MIB непустой -> принтер
    (авторитетно: свитчи/серверы её не реализуют); (3) иначе ключевые
    слова printer-моделей в sysDescr -> принтер (serial=None);
    (4) иначе None (SNMP отвечает, но это не принтер).
    """
    try:
        sys_vals = snmp_get(ip, _SYS_OIDS, community=community,
                            timeout=timeout, port=port)
    except Exception:
        return None    # молчит / не SNMP — большинство адресов отсекается тут
    sys_descr = sys_vals.get(_SYS_OIDS[0]) or ""
    sys_name = sys_vals.get(_SYS_OIDS[1]) or ""
    try:
        serial_rows = snmp_walk(ip, _SERIAL_OID, community=community,
                                timeout=timeout, port=port)
    except Exception:
        serial_rows = []
    serial = next((str(v) for _, v in serial_rows if v), None)
    if serial:
        return {"ip": ip, "sys_name": sys_name, "sys_descr": sys_descr,
                "serial": serial}
    if _PRINTER_KEYWORDS.search(sys_descr):
        return {"ip": ip, "sys_name": sys_name, "sys_descr": sys_descr,
                "serial": None}
    return None


def discover_printers(networks: list, community: str = "public",
                       timeout: float = 1.0, port: int = 161,
                       max_workers: int = MAX_WORKERS) -> dict:
    """Опрашивает все адреса сетей параллельно.

    -> {"found": [результаты probe_ip], "probed": N, "responded": M}.
    Каждый поток делает свой asyncio.run + SnmpDispatcher (snmp.py
    потокобезопасен по построению).
    """
    from concurrent.futures import ThreadPoolExecutor

    ips = [str(ip) for net in networks for ip in net.hosts()]
    found, responded = [], 0
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(probe_ip, ip, community, timeout, port)
                   for ip in ips]
        for fut in futures:
            result = fut.result()
            if result is not None:
                found.append(result)
                responded += 1
    return {"found": found, "probed": len(ips), "responded": responded}
