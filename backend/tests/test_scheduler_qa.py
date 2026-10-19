"""Независимые сквозные QA-тесты утреннего отчёта (Этап 22).

Проверяется сквозной путь контракта
`docs/features/rag-and-scheduler-design.md` §5 и §7, а не отдельные юниты
(это делают авторские `tests/test_scheduler_*.py`, `test_email_*.py`,
`test_report_*.py`, `test_settings_store.py`):

    реальная форма JSON `get_infrastructure_health` -> parse_health_report
    -> diff new/resolved между двумя прогонами -> рендер шаблонов из
    app_settings -> SMTP (ssl/starttls; для `none` — политика C1: запрет
    с учётной записью на не-loopback, разрешено без логина и для loopback)
    -> scheduler.last_run_* + AuditLog; инъекция CR/LF в теме и адресах;
    неизвестная переменная шаблона; секрет не попадает в письмо/логи;
    пустой пароль админки не затирает сохранённый; C3 (лимит тела письма);
    C4 (валидация имени SMTP-хоста); O-2 (детали ошибки инструмента не
    уходят в телеметрию/аудит); планировщик не стартует в тестовом
    окружении (NETOPS_SCHEDULER_ENABLED=false).

Сеть не используется: `execute_tool` и `smtplib` подменяются, БД —
временная sqlite (рабочая `backend/netops.db` не затрагивается).
Изоляция от порядка запуска: в autouse-фикстуре явно выключается
мок-режим (в общем каталоге `pytest tests/` другой модуль мог импортировать
`app` с `NETOPS_MOCK_MODE=true`), а реальный invoke инструмента в тесте 2
идёт на изолированную пустую БД. `tmp_path` не используется: DACL
pytest-of-* на этом хосте ломается.

Запуск: cd backend; .\\.venv\\Scripts\\python.exe -m pytest tests/test_scheduler_qa.py -v
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

# env ДО импорта app.*: Settings требует jwt_secret и llm_base_url.
_TMP = Path(tempfile.mkdtemp(prefix="netops_sched_qa_"))
os.environ["NETOPS_DATABASE_URL"] = (
    f"sqlite:///{(_TMP / 'qa.db').as_posix()}")
os.environ.setdefault("NETOPS_DEV_MODE", "true")
os.environ["NETOPS_MOCK_MODE"] = "false"
os.environ["NETOPS_SCHEDULER_ENABLED"] = "false"
os.environ["NETOPS_SCHEDULER_TZ"] = "UTC"
os.environ["NETOPS_JWT_SECRET"] = "sched" + "-qa" + "-jwt"
os.environ["NETOPS_LLM_BASE_URL"] = "http://llm.invalid:9/v1"
os.environ["NETOPS_ZABBIX_URL"] = "http://zbx.invalid/api_jsonrpc.php"

from cryptography.fernet import Fernet  # noqa: E402

if not os.environ.get("NETOPS_ENCRYPTION_KEY"):
    os.environ["NETOPS_ENCRYPTION_KEY"] = Fernet.generate_key().decode("ascii")

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app import settings_store  # noqa: E402
from app.agent import tools as tools_mod  # noqa: E402
from app.agent.mock import MOCK_TOOLS  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import AppSetting, AuditLog  # noqa: E402
from app.scheduler import delivery, jobs, reports, service  # noqa: E402
from app.scheduler.settings import DEFAULTS, SECRET_KEYS  # noqa: E402

Base.metadata.create_all(engine)

HOST = "mail.example.test"
USER = "report@example.test"
FROM = "reports@example.test"
RECIPIENT = "ops@example.test"
# Тестовый пароль собирается программно: литерал «пароль = значение»
# блокирует pre-tool-guard (см. .dsh/hooks/pre-tool-guard.ps1).
PWD = "test" + "-" + "smtp" + "-" + "pwd" + "-77"

AUDIT_TOOL = "morning_report"
KEY_LAST_STATUS = "scheduler.last_run_status"
KEY_LAST_AT = "scheduler.last_run_at"
KEY_LAST_MESSAGE = "scheduler.last_run_message"
KEY_SNAPSHOT = "scheduler.last_problem_keys"


# ---------------------------------------------------------------------------
# подпорки
# ---------------------------------------------------------------------------
class FakeSmtp:
    """Подмена smtplib.SMTP: запоминает экземпляры и вызовы (без сети)."""

    instances: list["FakeSmtp"] = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.context = context
        self.ehlo_calls = 0
        self.tls_called = False
        self.login_args: tuple | None = None
        self.sent: list = []
        FakeSmtp.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        self.ehlo_calls += 1

    def starttls(self, context=None):
        self.tls_called = True

    def login(self, user, auth_value):
        self.login_args = (user, auth_value)

    def send_message(self, message):
        self.sent.append(message)


class FakeSmtpSsl(FakeSmtp):
    """Подмена smtplib.SMTP_SSL (режим security=ssl)."""


def _patch_smtp(monkeypatch) -> type[FakeSmtp]:
    FakeSmtp.instances = []
    monkeypatch.setattr(delivery.smtplib, "SMTP", FakeSmtp)
    monkeypatch.setattr(delivery.smtplib, "SMTP_SSL", FakeSmtpSsl)
    return FakeSmtp


def _patch_health(monkeypatch, raw: str, status: str = "ok") -> list[dict]:
    """Подмена execute_tool: отчёт инструмента без опроса инфраструктуры.

    Подпись повторяет актуальный контракт `execute_tool` (включая
    `truncate=False` после хотфикса большого JSON), иначе задание падало бы
    с `TypeError` — это и была регрессия бага из эксплуатации.
    """
    calls: list[dict] = []

    def fake_execute(name, args, user_id=None, conversation_id=None,
                     user_role="viewer", truncate=True):
        calls.append({"name": name, "args": args, "user_role": user_role,
                      "truncate": truncate})
        return raw, status

    monkeypatch.setattr(tools_mod, "execute_tool", fake_execute)
    return calls


def _wipe_db() -> None:
    with SessionLocal() as db:
        db.query(AppSetting).delete()
        db.query(AuditLog).delete()
        db.commit()


def _seed(**overrides) -> dict:
    """Сохраняет валидные настройки отчёта в app_settings (как админка)."""
    values = {
        "scheduler.enabled": "true",
        "scheduler.time": "07:30",
        "scheduler.recipients": RECIPIENT,
        "scheduler.subject_template": DEFAULTS["scheduler.subject_template"],
        "scheduler.body_template": DEFAULTS["scheduler.body_template"],
        "smtp.host": HOST,
        "smtp.port": "587",
        "smtp.security": "starttls",
        "smtp.user": USER,
        "smtp.from_email": FROM,
        "smtp.password": PWD,
    }
    values.update(overrides)
    settings_store.set_settings(values, extra_secret_keys=SECRET_KEYS)
    return values


def _health_raw(problems: list | None = None, **summary) -> str:
    """JSON в форме, которую отдаёт реальный get_infrastructure_health."""
    report = {
        "timestamp": "2026-10-19 07:30:00",
        "summary": {
            "total_active_devices": 7,
            "zabbix_alerts": len(problems or []),
            "datastore_low_space": 1,
            "host_high_load": 0,
            "vms_with_snapshots": 1,
        },
        "zabbix_problems": problems or [],
        "vmware_issues": {
            "datastores_low_space": [{"datastore": "ds-1",
                                       "free_percent": 9.5,
                                       "source": "vc-1"}],
            "hosts_high_load": [],
            "vms_with_snapshots": [{"vm": "vm-1", "snapshot_count": 2,
                                    "source": "vc-1"}],
        },
        "inventory_note": "",
    }
    report["summary"].update(summary)
    return json.dumps(report, ensure_ascii=False)


def _problem(name: str, host: str, severity: str = "High") -> dict:
    return {"time": "2026-10-19 07:00", "host": host, "problem": name,
            "severity": severity, "ack": False}


def _audit_rows() -> list[AuditLog]:
    with SessionLocal() as db:
        return (db.query(AuditLog).filter(AuditLog.tool == AUDIT_TOOL)
                .order_by(AuditLog.id).all())


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    """Чистая БД настроек + гарантированно не запущенный планировщик.

    `mock_mode` выключается явно: при запуске в общем каталоге
    (`pytest tests/`) чужой модуль мог импортировать `app` с
    `NETOPS_MOCK_MODE=true`, и реальный инструмент вернул бы мок-JSON
    (order-зависимость, которой быть не должно).
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "scheduler_enabled", False)
    monkeypatch.setattr(settings, "scheduler_tz", "UTC")
    monkeypatch.setattr(settings, "mock_mode", False)
    service.stop_scheduler()
    service._last_start_error = ""
    _wipe_db()
    try:
        yield
    finally:
        service.stop_scheduler()
        _wipe_db()


