"""Разбор отчёта `get_infrastructure_health` и рендер письма.

Вход: JSON-строка инструмента `get_infrastructure_health` (см.
`agent/tools.py`): `summary`, `zabbix_problems`, `vmware_issues`,
`inventory_note`, `timestamp`.

Выход для шаблона: плоский словарь фактов (`critical`, `warning`,
`problems_text`, `new_problems_count`, `resolved_count`, `summary_text`, ...)
— шаблон письма из админки не должен сам разбирать JSON.

Контракт `parse_health_report(raw)`:
* принимает JSON-строку; ошибка разбора -> `ReportError` с текстом;
* проблемы Zabbix группируются по severity; ключ проблемы для сравнения
  прогонов — `name + host` (устойчиво к пересозданию problemid);
* `diff_with_previous(keys, previous_keys)` -> (new, resolved).

Секретов здесь нет и быть не должно: вход — уже готовый отчёт инструмента,
сообщения `ReportError` не содержат содержимого отчёта (только тип ошибки),
чтобы случайный текст не утёк в письмо/лог.
"""
from __future__ import annotations

import datetime as dt
import json
import logging

from jinja2.sandbox import SandboxedEnvironment
from jinja2 import ChainableUndefined, TemplateError

log = logging.getLogger(__name__)


class ReportError(RuntimeError):
    """Отчёт нельзя разобрать/отрендерить.

    Атрибут `public` — короткий безопасный текст для пользовательских
    сообщений (админка, `scheduler.last_run_message`, `audit_log.result`):
    в отличие от текста исключения, он не содержит деталей инструмента и
    данных. Полный текст исключения пишется только в серверный лог
    (design §5/§6.7).
    """

    public: str = "ошибка обработки отчёта"

    def __init__(self, message: str = "", *, public: str | None = None):
        super().__init__(message)
        if public is not None:
            self.public = public


# Классификация severity Zabbix. Строки ровно такие, как их отдаёт
# agent/tools.py (_SEVERITY): 0..5 -> Not classified/Information/Warning/
# Average/High/Disaster. Сравнение регистронезависимое.
_CRITICAL_SEVERITIES = frozenset({"high", "disaster"})
_WARNING_SEVERITIES = frozenset({"average", "warning"})
_INFO_SEVERITIES = frozenset({"information", "info", "not classified"})

_RANK_CRITICAL = 0
_RANK_WARNING = 1
_RANK_OTHER = 2

# Jinja2 для текстового письма: без HTML-эскейпа (письмо text/plain), но и без
# `StrictUndefined`: неизвестная переменная в шаблоне из админки должна дать
# пустую строку, а не 500. ChainableUndefined терпит и `{{ missing.field }}`.
# SandboxedEnvironment: шаблон редактируется в админке, поэтому доступ к
# опасным атрибутам/вызовам (SSTI вида `{{ ''.__class__.__mro__ }}`) запрещён
# на уровне Jinja2; данные из Zabbix остаются простыми строками.
_env = SandboxedEnvironment(undefined=ChainableUndefined, autoescape=False,
                            keep_trailing_newline=True)


