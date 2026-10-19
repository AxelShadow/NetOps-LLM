"""Тесты задания «утренний отчёт» (Этап 22, без сети и без реального SMTP).

Проверяет `app/scheduler/jobs.py`:
  * `run_morning_report` вызывает `execute_tool("get_infrastructure_health",
    {}, user_role="admin")` (подмена), разбирает отчёт, сравнивает проблемы
    со снимком `scheduler.last_problem_keys` (новые/устранённые, лимит 500),
    рендерит шаблоны из app_settings и «отправляет» письмо (подмена smtplib);
  * запись результата: `scheduler.last_run_at/status/message` и
    `AuditLog(tool="morning_report")`;
  * ошибки (статус инструмента, битый JSON, отказал SMTP, нет получателей,
    исключение) возвращаются в результате и аудите, не бросают;
  * тема письма санитизируется от переводов строк, секрет SMTP не попадает
    в логи.

БД: временная sqlite (env ДО импорта app), таблицы через create_all.
Запуск: .venv/Scripts/python.exe -m pytest tests/test_scheduler_jobs.py -v
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

_TMP = tempfile.mkdtemp(prefix="netops_jobs_")
os.environ.setdefault("NETOPS_DATABASE_URL",
                      f"sqlite:///{_TMP}/jobs_test.db".replace("\\", "/"))
os.environ.setdefault("NETOPS_DEV_MODE", "true")
os.environ.setdefault("NETOPS_MOCK_MODE", "true")
os.environ.setdefault("NETOPS_AD_DOMAIN", "mock.local")
os.environ.setdefault("NETOPS_BOOTSTRAP_ADMIN", "admin@mock.local")
os.environ.setdefault("NETOPS_JWT_SECRET", "jobs" + "-test")
os.environ.setdefault("NETOPS_LLM_BASE_URL", "http://localhost:9/v1")
os.environ.setdefault("NETOPS_SCHEDULER_ENABLED", "false")

from cryptography.fernet import Fernet  # noqa: E402

if not os.environ.get("NETOPS_ENCRYPTION_KEY"):
    os.environ["NETOPS_ENCRYPTION_KEY"] = Fernet.generate_key().decode("ascii")

import pytest  # noqa: E402

import app.agent.tools as tools_module  # noqa: E402
from app import settings_store  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import AppSetting, AuditLog  # noqa: E402
from app.scheduler import delivery, jobs  # noqa: E402
from app.scheduler import reports as scheduler_reports  # noqa: E402
from app.scheduler.settings import SECRET_KEY, SECRET_KEYS  # noqa: E402

Base.metadata.create_all(engine)

_TEST_AUTH = "unit" + "-report-auth"
_HOST = "mail.example.test"
_FROM = "reports@example.test"
_RECIPIENT = "ops@example.test"

_SETTING_KEYS = [
    "smtp.host", "smtp.port", "smtp.security", "smtp.user",
    "smtp.password", "smtp.from_email", "scheduler.recipients",
    "scheduler.subject_template", "scheduler.body_template",
    "scheduler.last_problem_keys", "scheduler.last_run_at",
    "scheduler.last_run_status", "scheduler.last_run_message",
]


def _health_report(problems: list | None = None, **summary_overrides) -> dict:
    """Отчёт инструмента get_infrastructure_health (структура tools.py)."""
    problems = problems if problems is not None else [
        {"time": "2026-10-19 07:10", "host": "srv-01",
         "problem": "CPU load high", "severity": "High", "ack": False},
        {"time": "2026-10-19 07:20", "host": "srv-02",
         "problem": "Disk almost full", "severity": "Disaster", "ack": True},
        {"time": "2026-10-19 07:30", "host": "srv-03",
         "problem": "Memory usage", "severity": "Average", "ack": False},
        {"time": "2026-10-19 07:40", "host": "srv-04",
         "problem": "Interface errors", "severity": "Warning", "ack": False},
    ]
    summary = {
        "total_active_devices": 42,
        "zabbix_alerts": len(problems),
        "datastore_low_space": 1,
        "host_high_load": 2,
        "vms_with_snapshots": 3,
    }
    summary.update(summary_overrides)
    return {
        "timestamp": "2026-10-19 08:05:11",
        "summary": summary,
        "zabbix_problems": problems,
        "vmware_issues": {
            "datastores_low_space": [{"source": "vc-1",
                                      "datastore": "ds-01",
                                      "free_percent": 7.5}],
            "hosts_high_load": [{"source": "vc-1", "host": "esx-1",
                                 "cpu_percent": 91.0, "mem_percent": 55.0}],
            "vms_with_snapshots": [{"source": "vc-1", "vm": "vm-1",
                                    "snapshot_count": 2,
                                    "oldest": "2026-09-01"}],
        },
        "inventory_note": "Обнаружено проблемных областей: 6.",
    }


class FakeSmtp:
    """Подмена smtplib.SMTP/SMTP_SSL: запоминает письмо, сети нет."""

    instances: list["FakeSmtp"] = []
    fail_on_send = ""

    def __init__(self, host, port, timeout=None, context=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.calls: list[str] = []
        self.login_args = None
        self.message = None
        FakeSmtp.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def ehlo(self):
        self.calls.append("ehlo")

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, auth_value):
        self.calls.append("login")
        self.login_args = (user, auth_value)

    def send_message(self, message):
        if self.fail_on_send:
            import smtplib

            raise smtplib.SMTPDataError(550, self.fail_on_send.encode("ascii"))
        self.calls.append("send_message")
        self.message = message


def _patch_smtp(monkeypatch) -> None:
    FakeSmtp.fail_on_send = ""
    FakeSmtp.instances.clear()
    monkeypatch.setattr(delivery.smtplib, "SMTP", FakeSmtp)
    monkeypatch.setattr(delivery.smtplib, "SMTP_SSL", FakeSmtp)


def _clear_settings() -> None:
    with SessionLocal() as db:
        db.query(AppSetting).filter(
            AppSetting.key.in_(_SETTING_KEYS)).delete(synchronize_session=False)
        db.query(AuditLog).filter(
            AuditLog.tool == jobs.AUDIT_TOOL).delete(synchronize_session=False)
        db.commit()


@pytest.fixture(autouse=True)
def clean_state():
    _clear_settings()
    FakeSmtp.instances.clear()
    yield
    _clear_settings()
    FakeSmtp.instances.clear()


def _seed_report_settings(**overrides) -> None:
    values = {
        "smtp.host": _HOST,
        "smtp.port": "25",
        "smtp.security": "none",
        "smtp.user": "",
        "smtp.from_email": _FROM,
        "scheduler.recipients": _RECIPIENT,
    }
    values.update(overrides)
    settings_store.set_settings(values, extra_secret_keys=SECRET_KEYS)


def _patch_execute_tool(monkeypatch) -> dict:
    """Подмена execute_tool: состояние теста меняет state['raw'/'status']."""
    state = {
        "raw": json.dumps(_health_report(), ensure_ascii=False),
        "status": "ok",
        "error": None,
        "calls": [],
    }

    def fake_execute_tool(name, args, user_id=None, conversation_id=None,
                          user_role="viewer"):
        state["calls"].append({"name": name, "args": args,
                               "user_role": user_role})
        if state["error"] is not None:
            raise state["error"]
        return state["raw"], state["status"]

    monkeypatch.setattr(tools_module, "execute_tool", fake_execute_tool)
    return state


def _last_audit() -> AuditLog | None:
    with SessionLocal() as db:
        return (db.query(AuditLog)
                .filter(AuditLog.tool == jobs.AUDIT_TOOL)
                .order_by(AuditLog.id.desc()).first())


def _setting(key: str, default: str = "") -> str:
    return settings_store.get_setting(key, default)


# --------------------------------------------------------------------------- #
# Успешный запуск
# --------------------------------------------------------------------------- #

def test_run_morning_report_success(monkeypatch, caplog):
    _patch_smtp(monkeypatch)
    state = _patch_execute_tool(monkeypatch)
    _seed_report_settings()

    result = jobs.run_morning_report()

    assert result["ok"] is True
    assert result["status"] == "ok"
    assert result["sent_to"] == [_RECIPIENT]
    assert isinstance(result["duration_ms"], int)
    assert "Отчёт отправлен" in result["message"]

    # execute_tool вызван как у агента: admin, пустые аргументы
    assert state["calls"] == [{"name": "get_infrastructure_health",
                               "args": {}, "user_role": "admin"}]

    # письмо собрано и «отправлено»
    assert len(FakeSmtp.instances) == 1
    msg = FakeSmtp.instances[0].message
    assert msg["To"] == _RECIPIENT
    assert msg["From"] == _FROM
    assert "утренний отчёт" in msg["Subject"]
    assert "19.10.2026" in msg["Subject"]
    assert "2 критичных" in msg["Subject"]
    body = msg.get_content()
    assert "Устройств в инвентаре (активных): 42" in body
    assert "Критичные (2):" in body
    assert "srv-01: CPU load high" in body

    # снимок проблем сохранён (для следующего diff)
    snapshot = json.loads(_setting("scheduler.last_problem_keys"))
    assert "CPU load high @ srv-01" in snapshot
    assert len(snapshot) == 4

    # статус последнего запуска
    assert _setting("scheduler.last_run_status") == "ok"
    assert _setting("scheduler.last_run_at")
    assert "Отчёт отправлен" in _setting("scheduler.last_run_message")

    # аудит
    audit = _last_audit()
    assert audit is not None
    assert audit.status == "ok"
    assert audit.duration_ms is not None and audit.duration_ms >= 0
    assert json.loads(audit.arguments) == {"trigger": "scheduler"}

    assert _TEST_AUTH not in caplog.text      # секрет не настроен/не логируется


def test_run_morning_report_diff_new_and_resolved(monkeypatch):
    _patch_smtp(monkeypatch)
    state = _patch_execute_tool(monkeypatch)
    _seed_report_settings(**{
        "scheduler.body_template":
            "Новых: {{ new_problems_count }}; "
            "устранено: {{ resolved_count }}\n"
            "Новые:\n{{ new_problems_text }}\n"
            "Устранённые:\n{{ resolved_text }}",
    })

    first = _health_report(problems=[
        {"time": "2026-10-19 07:10", "host": "srv-01",
         "problem": "Problem A", "severity": "High", "ack": False},
        {"time": "2026-10-19 07:20", "host": "srv-02",
         "problem": "Problem B", "severity": "Warning", "ack": False},
    ])
    state["raw"] = json.dumps(first, ensure_ascii=False)
    assert jobs.run_morning_report()["ok"] is True
    assert sorted(json.loads(_setting("scheduler.last_problem_keys"))) == [
        "Problem A @ srv-01", "Problem B @ srv-02"]

    # Вторая проблема устранена, появилась третья.
    second = _health_report(problems=[
        {"time": "2026-10-19 07:10", "host": "srv-01",
         "problem": "Problem A", "severity": "High", "ack": False},
        {"time": "2026-10-19 08:10", "host": "srv-03",
         "problem": "Problem C", "severity": "Average", "ack": False},
    ])
    state["raw"] = json.dumps(second, ensure_ascii=False)
    result = jobs.run_morning_report()

    assert result["ok"] is True
    body = FakeSmtp.instances[-1].message.get_content()
    assert "Новых: 1; устранено: 1" in body
    assert "- Problem C @ srv-03" in body
    assert "Problem B @ srv-02" in body      # устранённая проблема в письме
    assert sorted(json.loads(_setting("scheduler.last_problem_keys"))) == [
        "Problem A @ srv-01", "Problem C @ srv-03"]


def test_run_morning_report_snapshot_limit_500(monkeypatch):
    _patch_smtp(monkeypatch)
    state = _patch_execute_tool(monkeypatch)
    _seed_report_settings()

    problems = [
        {"time": "2026-10-19 07:00", "host": f"host-{index:03d}",
         "problem": f"Problem {index}", "severity": "Warning", "ack": False}
        for index in range(600)
    ]
    state["raw"] = json.dumps(_health_report(problems=problems),
                              ensure_ascii=False)
    result = jobs.run_morning_report()

    assert result["ok"] is True
    snapshot = json.loads(_setting("scheduler.last_problem_keys"))
    assert len(snapshot) == jobs.SNAPSHOT_LIMIT == 500


def test_run_morning_report_subject_is_single_line(monkeypatch):
    _patch_smtp(monkeypatch)
    state = _patch_execute_tool(monkeypatch)
    _seed_report_settings(**{
        "scheduler.subject_template": "Отчёт: {{ problems_text }}",
    })
    problems = [
        {"time": "2026-10-19 07:00", "host": "srv-01",
         "problem": "CPU\nBcc: attacker@example.test", "severity": "High",
         "ack": False},
    ]
    state["raw"] = json.dumps(_health_report(problems=problems),
                              ensure_ascii=False)

    result = jobs.run_morning_report()

    assert result["ok"] is True
    subject = FakeSmtp.instances[-1].message["Subject"]
    assert "\r" not in subject and "\n" not in subject
    assert "Bcc:" in subject            # текст сохранён, но в одной строке


def test_run_morning_report_uses_custom_templates(monkeypatch):
    _patch_smtp(monkeypatch)
    _patch_execute_tool(monkeypatch)
    _seed_report_settings(**{
        "scheduler.subject_template":
            "Своя тема {{ date }} ({{ critical }}/{{ warning }})",
        "scheduler.body_template":
            "Критичных: {{ critical }}; новых: {{ new_problems_count }}; "
            "{{ unknown_variable }}; {{ summary_text }}",
    })

    result = jobs.run_morning_report()

    assert result["ok"] is True
    msg = FakeSmtp.instances[-1].message
    assert msg["Subject"] == "Своя тема 19.10.2026 (2/2)"
    body = msg.get_content()
    assert "Критичных: 2; новых: 4;" in body   # первый прогон: все проблемы новые
    assert "Обнаружено проблемных областей: 6." in body


# --------------------------------------------------------------------------- #
# Ошибки: результат и аудит, без исключений
# --------------------------------------------------------------------------- #

def test_run_morning_report_tool_error_status(monkeypatch):
    _patch_smtp(monkeypatch)
    state = _patch_execute_tool(monkeypatch)
    _seed_report_settings()
    with SessionLocal() as db:
        db.add(AppSetting(key="scheduler.last_problem_keys",
                          value=json.dumps(["old problem @ host"])))
        db.commit()

    state["status"] = "error"
    state["raw"] = "Ошибка выполнения: Zabbix недоступен"

    result = jobs.run_morning_report()

    assert result["ok"] is False and result["status"] == "error"
    assert "статус error" in result["message"]
    # O-2: текст ошибки инструмента — только в серверном логе, не в message
    assert "Zabbix недоступен" not in result["message"]
    assert result["sent_to"] == []
    assert not FakeSmtp.instances
    # снимок не перезаписывается неуспешным прогоном
    assert json.loads(_setting("scheduler.last_problem_keys")) \
        == ["old problem @ host"]
    assert _setting("scheduler.last_run_status") == "error"
    assert "Zabbix недоступен" not in _setting("scheduler.last_run_message")
    audit = _last_audit()
    assert audit is not None and audit.status == "error"
    assert "Zabbix недоступен" not in (audit.result or "")


def test_run_morning_report_invalid_json(monkeypatch):
    _patch_smtp(monkeypatch)
    state = _patch_execute_tool(monkeypatch)
    _seed_report_settings()
    state["raw"] = "это не json"

    result = jobs.run_morning_report()

    assert result["ok"] is False
    assert "некорректный JSON" in result["message"]
    assert not FakeSmtp.instances


def test_run_morning_report_exception_does_not_escape(monkeypatch):
    _patch_smtp(monkeypatch)
    state = _patch_execute_tool(monkeypatch)
    _seed_report_settings()
    state["error"] = RuntimeError("внутренняя деталь, не для письма")

    result = jobs.run_morning_report()

    assert result["ok"] is False and result["status"] == "error"
    assert "внутренняя ошибка" in result["message"]
    assert "внутренняя деталь" not in result["message"]
    assert not FakeSmtp.instances
    audit = _last_audit()
    assert audit is not None and audit.status == "error"


def test_run_morning_report_error_detail_only_in_server_log(monkeypatch,
                                                            caplog):
    """O-2: детали ошибки — только в серверном логе (design §5/§6.7).

    В message/last_run_message/audit_log.result: класс ошибки + короткий
    безопасный текст; полный текст исключения виден лишь в логе сервера.
    """
    _patch_smtp(monkeypatch)
    state = _patch_execute_tool(monkeypatch)
    _seed_report_settings()
    detail = "внутренняя деталь: DB host internal-detail-7"
    state["error"] = scheduler_reports.ReportError(detail)

    with caplog.at_level(logging.WARNING):
        result = jobs.run_morning_report()

    assert result["ok"] is False and result["status"] == "error"
    assert "ReportError" in result["message"]        # класс ошибки виден
    assert "internal-detail-7" not in result["message"]

    last_message = _setting("scheduler.last_run_message")
    assert "ReportError" in last_message
    assert "internal-detail-7" not in last_message

    audit = _last_audit()
    assert audit is not None and audit.status == "error"
    assert "ReportError" in (audit.result or "")
    assert "internal-detail-7" not in (audit.result or "")

    # полный текст ошибки остаётся в серверном логе
    assert "internal-detail-7" in caplog.text


def test_run_morning_report_no_recipients(monkeypatch):
    _patch_smtp(monkeypatch)
    _patch_execute_tool(monkeypatch)
    _seed_report_settings(**{"scheduler.recipients": ""})

    result = jobs.run_morning_report()

    assert result["ok"] is False
    assert "Получатели не заданы" in result["message"]
    assert not FakeSmtp.instances
    assert _setting("scheduler.last_run_status") == "error"


def test_run_morning_report_smtp_failure(monkeypatch):
    _patch_smtp(monkeypatch)
    _patch_execute_tool(monkeypatch)
    _seed_report_settings()
    FakeSmtp.fail_on_send = "mailbox unavailable"

    result = jobs.run_morning_report()

    assert result["ok"] is False
    assert "SMTP: ошибка отправки" in result["message"]
    assert "mailbox unavailable" not in result["message"]
    assert _setting("scheduler.last_problem_keys") in ("", None)
    assert _setting("scheduler.last_run_status") == "error"


def test_run_morning_report_secret_not_in_logs(monkeypatch, caplog):
    _patch_smtp(monkeypatch)
    _patch_execute_tool(monkeypatch)
    _seed_report_settings(**{
        "smtp.user": _FROM,
        SECRET_KEY: _TEST_AUTH,
        "smtp.port": "587",
        "smtp.security": "starttls",
    })
    ciphertext = _setting(SECRET_KEY)
    assert ciphertext and ciphertext != _TEST_AUTH

    with caplog.at_level(logging.DEBUG):
        result = jobs.run_morning_report()

    assert result["ok"] is True
    assert FakeSmtp.instances[-1].login_args == (_FROM, _TEST_AUTH)
    assert _TEST_AUTH not in caplog.text
    assert ciphertext not in caplog.text
    # и статус/аудит не содержат секрета
    assert _TEST_AUTH not in _setting("scheduler.last_run_message")
    assert _TEST_AUTH not in (_last_audit().result or "")


def test_run_morning_report_body_limited(monkeypatch, caplog):
    """C3: итоговое тело письма ограничено MAX_RENDERED_BODY_CHARS."""
    _patch_smtp(monkeypatch)
    _patch_execute_tool(monkeypatch)
    _seed_report_settings()
    huge = "A" * (jobs.MAX_RENDERED_BODY_CHARS + 5000)

    def fake_render(key, facts):
        return huge if key == jobs.KEY_BODY else "Тема отчёта"

    monkeypatch.setattr(jobs, "_render", fake_render)

    with caplog.at_level(logging.WARNING):
        result = jobs.run_morning_report()

    assert result["ok"] is True
    body = FakeSmtp.instances[-1].message.get_content()
    # EmailMessage.set_content добавляет завершающий \n, поэтому сравниваем
    # содержимое без него.
    assert body.rstrip("\n") == huge[:jobs.MAX_RENDERED_BODY_CHARS]
    assert len(body) <= jobs.MAX_RENDERED_BODY_CHARS + 1
    assert "обрезано" in caplog.text
    # в логе длина, а не содержимое
    assert huge not in caplog.text


def test_truncate_body_short_body_unchanged():
    body = "короткое тело"
    assert jobs._truncate_body(body) == body


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