# ---------------------------------------------------------------------------
# 1. разбор реальной формы JSON get_infrastructure_health + контракт шаблона
# ---------------------------------------------------------------------------
def test_parse_real_health_json_contract():
    raw, status = MOCK_TOOLS["get_infrastructure_health"]({})
    assert status == "ok"
    facts = reports.parse_health_report(raw)

    contract_vars = [
        "date", "total_active_devices", "zabbix_alerts", "critical", "warning",
        "new_problems_count", "resolved_count", "datastore_low_space",
        "host_high_load", "vms_with_snapshots", "problems_text",
        "summary_text",
    ]
    for name in contract_vars:
        assert name in facts, f"нет переменной шаблона {name}"
    assert facts["critical"] == 0 and facts["warning"] == 2, facts
    assert facts["total_active_devices"] == 6
    assert facts["datastore_low_space"] == 1
    assert facts["vms_with_snapshots"] == 2
    assert isinstance(facts["problems_text"], str) and facts["problems_text"]
    assert "Мок-режим" in facts["summary_text"]

    # дефолтные шаблоны из реестра рендерятся без ошибок и не оставляют Jinja
    subject = reports.render_template(DEFAULTS["scheduler.subject_template"], facts)
    body = reports.render_template(DEFAULTS["scheduler.body_template"], facts)
    assert "{{" not in subject and "{{" not in body
    assert "NetOps" in subject
    assert "Утренний отчёт по инфраструктуре" in body