def _as_int(value) -> int:
    """Безопасно приводит счётчик к int (None/мусор -> 0)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_list(value) -> list:
    return value if isinstance(value, list) else []


def _as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _severity_rank(severity: str) -> int:
    normalized = (severity or "").strip().lower()
    if normalized in _CRITICAL_SEVERITIES:
        return _RANK_CRITICAL
    if normalized in _WARNING_SEVERITIES:
        return _RANK_WARNING
    return _RANK_OTHER


def problem_key(problem: dict) -> str:
    """Устойчивый ключ проблемы для сравнения прогонов: `name + host`.

    Опирается на имя триггера (`problem`/`name`) и хост, а не на problemid:
    Zabbix пересоздаёт id при переоткрытии проблемы, а имя+хост стабильны.
    """
    problem = _as_dict(problem)
    name = str(problem.get("problem") or problem.get("name")
               or problem.get("description") or "").strip()
    host = str(problem.get("host") or problem.get("hosts") or "").strip()
    if host:
        return f"{name} @ {host}"
    return name


def _format_problem_line(problem: dict) -> str:
    when = str(problem.get("time") or "").strip()
    host = str(problem.get("host") or "").strip()
    name = str(problem.get("problem") or problem.get("name") or "").strip()
    prefix = f"- [{when}] " if when else "- "
    if host:
        return f"{prefix}{host}: {name}"
    return f"{prefix}{name}"


def _problems_text(critical: list[dict], warning: list[dict],
                   other: list[dict], errors: list[str],
                   total: int) -> str:
    """Текст «Проблемы:» для шаблона — сгруппировано по severity."""
    if not total and not errors:
        return "Активных проблем Zabbix нет."
    blocks: list[str] = []
    if critical:
        blocks.append(f"Критичные ({len(critical)}):")
        blocks.extend(_format_problem_line(p) for p in critical)
    if warning:
        blocks.append(f"Предупреждения ({len(warning)}):")
        blocks.extend(_format_problem_line(p) for p in warning)
    if other:
        blocks.append(f"Прочие ({len(other)}):")
        blocks.extend(_format_problem_line(p) for p in other)
    if errors:
        blocks.append("Ошибки получения данных Zabbix:")
        blocks.extend(f"- {err}" for err in errors)
    return "\n".join(blocks)


def _summary_text(summary: dict, inventory_note: str) -> str:
    """Сводка: заметка инструмента, иначе собранная из счётчиков фраза."""
    note = (inventory_note or "").strip()
    if note:
        return note
    return (
        f"Устройств активно: {_as_int(summary.get('total_active_devices'))}; "
        f"алертов Zabbix: {_as_int(summary.get('zabbix_alerts'))}; "
        f"датасторов с малым запасом: "
        f"{_as_int(summary.get('datastore_low_space'))}; "
        f"хостов с высокой загрузкой: "
        f"{_as_int(summary.get('host_high_load'))}; "
        f"ВМ со снапшотами: {_as_int(summary.get('vms_with_snapshots'))}."
    )


def _report_date(timestamp: str) -> str:
    """Дата отчёта для шаблона: из timestamp инструмента, иначе сегодня."""
    raw = (timestamp or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(raw, fmt).strftime("%d.%m.%Y")
        except ValueError:
            continue
    return dt.date.today().strftime("%d.%m.%Y")


def _normalize_problem(problem: dict) -> dict:
    severity = str(problem.get("severity") or "").strip()
    return {
        "time": str(problem.get("time") or ""),
        "host": str(problem.get("host") or ""),
        "problem": str(problem.get("problem") or problem.get("name") or ""),
        "severity": severity,
        "ack": bool(problem.get("ack")),
        "key": problem_key(problem),
        "_rank": _severity_rank(severity),
    }


def _list_issues(items: list, keys: tuple[str, ...]) -> list[dict]:
    """Нормализует элементы vmware_issues в плоские dict для шаблона."""
    out: list[dict] = []
    for item in items:
        item = _as_dict(item)
        if not item:
            continue
        out.append({key: item.get(key) for key in keys if key in item})
    return out


def parse_health_report(raw: str) -> dict:
    """JSON инструмента -> словарь фактов для шаблона письма.

    Raises:
        ReportError: вход не JSON-объект или отчёт имеет неверный тип.
            Текст ошибки не содержит содержимого отчёта.
    """
    if not isinstance(raw, str):
        raise ReportError("Отчёт инструмента должен быть JSON-строкой",
                          public="отчёт инструмента: неверный формат")
    text = raw.strip()
    if not text:
        raise ReportError("Отчёт инструмента пуст",
                          public="отчёт инструмента пуст")
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ReportError(
            f"Отчёт инструмента: некорректный JSON ({type(exc).__name__})",
            public="отчёт инструмента: некорректный JSON",
        ) from exc
    if not isinstance(data, dict):
        raise ReportError("Отчёт инструмента: ожидался JSON-объект",
                          public="отчёт инструмента: неверный формат")

    summary = _as_dict(data.get("summary"))
    vmware = _as_dict(data.get("vmware_issues"))
    inventory_note = str(data.get("inventory_note") or "")
    timestamp = str(data.get("timestamp") or "")

    # Проблемы Zabbix: обычные записи + отдельный элемент {"error": ...},
    # который инструмент кладёт при сбое Zabbix (см. tools.py).
    problems: list[dict] = []
    errors: list[str] = []
    for item in _as_list(data.get("zabbix_problems")):
        if not isinstance(item, dict):
            continue
        if "error" in item and not item.get("problem") and not item.get("name"):
            errors.append(str(item.get("error") or ""))
            continue
        problems.append(_normalize_problem(item))

    critical = [p for p in problems if p["_rank"] == _RANK_CRITICAL]
    warning = [p for p in problems if p["_rank"] == _RANK_WARNING]
    other = [p for p in problems if p["_rank"] == _RANK_OTHER]
    ordered = sorted(problems, key=lambda p: p["_rank"])

    datastore_issues = _list_issues(
        _as_list(vmware.get("datastores_low_space")),
        ("datastore", "free_percent", "source"))
    host_issues = _list_issues(
        _as_list(vmware.get("hosts_high_load")),
        ("host", "cpu_percent", "mem_percent", "source"))
    snapshot_issues = _list_issues(
        _as_list(vmware.get("vms_with_snapshots")),
        ("vm", "snapshot_count", "oldest", "source"))

    # Счётчики: доверяем summary, но не даём ему разойтись с реальным списком
    # проблем (Zabbix мог вернуть ошибку -> summary.zabbix_alerts = 0).
    zabbix_alerts = _as_int(summary.get("zabbix_alerts")) or len(problems)
    datastore_low_space = _as_int(summary.get("datastore_low_space"))
    host_high_load = _as_int(summary.get("host_high_load"))
    vms_with_snapshots = _as_int(summary.get("vms_with_snapshots"))
    total_active_devices = _as_int(summary.get("total_active_devices"))

    facts = {
        # --- имена, на которые опирается шаблон из админки (контракт §5.2) ---
        "date": _report_date(timestamp),
        "total_active_devices": total_active_devices,
        "zabbix_alerts": zabbix_alerts,
        "critical": len(critical),
        "warning": len(warning),
        "new_problems_count": 0,
        "resolved_count": 0,
        "datastore_low_space": datastore_low_space,
        "host_high_load": host_high_load,
        "vms_with_snapshots": vms_with_snapshots,
        "problems_text": _problems_text(
            critical, warning, other, errors, len(problems)),
        "summary_text": _summary_text(summary, inventory_note),
        # --- дополнительный контекст (доступен продвинутым шаблонам) ---
        "problems_total": len(problems),
        "has_problems": bool(problems) or bool(errors),
        "problems": ordered,
        "critical_problems": critical,
        "warning_problems": warning,
        "other_problems": other,
        "problem_keys": [p["key"] for p in ordered if p["key"]],
        "zabbix_errors": errors,
        "errors_text": "\n".join(f"- {err}" for err in errors),
        "datastore_issues": datastore_issues,
        "host_issues": host_issues,
        "snapshot_issues": snapshot_issues,
        "inventory_note": inventory_note.strip(),
        "timestamp": timestamp,
        "new_problems": [],
        "resolved_problems": [],
        "new_problems_text": "",
        "resolved_text": "",
    }
    return facts


def _ordered_unique(values) -> list[str]:
    """Строки без пустых и дублей с сохранением порядка."""
    out: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def diff_with_previous(keys: list[str],
                       previous_keys: list[str]) -> tuple[list[str], list[str]]:
    """(новые проблемы, устранённые) — сравнение с предыдущим отчётом.

    `resolved` — то, что было в прошлом снимке и исчезло сейчас. Порядок
    элементов — как во входных списках (порядок стабилен для писем/тестов).
    None трактуется как пустой список, поэтому первый прогон не падает.
    """
    current = _ordered_unique(keys)
    previous = _ordered_unique(previous_keys)
    current_set = set(current)
    previous_set = set(previous)
    new = [key for key in current if key not in previous_set]
    resolved = [key for key in previous if key not in current_set]
    return new, resolved


def render_template(template: str, facts: dict) -> str:
    """Рендер текстового шаблона письма (Jinja2, без HTML-эскейпа).

    Неизвестные переменные дают пустую строку (ChainableUndefined), а не
    исключение — шаблон редактируется в админке и не должен ронять запуск.

    Raises:
        ReportError: шаблон не строка или содержит синтаксическую/рендер-ошибку.
    """
    if not isinstance(template, str):
        raise ReportError("Шаблон письма должен быть строкой",
                          public="некорректный шаблон письма")
    data = facts if isinstance(facts, dict) else {}
    try:
        return _env.from_string(template).render(**data)
    except TemplateError as exc:
        raise ReportError(
            f"Ошибка шаблона письма: {type(exc).__name__}: {exc}",
            public="ошибка шаблона письма",
        ) from exc
    except Exception as exc:   # рендер-ошибка из данных (не TemplateError)
        raise ReportError(
            f"Ошибка рендера письма: {type(exc).__name__}",
            public="ошибка рендера письма",
        ) from exc
