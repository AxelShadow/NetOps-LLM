"""SNMP-адаптер: v2c GET/WALK (pysnmp), только чтение.

Этап SNMP (PLAN.md §9): сетевые устройства и принтеры. Все функции
синхронные (вызываются из инструментов агента), таймаут обязателен —
SNMP-устройство может молчать, нельзя вешать запрос агента.
"""
import asyncio
import re

from pysnmp.hlapi.v1arch.asyncio import bulk_cmd, bulk_walk_cmd, get_cmd
from pysnmp.hlapi.v1arch.asyncio.auth import CommunityData
from pysnmp.hlapi.v1arch.asyncio.dispatch import SnmpDispatcher
from pysnmp.hlapi.v1arch.asyncio.transport import UdpTransportTarget
from pysnmp.proto import rfc1905
from pysnmp.proto.rfc1902 import (
    ObjectIdentifier,
    Null,
    Integer,
    Integer32,
    Unsigned32,
    Counter32,
    Gauge32,
    TimeTicks,
    Counter64,
)
from pysnmp.proto.errind import ErrorIndication


# Числовые OID вида "1.3.6.1.2.1.1.3.0" (имена MIB-объектов не резолвим).
_OID_RE = re.compile(r"^(?:\d+)(?:\.\d+)*$")

# Числовые SNMP-типы (INTEGER/Counter/Gauge/TimeTicks/Unsigned) -> int.
_INT_TYPES = (Integer, Integer32, Unsigned32, Counter32, Gauge32, TimeTicks, Counter64)

# SNMP-исключения (нет объекта/инстанса, конец MIB) -> значение None.
_NULLISH_TYPES = (Null, rfc1905.NoSuchObject, rfc1905.NoSuchInstance, rfc1905.EndOfMibView)


class SnmpError(Exception):
    """Любая ошибка SNMP-запроса (сеть, таймаут, ответ устройства)."""


def _check_oid(oid: str, where: str) -> None:
    """Валидирует числовой OID, иначе SnmpError."""
    if not isinstance(oid, str) or not _OID_RE.match(oid) or not oid:
        raise SnmpError(f"Невалидный OID {oid!r} ({where}): ожидаются числа вида 1.3.6.1.2.1.1.3.0")


def _value(val):
    """SNMP-значение -> python: int для числовых, str для остальных, None для SNMP-исключений."""
    if isinstance(val, _NULLISH_TYPES):
        return None
    if isinstance(val, _INT_TYPES):
        return int(val)
    # OctetString/IpAddress/Opaque: текст -> str, бинарное -> hex (prettyPrint сам выбирает).
    return val.prettyPrint()


def _run(coro):
    """Выполняет корутину pysnmp (asyncio-API) в синхронной функции.

    В backend нет гарантии работающего event-loop в вызывающем потоке,
    поэтому на каждый вызов — свежий loop через asyncio.run (loop не
    переиспользуется, гонок с FastAPI-петлёй нет).
    """
    try:
        return asyncio.run(coro)
    except SnmpError:
        raise
    except (OSError, asyncio.TimeoutError) as e:
        raise SnmpError(f"Ошибка сети при SNMP-запросе: {e}") from e
    except ErrorIndication as e:
        raise SnmpError(f"Ошибка SNMP-движка: {e}") from e
    except Exception as e:  # сетевые ошибки pysnmp приходят разными типами
        raise SnmpError(f"Ошибка SNMP-запроса: {e}") from e


async def _target(host: str, port: int, timeout: float):
    """Строит UDP-транспорт с таймаутом (retries=1: один повтор при потере)."""
    try:
        return await UdpTransportTarget.create((host, port), timeout=timeout, retries=1)
    except OSError as e:
        raise SnmpError(f"Невалидный адрес {host}: {e}") from e


async def _get(host: str, oids: list[str], community: str, timeout: float, port: int) -> dict:
    for oid in oids:
        _check_oid(oid, "GET")
    # На каждый вызов свой SnmpDispatcher: потокобезопасно (pysnmp требует
    # отдельный dispatcher на поток), создание дёшево (~0.02 мс).
    with SnmpDispatcher() as dispatcher:
        target = await _target(host, port, timeout)
        auth = CommunityData(community, mpModel=1)  # mpModel=1 -> SNMPv2c
        var_binds = [(ObjectIdentifier(oid), Null("")) for oid in oids]
        error_indication, error_status, error_index, rsp = await get_cmd(
            dispatcher, auth, target, *var_binds, lookupMib=False
        )
        if error_indication:
            raise SnmpError(f"GET {host}: {error_indication}")
        if error_status:
            idx = int(error_index) - 1
            bad = oids[idx] if 0 <= idx < len(oids) else "?"
            raise SnmpError(f"GET {host}: ошибка PDU {error_status.prettyPrint()} на {bad}")
        result = {}
        for oid, rsp_vb in zip(oids, rsp):
            rsp_oid = str(rsp_vb[0])
            result[oid] = _value(rsp_vb[1]) if rsp_oid == oid else None
        return result


async def _walk(host: str, oid: str, community: str, timeout: float, max_repetitions: int, port: int) -> list[tuple]:
    _check_oid(oid, "WALK")
    with SnmpDispatcher() as dispatcher:
        target = await _target(host, port, timeout)
        auth = CommunityData(community, mpModel=1)
        rows = []
        # lexicographicMode=False: останавливаемся на выходе из поддерева OID.
        agen = bulk_walk_cmd(
            dispatcher, auth, target, 0, max_repetitions,
            (ObjectIdentifier(oid), Null("")),
            lookupMib=False, lexicographicMode=False,
        )
        async for error_indication, error_status, error_index, var_binds in agen:
            if error_indication:
                raise SnmpError(f"WALK {host}: {error_indication}")
            if error_status:
                raise SnmpError(f"WALK {host}: ошибка PDU {error_status.prettyPrint()}")
            for vb in var_binds:
                rows.append((str(vb[0]), _value(vb[1])))
        return rows


def snmp_get(host: str, oids: list[str], community: str = "public", timeout: float = 2.0, *, port: int = 161) -> dict:
    """SNMPv2c GET нескольких OID одним запросом.

    Возвращает {oid: int|str|None}: числовые SNMP-типы -> int, OctetString
    -> str, отсутствующий на устройстве OID (noSuchInstance/noSuchObject) ->
    None (не ошибка). port — нестандартный UDP-порт агента (по умолчанию 161).
    """
    if not oids:
        return {}
    return _run(_get(host, list(oids), community, timeout, port))


def snmp_walk(host: str, oid: str, community: str = "public", timeout: float = 2.0, max_repetitions: int = 64, *, port: int = 161) -> list[tuple]:
    """SNMPv2c WALK (GETBULK-обход) поддерева OID.

    Возвращает [(full_oid, int|str|None), ...] в порядке обхода; пустое
    поддерево -> []. Обход останавливается на выходе за пределы поддерева
    (lexicographicMode=False), табличные значения идут с индексами строк.
    port — нестандартный UDP-порт агента (по умолчанию 161).
    """
    return _run(_walk(host, oid, community, timeout, max_repetitions, port))