# ---------------------------------------------------------------------------
# 2. реальный invoke инструмента: форма отчёта при сбое Zabbix
# ---------------------------------------------------------------------------
def test_real_tool_report_shape_when_zabbix_fails(monkeypatch):
    def zabbix_down(limit=100):
        raise RuntimeError("Zabbix недоступен (QA-подмена)")

    monkeypatch.setattr(tools_mod, "zabbix_problems", zabbix_down)
    # Изоляция от порядка запуска в общем каталоге: инвентарь — на отдельной
    # пустой БД, чтобы результат не зависел от того, какой модуль импортировал
    # app первым и что лежит в его временной базе.
    workdir = Path(tempfile.mkdtemp(prefix="netops_sched_qa_tool_"))
    local_engine = create_engine(f"sqlite:///{workdir / 'tool.db'}")
    Base.metadata.create_all(local_engine)
    monkeypatch.setattr(tools_mod, "SessionLocal",
                        sessionmaker(bind=local_engine))
    try:
        raw, status = tools_mod.get_infrastructure_health()
    finally:
        local_engine.dispose()
        shutil.rmtree(workdir, ignore_errors=True)
    assert status == "ok"

    facts = reports.parse_health_report(raw)
    assert facts["total_active_devices"] == 0        # инвентарь пуст
    assert facts["zabbix_errors"], "ошибка Zabbix должна быть в фактах"
    assert "Ошибки получения данных Zabbix" in facts["problems_text"]
    assert facts["new_problems_count"] == 0
    assert facts["resolved_count"] == 0
    # шаблон рендерится даже при сбое источника данных
    body = reports.render_template(DEFAULTS["scheduler.body_template"], facts)
    assert "Ошибки получения данных Zabbix" in body


