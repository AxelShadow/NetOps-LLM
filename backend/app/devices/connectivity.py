"""Проверка настроек подключения при добавлении устройства (Этап 20).

Чистая синхронная функция: вызывается из async-эндпоинта через
asyncio.to_thread (snmp_get внутри делает asyncio.run — из async-
контекста напрямую нельзя). Классифицированные тексты ошибок для
модалки «Новое устройство».

Логика по протоколу (protocol: "ssh,snmp" / "ssh" / "snmp" / ""):
- "snmp" -> реальный GET sysDescr (короткий таймаут); семантика порта
  как в _snmp_call: у printer порт формы = SNMP-порт, у сетевых
  SNMP всегда 161;
- "ssh" -> TCP-connect host:port (порт формы, дефолт 22);
- оба -> обе проверки, первая ошибка собирается в общий блок;
- protocol пуст: vmware/esxi -> TCP host:(port or 443); прочие типы
  -> ICMP ping (эхо-доступность).
"""
import re
import socket

from .snmp import snmp_get

_SYSDESCR_OID = "1.3.6.1.2.1.1.1.0"

_SNMP_TIMEOUT = 2.0    # сек: не тянем модалку дольше необходимого
_TCP_TIMEOUT = 5.0


def _classify_tcp_error(e: Exception, host: str, port: int) -> str:
    """Сокетная ошибка -> внятный текст для пользователя."""
    text = str(e)
    if isinstance(e, socket.timeout) or "timed out" in text:
        return (f"{host}:{port} не отвечает (таймаут "
                f"{_TCP_TIMEOUT:.0f} с) — устройство выключено или "
                f"порт {port} закрыт")
    if isinstance(e, ConnectionRefusedError) or "refused" in text.lower():
        return (f"{host}:{port} соединение отклонено — сервис на порту "
                f"{port} не запущен или закрыт файрволом")
    if "unreachable" in text.lower() or "no route" in text.lower():
        return f"{host} недоступен (нет маршрута до сети устройства)"
    if isinstance(e, socket.gaierror) or "name" in text.lower():
        return f"Не удалось разрешить адрес {host} (DNS/неверное имя)"
    return f"{host}:{port}: {type(e).__name__}: {text[:150]}"


def _tcp_check(host: str, port: int) -> str | None:
    """TCP-connect -> None (ок) | текст ошибки."""
    try:
        with socket.create_connection((host, port), timeout=_TCP_TIMEOUT):
            return None
    except Exception as e:
        return _classify_tcp_error(e, host, port)


def _ping_check(host: str) -> str | None:
    """ICMP-доступность через системный ping (паттерн _ping_cmd,
    agent/tools.py:206): считаем строку статистики ответов.

    Windows: «Получено = N» (локализовано), Unix: «N received»,
    «N packets received». N > 0 -> доступен.
    """
    import subprocess
    import platform
    from ..agent.tools import _HOST_RE
    if not _HOST_RE.match(host or ""):
        return f"Некорректный адрес {host!r}"
    flag = "-n" if platform.system() == "Windows" else "-c"
    try:
        r = subprocess.run(["ping", flag, "2", host],
                           capture_output=True, text=True, timeout=20)
    except Exception as e:
        return f"Ping {host}: {type(e).__name__}: {str(e)[:150]}"
    out = (r.stdout + r.stderr)
    m = re.search(r"(?:Получено|Received|received)[^0-9]*(\d+)", out)
    if m and int(m.group(1)) > 0:
        return None
    m2 = re.search(r"(\d+)\s+received", out)
    if m2 and int(m2.group(1)) > 0:
        return None
    return f"{host} не отвечает на ping (ICMP): {out.strip()[:120]}"


def _snmp_check(host: str, community: str, port: int) -> str | None:
    """SNMP GET sysDescr -> None (ок) | текст ошибки."""
    try:
        vals = snmp_get(host, [_SYSDESCR_OID], community=community,
                        timeout=_SNMP_TIMEOUT, port=port)
    except Exception as e:
        return (f"SNMP {host}:{port} не отвечает (community "
                f"«{community}»?): {str(e)[:150]}")
    descr = vals.get(_SYSDESCR_OID)
    if not descr:
        return (f"SNMP {host}:{port} отвечает, но sysDescr пуст — "
                f"проверьте community «{community}»")
    return None


def check_device_connectivity(*, host: str, protocol: str = "",
                              device_type: str = "", port: int = 0,
                              snmp_community: str = "public") -> tuple[bool, str]:
    """Настройки подключения нового устройства -> (ok, сообщение).

    ok=False: сообщение — развёрнутая причина (для красного блока
    модалки); ok=True: сообщение — короткое подтверждение (для лога).
    """
    protos = set(filter(None, protocol.split(",")))
    errors, oks = [], []

    if "snmp" in protos:
        # Семантика порта как в _snmp_call (agent/tools.py): у принтеров
        # порт формы = SNMP-порт, у сетевых SNMP живёт на 161 всегда
        snmp_port = port if device_type == "printer" else 161
        err = _snmp_check(host, snmp_community, snmp_port)
        if err:
            errors.append(err)
        else:
            oks.append("SNMP отвечает")

    if "ssh" in protos:
        err = _tcp_check(host, port or 22)
        if err:
            errors.append(err)
        else:
            oks.append(f"SSH-порт {port or 22} открыт")

    if not protos:
        # Протокол не задан: типовые проверки по типу устройства
        if device_type in ("vcenter", "esxi"):
            err = _tcp_check(host, port or 443)
            if err:
                errors.append(err)
            else:
                oks.append(f"Порт {port or 443} доступен")
        else:
            err = _ping_check(host)
            if err:
                errors.append(err)
            else:
                oks.append("Устройство отвечает на ping")

    if errors:
        return False, "; ".join(errors)
    return True, ", ".join(oks) or "Проверка не требуется"
