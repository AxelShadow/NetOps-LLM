"""SMTP-доставка писем (Zimbra) для утренних отчётов.

Безопасность:

* реквизиты приходят из `app_settings` (секрет — расшифрованный в
  `settings_store.get_secret_setting`), в логи и исключения не попадают:
  только имена ключей, тип ошибки и SMTP-коды;
* тема и адреса проверяются на переводы строк (защита от инъекции
  заголовков), адреса — простой адресной проверкой;
* имя SMTP-сервера валидируется (hostname без схемы/порта/пути/пробелов):
  опечатка `https://host` или `host:587` не должна уходить в smtplib;
* режим без TLS (`security=none`) с учётной записью запрещён — иначе
  логин/пароль уходят открытым текстом; исключение только для loopback
  (локальный тестовый SMTP-релей);
* таймаут обязателен: отчёт не должен висеть на недоступном сервере;
* письмо — `text/plain` UTF-8 (Zimbra нормально принимает), тема и тело
  рендерятся Jinja2 из шаблона админки.

Публичный контракт: `send_email(subject, body, recipients) -> None` или
`DeliveryError` с понятным текстом.
"""
from __future__ import annotations

import ipaddress
import logging
import re
import smtplib
import ssl
from email.message import EmailMessage

from ..settings_store import (SecretDecryptError, get_secret_setting,
                              get_setting)
from .settings import (DEFAULTS, PORT_MAX, PORT_MIN, RECIPIENTS_MAX,
                       SECRET_KEY, SECURITY_MODES)

log = logging.getLogger(__name__)

# Таймаут на все сетевые операции SMTP: отчёт не должен висеть на мёртвом
# сервере (задание выполняется в фоновом потоке, но всё равно ограничиваем).
SMTP_TIMEOUT_SECONDS = 20

# Максимальная длина адреса по RFC 5321 и простая проверка локальной части:
# буквы/цифры/спецсимволы без пробелов, домен — метки через точку. Точка в
# локальной части и в начале/конце метки отдельно отсекается ниже.
_ADDRESS_RE = re.compile(
    r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+"
    r"@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")

_RECIPIENTS_SPLIT_RE = re.compile(r"[,;\s]+")

# Имя SMTP-сервера: только hostname (буквы/цифры/дефис, метки через точку),
# без схемы, порта, пути и пробелов (C4). Допускается завершающая точка.
_SMTP_HOST_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\.?$")
_SMTP_HOST_MAX_CHARS = 253

# Loopback-имена SMTP-сервера — единственное исключение для режима без TLS
# с учётной записью (C1). Loopback-адреса распознаются стандартным
# `ipaddress` (`is_loopback`), без хардкода внутренних адресов.
_LOOPBACK_NAMES = frozenset({"localhost"})


def _is_loopback_host(host: str) -> bool:
    """True для loopback-имени/адреса SMTP-сервера (C1)."""
    candidate = str(host or "").strip().lower()
    if candidate in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(candidate.strip("[]")).is_loopback
    except ValueError:
        return False


def _validate_smtp_host(host: str) -> str:
    """C4: проверяет имя SMTP-сервера (hostname, не длиннее 253 символов).

    Raises:
        DeliveryError: пусто, слишком длинно или есть схема/порт/путь/пробелы.
            Значение хоста в текст ошибки не попадает (это реквизит).
    """
    candidate = str(host or "").strip()
    if not candidate:
        raise DeliveryError(
            "SMTP-сервер не настроен: заполните поле «Сервер» (smtp.host)")
    if len(candidate) > _SMTP_HOST_MAX_CHARS:
        raise DeliveryError(
            "SMTP-сервер: слишком длинное имя хоста "
            f"(максимум {_SMTP_HOST_MAX_CHARS} символов)")
    if _is_loopback_host(candidate):
        return candidate
    if not _SMTP_HOST_RE.match(candidate):
        raise DeliveryError(
            "SMTP-сервер: некорректное имя хоста — нужен hostname без схемы, "
            "порта, пути и пробелов")
    return candidate


# Имена ключей app_settings — строки, чтобы тесты/админка не дублировали текст.
_KEY_HOST = "smtp.host"
_KEY_PORT = "smtp.port"
_KEY_SECURITY = "smtp.security"
_KEY_USER = "smtp.user"
_KEY_FROM = "smtp.from_email"
_KEY_RECIPIENTS = "scheduler.recipients"


