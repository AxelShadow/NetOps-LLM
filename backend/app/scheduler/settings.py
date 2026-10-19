"""Реестр runtime-настроек утреннего отчёта (app_settings).

Единый источник правды для админки (какие поля показывать, какие
ограничения применять) и для планировщика (что читать). Значения — строки;
дефолты — здесь, при отсутствии строки в БД применяется дефолт.

Ключи и назначение:

| Ключ | Дефолт | Смысл |
|---|---|---|
| `scheduler.enabled` | false | включён ли утренний отчёт |
| `scheduler.time` | 08:00 | время запуска HH:MM в таймзоне сервиса |
| `scheduler.recipients` | "" | список адресов через запятую/точку с запятой |
| `scheduler.subject_template` | см. DEFAULTS | шаблон темы письма (Jinja2) |
| `scheduler.body_template` | см. DEFAULTS | шаблон тела письма (Jinja2, текст) |
| `smtp.host` | "" | SMTP-сервер Zimbra |
| `smtp.port` | 587 | порт |
| `smtp.security` | starttls | ssl / starttls / none |
| `smtp.user` | "" | логин учётной записи |
| `smtp.password` | "" | **секрет**: хранится зашифрованным (Fernet) |
| `smtp.from_email` | "" | адрес отправителя (пусто = smtp.user) |

Последний запуск отчёта хранится в ключах `scheduler.last_run_at`,
`scheduler.last_run_status`, `scheduler.last_run_message` — их пишет
планировщик, админка только показывает.
"""
from __future__ import annotations

DEFAULTS: dict[str, str] = {
    "scheduler.enabled": "false",
    "scheduler.time": "08:00",
    "scheduler.recipients": "",
    "scheduler.subject_template": (
        "NetOps: утренний отчёт {{ date }} — "
        "{{ critical }} критичных, {{ warning }} предупреждений"),
    "scheduler.body_template": (
        "Утренний отчёт по инфраструктуре\n"
        "Дата: {{ date }}\n\n"
        "Устройств в инвентаре (активных): {{ total_active_devices }}\n"
        "Алертов Zabbix: {{ zabbix_alerts }} (критичных {{ critical }}, "
        "предупреждений {{ warning }})\n"
        "Новых с прошлого отчёта: {{ new_problems_count }}; "
        "устранено: {{ resolved_count }}\n"
        "Датасторы с малым запасом: {{ datastore_low_space }}\n"
        "Хосты с высокой загрузкой: {{ host_high_load }}\n"
        "ВМ со снапшотами: {{ vms_with_snapshots }}\n\n"
        "Проблемы:\n{{ problems_text }}\n\n"
        "Сводка: {{ summary_text }}\n"),
    "smtp.host": "",
    "smtp.port": "587",
    "smtp.security": "starttls",
    "smtp.user": "",
    "smtp.password": "",
    "smtp.from_email": "",
    "scheduler.last_run_at": "",
    "scheduler.last_run_status": "",
    "scheduler.last_run_message": "",
}

# Секреты: значения шифруются (settings_store.SECRET_KEYS + этот набор).
SECRET_KEY = "smtp.password"
SECRET_KEYS: frozenset[str] = frozenset({SECRET_KEY})

# Ограничения для валидации формы админки.
TIME_RE = r"^([01]\d|2[0-3]):([0-5]\d)$"
SECURITY_MODES = ("starttls", "ssl", "none")
PORT_MIN, PORT_MAX = 1, 65535
RECIPIENTS_MAX = 20            # адресов в одном отчёте
TEMPLATE_MAX_CHARS = 20000     # шаблон письма


def key_names() -> list[str]:
    """Все ключи реестра (для диагностики/админки)."""
    return list(DEFAULTS.keys())
