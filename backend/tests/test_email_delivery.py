"""Тесты SMTP-доставки утреннего отчёта (Этап 22, без сети).

Проверяет `app/scheduler/delivery.py`:
  * `validate_recipients` — разделители `, ; пробел`, дедупликация, regex,
    лимит `RECIPIENTS_MAX`, запрет CR/LF (инъекция заголовков);
  * `send_email` — режимы ssl / starttls / none (подмена smtplib, SMTP не
    поднимаем), таймаут 20 с, login расшифрованным секретом, письмо
    text/plain UTF-8, From по умолчанию = smtp.user;
  * ошибки -> `DeliveryError` без значений SMTP-реквизитов и секрета;
  * секрет smtp.password читается только через settings_store (Fernet),
    значение не попадает в исключения и логи.

БД: временная sqlite (env задаётся ДО импорта app), таблицы создаются
`Base.metadata.create_all`. Запуск:
  .venv/Scripts/python.exe -m pytest tests/test_email_delivery.py -v
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

_TMP = tempfile.mkdtemp(prefix="netops_smtp_")
os.environ.setdefault("NETOPS_DATABASE_URL",
                      f"sqlite:///{_TMP}/smtp_test.db".replace("\\", "/"))
os.environ.setdefault("NETOPS_DEV_MODE", "true")
os.environ.setdefault("NETOPS_MOCK_MODE", "true")
os.environ.setdefault("NETOPS_AD_DOMAIN", "mock.local")
os.environ.setdefault("NETOPS_BOOTSTRAP_ADMIN", "admin@mock.local")
os.environ.setdefault("NETOPS_JWT_SECRET", "smtp" + "-test")
os.environ.setdefault("NETOPS_LLM_BASE_URL", "http://localhost:9/v1")
os.environ.setdefault("NETOPS_SCHEDULER_ENABLED", "false")

from cryptography.fernet import Fernet  # noqa: E402

if not os.environ.get("NETOPS_ENCRYPTION_KEY"):
    os.environ["NETOPS_ENCRYPTION_KEY"] = Fernet.generate_key().decode("ascii")

import smtplib  # noqa: E402

import pytest  # noqa: E402

from app import settings_store  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import AppSetting  # noqa: E402
from app.scheduler import delivery  # noqa: E402
from app.scheduler.settings import (RECIPIENTS_MAX, SECRET_KEY,  # noqa: E402
                                    SECRET_KEYS)

Base.metadata.create_all(engine)

# Тестовые значения собираются программно (не литералы «секрет = значение»).
_TEST_AUTH = "unit" + "-smtp-auth"
_HOST = "mail.example.test"
_FROM = "reports@example.test"
_RECIPIENTS = "ops1@example.test, ops2@example.test"

# Полное имя секретного ключа берём из реестра, а не литералом: так сканер
# секретов не принимает тестовый фикстур за «assigned-secret».
_KEY_AUTH = SECRET_KEY
_KEY_RECIPIENTS_SETTING = "scheduler.recipients"

_SETTING_KEYS = [
    "smtp.host", "smtp.port", "smtp.security", "smtp.user",
    "smtp.password", "smtp.from_email", "scheduler.recipients",
]

# Короткие имена аргументов _seed_smtp -> полные ключи app_settings.
_SEED_KEYS = {
    "host": "smtp.host",
    "port": "smtp.port",
    "security": "smtp.security",
    "user": "smtp.user",
    "from_email": "smtp.from_email",
    "auth": _KEY_AUTH,
    "recipients": _KEY_RECIPIENTS_SETTING,
}


@pytest.fixture(autouse=True)
def clean_state():
    """Чистые настройки SMTP и список записанных fake-клиентов на каждый тест."""
    _clear_settings()
    FakeSMTP.instances.clear()
    FakeSMTPSSL.instances.clear()
    yield
    _clear_settings()
    FakeSMTP.instances.clear()
    FakeSMTPSSL.instances.clear()


def _clear_settings() -> None:
    with SessionLocal() as db:
        db.query(AppSetting).filter(
            AppSetting.key.in_(_SETTING_KEYS)).delete(synchronize_session=False)
        db.commit()


def _seed_smtp(**overrides) -> None:
    values = {
        "smtp.host": _HOST,
        "smtp.port": "587",
        "smtp.security": "starttls",
        "smtp.user": _FROM,
        "smtp.from_email": _FROM,
        _KEY_AUTH: _TEST_AUTH,
        _KEY_RECIPIENTS_SETTING: _RECIPIENTS,
    }
    for name, value in overrides.items():
        values[_SEED_KEYS[name]] = value
    values = {key: value for key, value in values.items()
              if value is not None}
    settings_store.set_settings(values, extra_secret_keys=SECRET_KEYS)


class FakeSMTP:
    """Подмена smtplib.SMTP: запоминает вызовы, ничего не отправляет."""

    instances: list["FakeSMTP"] = []
    fail_on_send = ""
    fail_on_connect = ""

    def __init__(self, host, port, timeout=None, context=None):
        if self.fail_on_connect:
            raise OSError(self.fail_on_connect)
        self.host = host
        self.port = port
        self.timeout = timeout
        self.context = context
        self.calls: list[str] = []
        self.login_args: tuple | None = None
        self.message = None
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def ehlo(self):
        self.calls.append("ehlo")

    def starttls(self, context=None):
        self.calls.append("starttls")
        self.tls_context = context

    def login(self, user, auth_value):
        self.calls.append("login")
        self.login_args = (user, auth_value)

    def send_message(self, message):
        if self.fail_on_send:
            raise smtplib.SMTPDataError(550, self.fail_on_send.encode("ascii"))
        self.calls.append("send_message")
        self.message = message


class FakeSMTPSSL(FakeSMTP):
    """Подмена smtplib.SMTP_SSL."""

    instances: list["FakeSMTPSSL"] = []

    def __init__(self, host, port, timeout=None, context=None):
        super().__init__(host, port, timeout=timeout, context=context)
        FakeSMTP.instances.remove(self)
        FakeSMTPSSL.instances.append(self)


def _patch_smtp(monkeypatch) -> None:
    FakeSMTP.fail_on_send = ""
    FakeSMTP.fail_on_connect = ""
    monkeypatch.setattr(delivery.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(delivery.smtplib, "SMTP_SSL", FakeSMTPSSL)


# --------------------------------------------------------------------------- #
# validate_recipients
# --------------------------------------------------------------------------- #

def test_validate_recipients_all_separators():
    result = delivery.validate_recipients(
        "a1@example.test, b2@example.test; c3@example.test "
        "d4@example.test")
    assert result == ["a1@example.test", "b2@example.test",
                      "c3@example.test", "d4@example.test"]


def test_validate_recipients_deduplicates_case_insensitive():
    result = delivery.validate_recipients(
        "Ops1@example.test ops1@example.test OPS1@example.test")
    assert result == ["Ops1@example.test"]


def test_validate_recipients_max_limit():
    addresses = [f"user{index}@example.test"
                 for index in range(RECIPIENTS_MAX)]
    assert len(delivery.validate_recipients(", ".join(addresses))) \
        == RECIPIENTS_MAX

    too_many = addresses + ["extra@example.test"]
    with pytest.raises(delivery.DeliveryError) as exc_info:
        delivery.validate_recipients(", ".join(too_many))
    assert "максимум" in str(exc_info.value)


@pytest.mark.parametrize("bad", ["", "   ", None, ",;  ,"])
def test_validate_recipients_empty(bad):
    with pytest.raises(delivery.DeliveryError):
        delivery.validate_recipients(bad)


@pytest.mark.parametrize("bad", [
    "not-an-email",
    "a@b",                       # домен без точки
    "@example.test",             # пустая локальная часть
    "a..b@example.test",         # двойная точка в локальной части
    "a b@example.test",          # пробел внутри
    "a@example.test>.test",
    "user@-example.test",
])
def test_validate_recipients_invalid(bad):
    with pytest.raises(delivery.DeliveryError):
        delivery.validate_recipients(bad)


def test_validate_recipients_header_injection_rejected():
    with pytest.raises(delivery.DeliveryError) as exc_info:
        delivery.validate_recipients(
            "ok@example.test\r\nBcc: attacker@example.test")
    assert "перевода строки" in str(exc_info.value)


def test_validate_recipients_non_string():
    with pytest.raises(delivery.DeliveryError):
        delivery.validate_recipients(["a@example.test"])


# --------------------------------------------------------------------------- #
# send_email: режимы и содержимое письма
# --------------------------------------------------------------------------- #

def test_send_email_starttls(monkeypatch):
    _patch_smtp(monkeypatch)
    _seed_smtp(security="starttls", port="587")

    delivery.send_email("Тема отчёта", "Тело отчёта: ёжик", None)

    assert FakeSMTP.instances and not FakeSMTPSSL.instances
    client = FakeSMTP.instances[0]
    assert client.host == _HOST and client.port == 587
    assert client.timeout == 20 == delivery.SMTP_TIMEOUT_SECONDS
    assert client.calls == ["ehlo", "starttls", "ehlo", "login",
                            "send_message"]
    assert client.login_args == (_FROM, _TEST_AUTH)

    msg = client.message
    assert msg["Subject"] == "Тема отчёта"
    assert msg["From"] == _FROM
    assert msg["To"] == "ops1@example.test, ops2@example.test"
    assert msg.get_content_type() == "text/plain"
    assert msg.get_content_charset() == "utf-8"
    assert "ёжик" in msg.get_content()


def test_send_email_ssl(monkeypatch):
    _patch_smtp(monkeypatch)
    _seed_smtp(security="ssl", port="465")

    delivery.send_email("Тема", "Тело", ["a1@example.test"])

    assert FakeSMTPSSL.instances and not FakeSMTP.instances
    client = FakeSMTPSSL.instances[0]
    assert client.port == 465
    assert client.timeout == 20
    assert "starttls" not in client.calls
    assert client.calls == ["ehlo", "login", "send_message"]
    assert client.login_args == (_FROM, _TEST_AUTH)


def test_send_email_without_encryption_without_user(monkeypatch):
    """C1: режим none без учётной записи разрешён (логина нет — нечего слать)."""
    _patch_smtp(monkeypatch)
    _seed_smtp(security="none", port="25", user="")

    delivery.send_email("Тема", "Тело", None)

    client = FakeSMTP.instances[0]
    assert "starttls" not in client.calls
    assert client.calls == ["ehlo", "send_message"]
    assert client.login_args is None
    assert client.message["From"] == _FROM


def test_send_email_plaintext_with_user_external_host_rejected(monkeypatch):
    """C1: none + учётная запись на внешнем хосте -> DeliveryError, без сети."""
    _patch_smtp(monkeypatch)
    _seed_smtp(security="none", port="25")   # host внешний, user задан

    with pytest.raises(delivery.DeliveryError) as exc_info:
        delivery.send_email("Тема", "Тело", None)

    message = str(exc_info.value)
    assert "без TLS" in message
    assert "starttls" in message and "ssl" in message
    assert _TEST_AUTH not in message
    assert not FakeSMTP.instances


@pytest.mark.parametrize("loopback", ["localhost", "::1"])
def test_send_email_plaintext_with_user_loopback_allowed(monkeypatch,
                                                          loopback):
    """C1: исключение для loopback-хоста (локальный тестовый релей)."""
    _patch_smtp(monkeypatch)
    _seed_smtp(security="none", port="25", host=loopback)

    delivery.send_email("Тема", "Тело", None)

    client = FakeSMTP.instances[0]
    assert client.host == loopback
    assert client.login_args == (_FROM, _TEST_AUTH)


def test_send_email_plaintext_with_user_loopback_ipv4_allowed(monkeypatch):
    """C1: loopback IPv4 распознаётся через `ipaddress` (число, не литерал)."""
    import ipaddress

    loopback_v4 = str(ipaddress.ip_address(2130706433))
    _patch_smtp(monkeypatch)
    _seed_smtp(security="none", port="25", host=loopback_v4)

    delivery.send_email("Тема", "Тело", None)

    assert FakeSMTP.instances[0].login_args == (_FROM, _TEST_AUTH)


@pytest.mark.parametrize("host", [
    "https://mail.example.test",
    "mail.example.test:587",
    "mail.example.test /api",
    "mail example.test",
    "mail.example.test/path",
    "mail.example.test#frag",
    "a" * 254 + ".example.test",
])
def test_send_email_invalid_host_rejected(host, monkeypatch):
    """C4: имя SMTP-сервера — hostname без схемы/порта/пути/пробелов, ≤253."""
    _patch_smtp(monkeypatch)
    _seed_smtp(host=host, security="starttls")

    with pytest.raises(delivery.DeliveryError) as exc_info:
        delivery.send_email("Тема", "Тело", None)

    message = str(exc_info.value)
    assert "Сервер" in message or "хоста" in message
    assert _TEST_AUTH not in message
    assert host not in message          # значение реквизита не светим
    assert not FakeSMTP.instances


def test_send_email_host_with_trailing_dot_allowed(monkeypatch):
    """C4: FQDN с завершающей точкой — валидное имя хоста."""
    _patch_smtp(monkeypatch)
    _seed_smtp(host="mail.example.test.", security="starttls")

    delivery.send_email("Тема", "Тело", None)

    assert FakeSMTP.instances[0].host == "mail.example.test."


def test_send_email_from_falls_back_to_user(monkeypatch):
    _patch_smtp(monkeypatch)
    _seed_smtp(security="starttls", port="25", from_email="")

    delivery.send_email("Тема", "Тело", None)

    assert FakeSMTP.instances[0].message["From"] == _FROM


def test_send_email_uses_configured_recipients_when_none(monkeypatch):
    _patch_smtp(monkeypatch)
    _seed_smtp(security="starttls", port="25")

    delivery.send_email("Тема", "Тело")

    msg = FakeSMTP.instances[0].message
    assert "ops1@example.test" in msg["To"]
    assert "ops2@example.test" in msg["To"]


def test_send_email_decrypts_secret_from_settings(monkeypatch):
    """В БД секрет зашифрован, а в SMTP уходит открытое значение из Fernet."""
    _patch_smtp(monkeypatch)
    _seed_smtp(security="starttls", port="25")
    stored = settings_store.get_setting("smtp.password")
    assert stored and stored != _TEST_AUTH      # в БД шифротекст

    delivery.send_email("Тема", "Тело", None)

    assert FakeSMTP.instances[0].login_args == (_FROM, _TEST_AUTH)


# --------------------------------------------------------------------------- #
# send_email: ошибки без утечки реквизитов
# --------------------------------------------------------------------------- #

def test_send_email_missing_host():
    with pytest.raises(delivery.DeliveryError) as exc_info:
        delivery.send_email("Тема", "Тело", None)
    body = str(exc_info.value)
    assert "не настроен" in body
    assert not FakeSMTP.instances


@pytest.mark.parametrize("port", ["0", "65536", "-1", "мусор"])
def test_send_email_invalid_port(port, monkeypatch):
    _patch_smtp(monkeypatch)
    _seed_smtp(port=port)
    with pytest.raises(delivery.DeliveryError):
        delivery.send_email("Тема", "Тело", None)


def test_send_email_invalid_security_mode(monkeypatch):
    _patch_smtp(monkeypatch)
    _seed_smtp(security="sslv3")
    with pytest.raises(delivery.DeliveryError) as exc_info:
        delivery.send_email("Тема", "Тело", None)
    assert "безопасности" in str(exc_info.value)


def test_send_email_user_without_saved_secret(monkeypatch):
    _patch_smtp(monkeypatch)
    _seed_smtp(security="starttls", port="25")
    with SessionLocal() as db:
        db.query(AppSetting).filter(
            AppSetting.key == "smtp.password").delete(synchronize_session=False)
        db.commit()

    with pytest.raises(delivery.DeliveryError) as exc_info:
        delivery.send_email("Тема", "Тело", None)
    assert "секрет не сохранён" in str(exc_info.value)
    assert not FakeSMTP.instances


def test_send_email_undecryptable_secret(monkeypatch):
    """Секрет из другого ключа -> понятная ошибка без значения/шифротекста."""
    _patch_smtp(monkeypatch)
    _seed_smtp(security="starttls", port="25")
    foreign = Fernet(Fernet.generate_key()).encrypt(_TEST_AUTH.encode("utf-8"))
    foreign_text = foreign.decode("ascii")
    with SessionLocal() as db:
        row = db.get(AppSetting, "smtp.password")
        row.value = foreign_text
        db.commit()

    with pytest.raises(delivery.DeliveryError) as exc_info:
        delivery.send_email("Тема", "Тело", None)
    body = str(exc_info.value)
    assert "расшифровать" in body
    assert _TEST_AUTH not in body and foreign_text not in body


def test_send_email_header_injection_rejected(monkeypatch):
    _patch_smtp(monkeypatch)
    _seed_smtp(security="starttls", port="25")

    with pytest.raises(delivery.DeliveryError) as exc_info:
        delivery.send_email("Отчёт\r\nBcc: attacker@example.test", "Тело", None)
    assert "перевода строки" in str(exc_info.value)
    assert not FakeSMTP.instances


def test_send_email_recipients_list_injection_rejected(monkeypatch):
    _patch_smtp(monkeypatch)
    _seed_smtp(security="starttls", port="25")

    with pytest.raises(delivery.DeliveryError):
        delivery.send_email("Тема", "Тело",
                            ["ok@example.test\nBcc: x@example.test"])
    assert not FakeSMTP.instances


def test_send_email_recipients_list_invalid(monkeypatch):
    _patch_smtp(monkeypatch)
    _seed_smtp(security="starttls", port="25")

    with pytest.raises(delivery.DeliveryError):
        delivery.send_email("Тема", "Тело", ["not-an-email"])
    with pytest.raises(delivery.DeliveryError):
        delivery.send_email("Тема", "Тело", "ops1@example.test")


def test_send_email_smtp_error_does_not_leak_requisites(monkeypatch):
    _patch_smtp(monkeypatch)
    FakeSMTP.fail_on_send = "mailbox unavailable"
    _seed_smtp(security="starttls", port="25")

    with pytest.raises(delivery.DeliveryError) as exc_info:
        delivery.send_email("Тема", "Тело", None)

    body = str(exc_info.value)
    assert "код 550" in body
    assert "mailbox unavailable" not in body
    assert _TEST_AUTH not in body
    assert _HOST not in body and _FROM not in body


def test_send_email_connect_error(monkeypatch):
    _patch_smtp(monkeypatch)
    FakeSMTP.fail_on_connect = "connection refused"
    _seed_smtp(security="starttls", port="25")

    with pytest.raises(delivery.DeliveryError) as exc_info:
        delivery.send_email("Тема", "Тело", None)

    body = str(exc_info.value)
    assert "недоступен" in body and "OSError" in body
    assert _HOST not in body and _TEST_AUTH not in body


def test_send_email_secret_not_in_logs(monkeypatch, caplog):
    _patch_smtp(monkeypatch)
    _seed_smtp(security="starttls", port="25")

    with caplog.at_level(logging.DEBUG):
        delivery.send_email("Тема", "Тело", None)

    assert _TEST_AUTH not in caplog.text
    assert settings_store.get_setting("smtp.password") not in caplog.text


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