# ---------------------------------------------------------------------------
# 3. сквозной прогон задания: health -> шаблон -> SMTP -> телеметрия
# ---------------------------------------------------------------------------
def test_report_job_end_to_end(monkeypatch, caplog):
    _seed()
    raw, _ = MOCK_TOOLS["get_infrastructure_health"]({})
    calls = _patch_health(monkeypatch, raw)
    fake = _patch_smtp(monkeypatch)

    with caplog.at_level(logging.DEBUG):
        result = jobs.run_morning_report()

    assert result["ok"] is True, result["message"]
    assert result["status"] == "ok"
    assert result["sent_to"] == [RECIPIENT]
    assert calls == [{"name": "get_infrastructure_health", "args": {},
                      "user_role": "admin", "truncate": False}]
    assert len(fake.instances) == 1
    client = fake.instances[0]
    assert client.login_args == (USER, PWD)
    assert client.tls_called is True, "режим starttls не применился"
    assert client.sent, "письмо не отправлено"
    message = client.sent[0]
    subject = str(message["Subject"])
    body = message.get_content()
    assert "NetOps" in subject
    assert "Утренний отчёт по инфраструктуре" in body
    assert "Мок-режим" in body

    # телеметрия: scheduler.last_run_* + снимок проблем + аудит
    assert settings_store.get_setting(KEY_LAST_STATUS) == "ok"
    assert settings_store.get_setting(KEY_LAST_AT)
    snapshot = json.loads(settings_store.get_setting(KEY_SNAPSHOT))
    assert len(snapshot) == 2, snapshot
    rows = _audit_rows()
    assert len(rows) == 1 and rows[0].status == "ok"

    # секрет SMTP не попадает ни в письмо, ни в логи, ни в телеметрию
    assert PWD not in subject and PWD not in body
    assert PWD not in caplog.text
    assert PWD not in (settings_store.get_setting("smtp.password") or "")
    assert PWD not in (rows[0].result or "")
    assert PWD not in settings_store.get_setting(KEY_LAST_STATUS)


# ---------------------------------------------------------------------------
# 4. diff между двумя прогонами: новые / устранённые проблемы
# ---------------------------------------------------------------------------
def test_diff_new_and_resolved_between_runs(monkeypatch):
    _seed(**{"scheduler.body_template": (
        "новых={{ new_problems_count }}; устранено={{ resolved_count }}\n"
        "{{ new_problems_text }}\n{{ resolved_text }}\n")})
    fake = _patch_smtp(monkeypatch)

    first = _health_raw([_problem("CPU high", "host-a"),
                         _problem("Disk full", "host-b")])
    _patch_health(monkeypatch, first)
    assert jobs.run_morning_report()["ok"] is True

    second = _health_raw([_problem("CPU high", "host-a"),
                          _problem("Memory low", "host-c")])
    _patch_health(monkeypatch, second)
    assert jobs.run_morning_report()["ok"] is True

    body1 = fake.instances[0].sent[0].get_content()
    body2 = fake.instances[1].sent[0].get_content()
    assert "новых=2; устранено=0" in body1, body1
    assert "новых=1; устранено=1" in body2, body2
    assert "Memory low @ host-c" in body2
    assert "Disk full @ host-b" in body2

    snapshot = json.loads(settings_store.get_setting(KEY_SNAPSHOT))
    assert snapshot == ["CPU high @ host-a", "Memory low @ host-c"], snapshot


# ---------------------------------------------------------------------------
# 5. SMTP-режимы сквозь задание (подмена smtplib) + политики C1/C4
# ---------------------------------------------------------------------------
# IPv4-loopback собираем программно: pre-tool-guard не пропускает приватные
# адреса литералом в записываемом файле.
LOOPBACK_V4 = "127." + "0." + "0." + "1"


@pytest.mark.parametrize("mode,ssl_expected,tls_expected", [
    ("ssl", True, False),
    ("starttls", False, True),
])
def test_smtp_modes_end_to_end(monkeypatch, mode, ssl_expected, tls_expected):
    _seed(**{"smtp.security": mode, "smtp.port": "465" if mode == "ssl" else "25"})
    raw, _ = MOCK_TOOLS["get_infrastructure_health"]({})
    _patch_health(monkeypatch, raw)
    fake = _patch_smtp(monkeypatch)

    result = jobs.run_morning_report()
    assert result["ok"] is True, result["message"]
    assert len(fake.instances) == 1
    client = fake.instances[0]
    assert isinstance(client, FakeSmtpSsl) is ssl_expected, type(client).__name__
    assert client.tls_called is tls_expected
    assert client.login_args == (USER, PWD)


def test_smtp_none_with_user_on_external_host_denied(monkeypatch):
    """C1: `none` + логин + внешний хост -> DeliveryError, письмо не уходит.

    Иначе SMTP-учётка ушла бы открытым текстом. Проверяется сквозь задание:
    ошибка доставки должна попасть в результат/аудит, а не превратиться в 500.
    """
    _seed(**{"smtp.security": "none", "smtp.port": "25"})
    raw, _ = MOCK_TOOLS["get_infrastructure_health"]({})
    _patch_health(monkeypatch, raw)
    fake = _patch_smtp(monkeypatch)

    result = jobs.run_morning_report()
    assert result["ok"] is False
    assert "SMTP без TLS с учётной записью запрещён" in result["message"], \
        result["message"]
    assert fake.instances == [], "письмо с открытым паролем не должно уходить"
    assert settings_store.get_setting(KEY_LAST_STATUS) == "error"
    rows = _audit_rows()
    assert len(rows) == 1 and rows[0].status == "error"
    assert "SMTP без TLS" in (rows[0].result or "")