class DeliveryError(RuntimeError):
    """Ошибка отправки письма (сообщение без секретов)."""


def _is_valid_email(address: str) -> bool:
    """Простая проверка адреса: одна @, валидные локальная часть и домен."""
    if not isinstance(address, str):
        return False
    addr = address.strip()
    if not addr or len(addr) > 254 or " " in addr:
        return False
    if addr.count("@") != 1:
        return False
    local, _domain = addr.rsplit("@", 1)
    if not local or local.startswith(".") or local.endswith(".") \
            or ".." in local:
        return False
    return bool(_ADDRESS_RE.match(addr))


def _check_no_newlines(value: str, what: str) -> None:
    """Запрет CR/LF: защита от инъекции заголовков письма."""
    if not isinstance(value, str):
        raise DeliveryError(f"{what}: ожидалась строка")
    if "\r" in value or "\n" in value:
        raise DeliveryError(
            f"{what}: недопустимые символы перевода строки")


def validate_recipients(raw: str) -> list[str]:
    """Разбирает строку адресов (запятая/точка с запятой/пробел) и проверяет.

    Raises:
        DeliveryError: список пуст, есть невалидный адрес, превышен лимит
            `RECIPIENTS_MAX` или в строке есть CR/LF (инъекция заголовка).
    """
    if raw is None:
        raw = ""
    if not isinstance(raw, str):
        raise DeliveryError("Список получателей должен быть строкой")
    _check_no_newlines(raw, "Список получателей")
    parts = [part.strip() for part in _RECIPIENTS_SPLIT_RE.split(raw)
             if part.strip()]
    if not parts:
        raise DeliveryError(
            "Получатели не заданы: заполните список адресов в настройках "
            "отчёта")
    return _normalize_addresses(parts)


def _normalize_addresses(addresses: list[str]) -> list[str]:
    """Проверка, дедупликация (без учёта регистра) и лимит RECIPIENTS_MAX."""
    normalized: list[str] = []
    seen: set[str] = set()
    for address in addresses:
        addr = str(address or "").strip()
        if not _is_valid_email(addr):
            raise DeliveryError(f"Некорректный адрес получателя: {addr}")
        key = addr.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(addr)
    if not normalized:
        raise DeliveryError("Получатели не заданы")
    if len(normalized) > RECIPIENTS_MAX:
        raise DeliveryError(
            f"Слишком много получателей: {len(normalized)} "
            f"(максимум {RECIPIENTS_MAX})")
    return normalized


def _load_smtp_config() -> dict:
    """SMTP-реквизиты из app_settings; секрет расшифровывается здесь.

    В возвращаемом dict поле `auth_value` — расшифрованный секрет учётной
    записи; наружу (в лог/исключение) оно не выводится.

    Raises:
        DeliveryError: конфигурация неполная/некорректная (включая запрет
            «none» с учётной записью на не-loopback сервере, C1). В тексте
            ошибки нет значений реквизитов (host/port/user/from/секрет),
            только имена полей формы — они и так видны администратору.
    """
    host = _validate_smtp_host(
        (get_setting(_KEY_HOST, DEFAULTS[_KEY_HOST]) or "").strip())

    raw_port = (get_setting(_KEY_PORT, DEFAULTS[_KEY_PORT]) or "").strip()
    try:
        port = int(raw_port)
    except (TypeError, ValueError):
        raise DeliveryError("Порт SMTP должен быть числом") from None
    if not PORT_MIN <= port <= PORT_MAX:
        raise DeliveryError(
            f"Порт SMTP вне диапазона {PORT_MIN}..{PORT_MAX}")

    security = (get_setting(_KEY_SECURITY,
                            DEFAULTS[_KEY_SECURITY]) or "").strip().lower()
    if security not in SECURITY_MODES:
        raise DeliveryError(
            "Недопустимый режим SMTP-безопасности: ожидается один из "
            f"{', '.join(SECURITY_MODES)}")

    user = (get_setting(_KEY_USER, DEFAULTS[_KEY_USER]) or "").strip()
    # C1: без TLS учётные данные уходят открытым текстом. «none» с логином
    # допустим только для loopback-сервера (локальный тестовый релей).
    if security == "none" and user and not _is_loopback_host(host):
        raise DeliveryError(
            "SMTP без TLS с учётной записью запрещён: выберите «starttls» или "
            "«ssl» (режим «none» допустим только для loopback-сервера)")
    from_email = (get_setting(_KEY_FROM, DEFAULTS[_KEY_FROM]) or "").strip()
    if not from_email:
        from_email = user
    if not from_email:
        raise DeliveryError(
            "Не задан адрес отправителя: заполните «От» или «Пользователь»")
    if not _is_valid_email(from_email):
        raise DeliveryError("Некорректный адрес отправителя")

    auth_value = ""
    if user:
        try:
            auth_value = get_secret_setting(SECRET_KEY)
        except SecretDecryptError as exc:
            raise DeliveryError(
                f"Не удалось расшифровать секрет {SECRET_KEY}: "
                "проверьте NETOPS_ENCRYPTION_KEY") from exc
        if not auth_value:
            raise DeliveryError(
                "SMTP-пользователь задан, а секрет не сохранён "
                f"(поле {SECRET_KEY})")

    return {"host": host, "port": port, "security": security,
            "user": user, "auth_value": auth_value, "from_email": from_email}


