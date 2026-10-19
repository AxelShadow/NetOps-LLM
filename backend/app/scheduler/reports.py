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
"""
from __future__ import annotations


class ReportError(RuntimeError):
    """Отчёт нельзя разобрать/отрендерить (сообщение без секретов)."""


def parse_health_report(raw: str) -> dict:
    """JSON инструмента -> словарь фактов для шаблона письма."""
    raise ReportError("Разбор отчёта ещё не реализован (Этап 22 в работе)")


def diff_with_previous(keys: list[str],
                       previous_keys: list[str]) -> tuple[list[str], list[str]]:
    """(новые проблемы, устранённые) — сравнение с предыдущим отчётом."""
    raise ReportError("Разбор отчёта ещё не реализован (Этап 22 в работе)")


def render_template(template: str, facts: dict) -> str:
    """Рендер текстового шаблона письма (Jinja2, без HTML-эскейпа)."""
    raise ReportError("Рендер шаблона ещё не реализован (Этап 22 в работе)")