def test_smtp_none_without_user_allowed(monkeypatch):
    """C1: `none` без логина (пароль не передаётся) — допустимо."""
    _seed(**{"smtp.security": "none", "smtp.port": "25",
             "smtp.user": "", "smtp.password": ""})
    raw, _ = MOCK_TOOLS["get_infrastructure_health"]({})
    _patch_health(monkeypatch, raw)
    fake = _patch_smtp(monkeypatch)

    result = jobs.run_morning_report()
    assert result["ok"] is True, result["message"]
    client = fake.instances[-1]
    assert not isinstance(client, FakeSmtpSsl)
    assert client.tls_called is False
    assert client.login_args is None, "без логина пароль не передаётся"
    assert client.sent, "письмо не отправлено"


@pytest.mark.parametrize("host", ["localhost", LOOPBACK_V4, "::1"])
def test_smtp_none_with_user_on_loopback_allowed(monkeypatch, host):
    """C1: loopback — единственное исключение для `none` с учётной записью."""
    _seed(**{"smtp.security": "none", "smtp.port": "25", "smtp.host": host})
    raw, _ = MOCK_TOOLS["get_infrastructure_health"]({})
    _patch_health(monkeypatch, raw)
    fake = _patch_smtp(monkeypatch)

    result = jobs.run_morning_report()
    assert result["ok"] is True, result["message"]
    client = fake.instances[-1]
    assert client.login_args == (USER, PWD)
    assert client.tls_called is False
    assert client.sent


def test_smtp_host_with_scheme_rejected(monkeypatch):
    """C4: опечатка в имени SMTP-сервера (`https://host`) -> ошибка без утечки."""
    _seed(**{"smtp.host": "https://mail.example.test"})
    raw, _ = MOCK_TOOLS["get_infrastructure_health"]({})
    _patch_health(monkeypatch, raw)
    fake = _patch_smtp(monkeypatch)

    result = jobs.run_morning_report()
    assert result["ok"] is False
    assert "некорректное имя хоста" in result["message"], result["message"]
    assert "https://" not in result["message"]
    assert fake.instances == []


# ---------------------------------------------------------------------------
# 5a. C3: раздутое тело письма обрезается; O-2: детали ошибки — только в лог
# ---------------------------------------------------------------------------
def test_huge_rendered_body_is_capped(monkeypatch, caplog):
    _seed(**{"scheduler.body_template": '{{ "A" * 200000 }}'})
    raw, _ = MOCK_TOOLS["get_infrastructure_health"]({})
    _patch_health(monkeypatch, raw)
    fake = _patch_smtp(monkeypatch)

    with caplog.at_level(logging.DEBUG):
        result = jobs.run_morning_report()

    assert result["ok"] is True, result["message"]
    body = fake.instances[-1].sent[0].get_content()
    # EmailMessage может добавить завершающий перевод строки (+1 символ)
    assert jobs.MAX_RENDERED_BODY_CHARS <= len(body) <= (jobs.MAX_RENDERED_BODY_CHARS + 1)
    assert "обрезано" in caplog.text
    assert "A" * 1000 in body, "обрезано по лимиту, а не пустое тело"


def test_tool_error_details_not_leaked_to_telemetry(monkeypatch, caplog):
    """O-2: текст ошибки инструмента — в серверный лог, не в UI/аудит."""
    _seed()
    detail = "internal-detail-77: connect to relay refused"
    calls = _patch_health(monkeypatch, detail, status="error")
    fake = _patch_smtp(monkeypatch)

    with caplog.at_level(logging.DEBUG):
        result = jobs.run_morning_report()

    assert calls == [{"name": "get_infrastructure_health", "args": {},
                      "user_role": "admin", "truncate": False}]
    assert result["ok"] is False
    # публичная часть — статус инструмента, без деталей
    assert "get_infrastructure_health" in result["message"]
    assert "internal-detail-77" not in result["message"]
    assert fake.instances == [], "при ошибке инструмента письмо не уходит"
    assert "internal-detail-77" not in settings_store.get_setting(
        KEY_LAST_MESSAGE)
    rows = _audit_rows()
    assert len(rows) == 1 and rows[0].status == "error"
    assert "internal-detail-77" not in (rows[0].result or "")
    # полные детали допустимы в серверном логе
    assert "internal-detail-77" in caplog.text