def _build_message(subject: str, body: str, from_email: str,
                   recipients: list[str]) -> EmailMessage:
    """Собирает text/plain UTF-8 письмо; заголовки уже провалидированы."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = ", ".join(recipients)
    msg.set_content(body or "", subtype="plain", charset="utf-8")
    return msg


def _send_via_smtp(config: dict, msg: EmailMessage) -> None:
    """Одна попытка отправки с выбранным режимом шифрования.

    Исключения smtplib/OS/SSL превращаются в `DeliveryError` без реквизитов:
    только тип ошибки или SMTP-код (текст ответа сервера может содержать
    внутренние детали).
    """
    host = config["host"]
    port = config["port"]
    security = config["security"]
    user = config["user"]
    auth_value = config["auth_value"]
    context = ssl.create_default_context()

    client = None
    try:
        if security == "ssl":
            client = smtplib.SMTP_SSL(host, port,
                                      timeout=SMTP_TIMEOUT_SECONDS,
                                      context=context)
        else:
            client = smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT_SECONDS)
        with client:
            client.ehlo()
            if security == "starttls":
                client.starttls(context=context)
                client.ehlo()
            if user:
                client.login(user, auth_value)
            client.send_message(msg)
    except smtplib.SMTPException as exc:
        code = getattr(exc, "smtp_code", None)
        suffix = f" (код {code})" if code else ""
        raise DeliveryError(
            f"SMTP: ошибка отправки письма{suffix}") from None
    except (OSError, TimeoutError, ssl.SSLError) as exc:
        raise DeliveryError(
            f"SMTP: сервер недоступен ({type(exc).__name__})") from None
    except DeliveryError:
        raise
    except Exception as exc:   # неизвестная ошибка транспорта — не светим детали
        raise DeliveryError(
            f"SMTP: неожиданная ошибка отправки ({type(exc).__name__})"
        ) from None


def send_email(subject: str, body: str,
               recipients: list[str] | None = None) -> None:
    """Отправить письмо по SMTP-настройкам из app_settings.

    Args:
        subject: тема письма (без CR/LF).
        body: текстовое тело письма (UTF-8, переносы разрешены).
        recipients: готовый список адресов; None — взять
            `scheduler.recipients` из app_settings и провалидировать.

    Raises:
        DeliveryError: конфигурация неполная, адрес/тема невалидны или
            отправка не удалась; текст ошибки без значений секретов.
    """
    _check_no_newlines(subject, "Тема письма")
    if not isinstance(body, str):
        raise DeliveryError("Тело письма должно быть строкой")

    config = _load_smtp_config()

    if recipients is None:
        recipients = validate_recipients(
            get_setting(_KEY_RECIPIENTS, DEFAULTS[_KEY_RECIPIENTS]))
    else:
        if not isinstance(recipients, (list, tuple)):
            raise DeliveryError(
                "Получатели должны быть строкой или списком адресов")
        recipients = _normalize_addresses([str(r) for r in recipients])

    msg = _build_message(subject, body, config["from_email"], recipients)
    _send_via_smtp(config, msg)
    log.info("Утренний отчёт отправлен: получателей %d", len(recipients))
