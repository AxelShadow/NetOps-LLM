"""Тесты разбора отчёта `get_infrastructure_health` и рендера письма (Этап 22).

Проверяет `app/scheduler/reports.py`:
  * `parse_health_report` — счётчики summary, классификация severity
    (High/Disaster -> critical, Average/Warning -> warning), группировка
    `problems_text`, ошибка Zabbix отдельным элементом {"error": ...},
    устойчивый ключ проблемы `name @ host`, дата и summary_text;
  * ошибки разбора (не JSON, пусто, не объект, не строка) -> ReportError;
  * `diff_with_previous` — новые/устранённые, пустой предыдущий снимок,
    дедупликация, порядок;
  * `render_template` — Jinja2 без HTML-эскейпа, неизвестные переменные не
    роняют рендер, синтаксическая ошибка -> ReportError.

Без сети и БД: модуль `reports` чистый (JSON + Jinja2).
Запуск: .venv/Scripts/python.exe -m pytest tests/test_report_parsing.py -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

import pytest  # noqa: E402

from app.scheduler import reports  # noqa: E402


def _health_report(**overrides) -> dict:
    """Пример отчёта инструмента (структура — как в agent/tools.py)."""
    report = {
        "timestamp": "2026-10-19 08:05:11",
        "summary": {
            "total_active_devices": 42,
            "zabbix_alerts": 5,
            "datastore_low_space": 1,
            "host_high_load": 2,
            "vms_with_snapshots": 3,
        },
        "zabbix_problems": [
            {"time": "2026-10-19 07:10", "host": "srv-01", "problem":
             "CPU load high", "severity": "High", "ack": False},
            {"time": "2026-10-19 07:20", "host": "srv-02", "problem":
             "Disk almost full", "severity": "Disaster", "ack": True},
            {"time": "2026-10-19 07:30", "host": "srv-03", "problem":
             "Memory usage", "severity": "Average", "ack": False},
            {"time": "2026-10-19 07:40", "host": "srv-04", "problem":
             "Interface errors", "severity": "Warning", "ack": False},
            {"time": "2026-10-19 07:50", "host": "srv-05", "problem":
             "Agent ping", "severity": "Information", "ack": False},
        ],
        "vmware_issues": {
            "datastores_low_space": [
                {"source": "vc-1", "datastore": "ds-01",
                 "free_percent": 7.5},
            ],
            "hosts_high_load": [
                {"source": "vc-1", "host": "esx-1", "cpu_percent": 91.0,
                 "mem_percent": 55.0},
                {"source": "vc-1", "host": "esx-2", "cpu_percent": None,
                 "mem_percent": 88.0},
            ],
            "vms_with_snapshots": [
                {"source": "vc-1", "vm": "vm-1", "snapshot_count": 2,
                 "oldest": "2026-09-01"},
            ],
        },
        "inventory_note": "Обнаружено проблемных областей: 11.",
    }
    report.update(overrides)
    return report


def _raw(report: dict) -> str:
    return json.dumps(report, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# parse_health_report
# --------------------------------------------------------------------------- #

def test_parse_counts_and_severity_groups():
    facts = reports.parse_health_report(_raw(_health_report()))

    assert facts["critical"] == 2          # High + Disaster
    assert facts["warning"] == 2           # Average + Warning
    assert facts["problems_total"] == 5
    assert facts["other_problems"][0]["problem"] == "Agent ping"
    assert facts["zabbix_alerts"] == 5
    assert facts["total_active_devices"] == 42
    assert facts["datastore_low_space"] == 1
    assert facts["host_high_load"] == 2
    assert facts["vms_with_snapshots"] == 3
    assert facts["date"] == "19.10.2026"
    assert facts["has_problems"] is True


def test_parse_problems_text_grouped():
    facts = reports.parse_health_report(_raw(_health_report()))
    text = facts["problems_text"]

    assert "Критичные (2):" in text
    assert "Предупреждения (2):" in text
    assert "Прочие (1):" in text
    assert "srv-01: CPU load high" in text
    assert "srv-04: Interface errors" in text
    # critical-группа идёт раньше warning-группы
    assert text.index("Критичные") < text.index("Предупреждения")
    # строка проблемы содержит время
    assert "[2026-10-19 07:10]" in text


def test_parse_problem_keys_stable_name_and_host():
    facts = reports.parse_health_report(_raw(_health_report()))

    assert "CPU load high @ srv-01" in facts["problem_keys"]
    assert "Disk almost full @ srv-02" in facts["problem_keys"]
    assert len(facts["problem_keys"]) == 5
    # ключ не зависит от problemid (его в отчёте и нет)
    assert all("@ " in key for key in facts["problem_keys"])


def test_parse_vmware_issue_lists():
    facts = reports.parse_health_report(_raw(_health_report()))

    assert facts["datastore_issues"][0]["datastore"] == "ds-01"
    assert facts["host_issues"][0]["host"] == "esx-1"
    assert facts["snapshot_issues"][0]["vm"] == "vm-1"
    assert facts["inventory_note"] == "Обнаружено проблемных областей: 11."
    assert facts["summary_text"] == "Обнаружено проблемных областей: 11."


def test_parse_zabbix_error_item():
    report = _health_report(
        zabbix_problems=[{"error": "Zabbix недоступен: timeout"}],
        summary={"total_active_devices": 5, "zabbix_alerts": 0,
                 "datastore_low_space": 0, "host_high_load": 0,
                 "vms_with_snapshots": 0})
    facts = reports.parse_health_report(_raw(report))

    assert facts["critical"] == 0 and facts["warning"] == 0
    assert facts["zabbix_errors"] == ["Zabbix недоступен: timeout"]
    assert "Ошибки получения данных Zabbix" in facts["problems_text"]
    assert facts["has_problems"] is True
    # ошибка не превращается в проблему с пустым ключом
    assert facts["problem_keys"] == []


def test_parse_no_problems_text():
    report = {
        "timestamp": "2026-10-19 08:00:00",
        "summary": {"total_active_devices": 3, "zabbix_alerts": 0,
                    "datastore_low_space": 0, "host_high_load": 0,
                    "vms_with_snapshots": 0},
        "zabbix_problems": [],
        "vmware_issues": {"datastores_low_space": [], "hosts_high_load": [],
                          "vms_with_snapshots": []},
        "inventory_note": "Проблем не обнаружено. Инфраструктура в норме.",
    }
    facts = reports.parse_health_report(_raw(report))

    assert facts["critical"] == 0
    assert facts["problems_text"] == "Активных проблем Zabbix нет."
    assert facts["has_problems"] is False
    assert facts["new_problems_count"] == 0
    assert facts["new_problems"] == [] and facts["resolved_problems"] == []


def test_parse_minimal_report_uses_fallbacks():
    facts = reports.parse_health_report("{}")

    assert facts["total_active_devices"] == 0
    assert facts["zabbix_alerts"] == 0
    assert facts["problems_text"] == "Активных проблем Zabbix нет."
    # summary_text собирается из счётчиков, если inventory_note пуст
    assert "Устройств активно: 0" in facts["summary_text"]
    assert facts["date"]        # сегодняшняя дата в формате dd.mm.yyyy


def test_parse_zabbix_alerts_falls_back_to_problem_count():
    report = _health_report(summary={"total_active_devices": 1})
    facts = reports.parse_health_report(_raw(report))
    assert facts["zabbix_alerts"] == 5


def test_parse_severity_case_insensitive():
    report = _health_report(
        zabbix_problems=[
            {"host": "h1", "problem": "p1", "severity": "high"},
            {"host": "h2", "problem": "p2", "severity": "DISASTER"},
            {"host": "h3", "problem": "p3", "severity": "warning"},
        ],
        summary={"zabbix_alerts": 3})
    facts = reports.parse_health_report(_raw(report))
    assert facts["critical"] == 2
    assert facts["warning"] == 1


@pytest.mark.parametrize("bad", ["", "   ", "not json {", "[]", "null",
                                 '"строка"', "42"])
def test_parse_invalid_input_raises_report_error(bad: str):
    with pytest.raises(reports.ReportError):
        reports.parse_health_report(bad)


def test_parse_non_string_input_raises_report_error():
    with pytest.raises(reports.ReportError):
        reports.parse_health_report({"summary": {}})
    with pytest.raises(reports.ReportError):
        reports.parse_health_report(None)


def test_parse_error_message_does_not_echo_raw_text():
    marker = "очень-секретный-текст-отчёта"
    with pytest.raises(reports.ReportError) as exc_info:
        reports.parse_health_report("{" + marker)
    assert marker not in str(exc_info.value)


# --------------------------------------------------------------------------- #
# diff_with_previous
# --------------------------------------------------------------------------- #

def test_diff_new_and_resolved():
    new, resolved = reports.diff_with_previous(
        ["p1 @ h1", "p2 @ h2", "p3 @ h3"],
        ["p2 @ h2", "p4 @ h4"])
    assert new == ["p1 @ h1", "p3 @ h3"]
    assert resolved == ["p4 @ h4"]


def test_diff_first_run_all_new():
    new, resolved = reports.diff_with_previous(["a", "b"], [])
    assert new == ["a", "b"] and resolved == []


def test_diff_all_resolved():
    new, resolved = reports.diff_with_previous([], ["a", "b"])
    assert new == [] and resolved == ["a", "b"]


def test_diff_dedup_and_order():
    new, resolved = reports.diff_with_previous(
        ["b", "a", "b", "a"], ["a", "a"])
    assert new == ["b"]
    # порядок предыдущего снимка сохраняется
    new, resolved = reports.diff_with_previous([], ["z", "y", "z"])
    assert resolved == ["z", "y"]


def test_diff_none_treated_as_empty():
    new, resolved = reports.diff_with_previous(None, None)
    assert new == [] and resolved == []


# --------------------------------------------------------------------------- #
# render_template
# --------------------------------------------------------------------------- #

def test_render_template_unknown_variables_are_empty():
    out = reports.render_template(
        "A={{ unknown }}; B={{ missing.field }}; C={{ critical }}",
        {"critical": 3})
    assert out == "A=; B=; C=3"


def test_render_template_no_html_escape():
    out = reports.render_template("{{ payload }}",
                                  {"payload": "<b>&\"'</b>"})
    assert out == "<b>&\"'</b>"


def test_render_template_default_email_template_renders():
    facts = reports.parse_health_report(_raw(_health_report()))
    subject = reports.render_template(
        "NetOps: отчёт {{ date }} — {{ critical }} критичных, "
        "{{ warning }} предупреждений", facts)
    body = reports.render_template(
        "Дата: {{ date }}\nПроблемы:\n{{ problems_text }}\n"
        "Сводка: {{ summary_text }}", facts)
    assert "19.10.2026" in subject and "2 критичных" in subject
    assert "srv-01: CPU load high" in body
    assert "Сводка: Обнаружено проблемных областей: 11." in body


def test_render_template_newlines_preserved():
    out = reports.render_template("line1\nline2\n", {})
    assert out == "line1\nline2\n"


def test_render_template_syntax_error_raises_report_error():
    with pytest.raises(reports.ReportError):
        reports.render_template("{{ broken ", {})
    with pytest.raises(reports.ReportError):
        reports.render_template("{% if %}", {})


def test_render_template_non_string_raises():
    with pytest.raises(reports.ReportError):
        reports.render_template(None, {})
    with pytest.raises(reports.ReportError):
        reports.render_template(b"bytes", {})


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