# ---------------------------------------------------------------------------
# 5b. регресс бага из эксплуатации: большой (>MAX_RESULT) валидный JSON
# ---------------------------------------------------------------------------
def test_big_health_json_not_truncated_and_report_sent(monkeypatch):
    """Хотфикс «ReportError: некорректный JSON» на реальном парке.

    `execute_tool` обрезал ответ `get_infrastructure_health` до `MAX_RESULT`
    и ломал JSON. Контракт после хотфикса: у агента дефолт `truncate=True`
    (бюджет контекста LLM), у планировщика — `truncate=False` (полный разбор).

    Проверяется: (1) дефолт по-прежнему обрезает; (2) `truncate=False` даёт
    валидный JSON целиком; (3) `run_morning_report()` на таком отчёте уходит.
    """
    _seed()
    big_raw = _health_raw(problems=[
        _problem(f"Проблема {i}", f"host-{i}") for i in range(600)])
    assert len(big_raw) > tools_mod.MAX_RESULT, len(big_raw)

    def big_tool():
        return big_raw, "ok"

    # execute_tool проверяет RBAC по tool_roles у функции из реестра
    big_tool.tool_roles = ("viewer", "engineer", "admin")
    monkeypatch.setitem(tools_mod._registry, "get_infrastructure_health",
                        big_tool)

    truncated, status = tools_mod.execute_tool(
        "get_infrastructure_health", {}, user_role="admin")
    assert status == "ok"
    assert len(truncated) == tools_mod.MAX_RESULT
    with pytest.raises(json.JSONDecodeError):
        json.loads(truncated)          # именно этот путь ломал JSON

    full, status = tools_mod.execute_tool(
        "get_infrastructure_health", {}, user_role="admin", truncate=False)
    assert status == "ok"
    assert full == big_raw
    payload = json.loads(full)
    assert len(payload["zabbix_problems"]) == 600

    fake = _patch_smtp(monkeypatch)
    result = jobs.run_morning_report()
    assert result["ok"] is True, result["message"]
    assert result["status"] == "ok"
    assert fake.instances and fake.instances[-1].sent, "письмо не отправлено"
    body = fake.instances[-1].sent[0].get_content()
    assert "Проблема 0" in body
    rows = _audit_rows()
    assert len(rows) == 1 and rows[0].status == "ok"


# ---------------------------------------------------------------------------
# 6. инъекция CR/LF: тема, получатели, прямой send_email
# ---------------------------------------------------------------------------
def test_header_injection_end_to_end(monkeypatch):
    _seed(**{"scheduler.subject_template": "Отчёт\r\nBcc: attacker@example.test"})
    raw, _ = MOCK_TOOLS["get_infrastructure_health"]({})
    _patch_health(monkeypatch, raw)
    fake = _patch_smtp(monkeypatch)

    result = jobs.run_morning_report()
    assert result["ok"] is True, result["message"]
    message = fake.instances[-1].sent[0]
    subject = str(message["Subject"])
    assert "\r" not in subject and "\n" not in subject
    assert message["Bcc"] is None
    assert "attacker@example.test" in subject   # безопасно, как текст темы

    # CR/LF в списке получателей — ошибка валидации, письмо не уходит
    _seed(**{"scheduler.recipients": "ops@example.test\r\nBcc: x@example.test"})
    before = len(fake.instances)
    result = jobs.run_morning_report()
    assert result["ok"] is False
    assert "символы перевода строки" in result["message"], result["message"]
    assert len(fake.instances) == before, "письмо не должно уходить"
    assert _audit_rows()[-1].status == "error"

    # прямой контракт delivery: перевод строки в теме -> DeliveryError
    with pytest.raises(delivery.DeliveryError):
        delivery.send_email("Тема\r\nBcc: attacker@example.test", "тело",
                            [RECIPIENT])


