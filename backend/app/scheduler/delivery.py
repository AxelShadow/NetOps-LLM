"""SMTP-доставка писем (Zimbra) для утренних отчётов.

Безопасность:

* реквизиты приходят из `app_settings` (секрет — расшифрованный в
  `settings_store.get_secret_setting`), в логи и исключения не попадают:
  только имена ключей, тип ошибки и SMTP-коды;
* тема и адреса проверяются на переводы строк (защита от инъекции
  заголовков), адреса — простой адресной проверкой;
* таймаут обязателен: отчёт не должен висеть на недоступном сервере;
* письмо — `text/plain` UTF-8 (Zimbra нормально принимает), тема и тело
  рендерятся Jinja2 из шаблона админки.

Публичный контракт: `send_email(subject, body, recipients) -> None` или
`DeliveryError` с понятным текстом.
"""
from __future__ import annotations


class DeliveryError(RuntimeError):
    """Ошибка отправки письма (сообщение без секретов)."""


def validate_recipients(raw: str) -> list[str]:
    """Разбирает строку адресов (запятая/точка с запятой/пробел) и проверяет."""
    raise DeliveryError("Отправка почты ещё не реализована (Этап 22 в работе)")


def send_email(subject: str, body: str,
               recipients: list[str] | None = None) -> None:
    """Отправить письмо по SMTP-настройкам из app_settings."""
    raise DeliveryError("Отправка почты ещё не реализована (Этап 22 в работе)")