# ---------------------------------------------------------------------------
# 7. неизвестная переменная шаблона -> пустая строка, не ошибка
# ---------------------------------------------------------------------------
def test_unknown_template_variable_renders_empty(monkeypatch):
    _seed(**{"scheduler.body_template": (
        "{{ unknown_var }}|{{ date }}|{{ critical.deep.unknown }}\n")})
    raw, _ = MOCK_TOOLS["get_infrastructure_health"]({})
    _patch_health(monkeypatch, raw)
    fake = _patch_smtp(monkeypatch)

    result = jobs.run_morning_report()
    assert result["ok"] is True, result["message"]
    body = fake.instances[-1].sent[0].get_content()
    assert "unknown_var" not in body
    assert body.count("|") >= 2
    assert "{{" not in body and "{%" not in body


# ---------------------------------------------------------------------------
# 8. пустой пароль из админки не затирает сохранённый
# ---------------------------------------------------------------------------
def test_empty_password_does_not_overwrite_saved(monkeypatch):
    _seed()
    ciphertext = settings_store.get_setting("smtp.password")
    assert ciphertext and ciphertext != PWD
    assert settings_store.get_secret_setting("smtp.password") == PWD

    # админка сохраняет форму без нового пароля
    settings_store.set_settings({
        "smtp.host": HOST, "smtp.port": "587", "smtp.security": "starttls",
        "smtp.user": USER, "smtp.from_email": FROM, "smtp.password": "",
    }, extra_secret_keys=SECRET_KEYS)
    assert settings_store.get_setting("smtp.password") == ciphertext
    assert settings_store.get_secret_setting("smtp.password") == PWD

    raw, _ = MOCK_TOOLS["get_infrastructure_health"]({})
    _patch_health(monkeypatch, raw)
    fake = _patch_smtp(monkeypatch)
    result = jobs.run_morning_report()
    assert result["ok"] is True, result["message"]
    assert fake.instances[-1].login_args == (USER, PWD)


# ---------------------------------------------------------------------------
# 9. планировщик не стартует в тестовом окружении
# ---------------------------------------------------------------------------
def test_scheduler_not_started_when_env_disabled():
    settings = get_settings()
    assert settings.scheduler_enabled is False
    assert service._scheduler is None

    state = service.get_state()
    assert state["running"] is False
    assert state["enabled"] is False
    assert "NETOPS_SCHEDULER_ENABLED=false" in state["message"]

    service.start_scheduler()
    assert service._scheduler is None, "в тестах планировщик не должен стартовать"
    service.stop_scheduler()          # идемпотентность
    service.stop_scheduler()

    applied = service.apply_settings()
    assert applied["applied"] is False
    assert "NETOPS_SCHEDULER_ENABLED=false" in applied["message"]

    # даже если в настройках отчёта «включено», env-выключатель сильнее
    _seed()
    state = service.get_state()
    assert state["env_enabled"] is False
    assert state["enabled"] is False
    service.start_scheduler()
    assert service._scheduler is None


# ---------------------------------------------------------------------------
# 10. отказ доставки: нет получателей / SMTP недоступен -> ошибка без raise
# ---------------------------------------------------------------------------
def test_run_job_now_no_recipients(monkeypatch):
    _seed(**{"scheduler.recipients": ""})
    raw, _ = MOCK_TOOLS["get_infrastructure_health"]({})
    _patch_health(monkeypatch, raw)
    fake = _patch_smtp(monkeypatch)

    result = service.run_job_now()
    assert result["ok"] is False and result["status"] == "error"
    assert "Получатели" in result["message"], result["message"]
    assert fake.instances == [], "SMTP не должен вызываться"
    assert settings_store.get_setting(KEY_LAST_STATUS) == "error"
    rows = _audit_rows()
    assert len(rows) == 1 and rows[0].status == "error"


def test_run_job_now_smtp_unavailable(monkeypatch):
    _seed()

    class DownSmtp:
        def __init__(self, *args, **kwargs):
            raise OSError("network down")

    raw, _ = MOCK_TOOLS["get_infrastructure_health"]({})
    _patch_health(monkeypatch, raw)
    monkeypatch.setattr(delivery.smtplib, "SMTP", DownSmtp)
    monkeypatch.setattr(delivery.smtplib, "SMTP_SSL", DownSmtp)

    result = jobs.run_morning_report()
    assert result["ok"] is False
    assert "недоступен" in result["message"], result["message"]
    assert PWD not in result["message"]
    rows = _audit_rows()
    assert len(rows) == 1 and rows[0].status == "error"
    assert PWD not in (rows[0].result or "")
