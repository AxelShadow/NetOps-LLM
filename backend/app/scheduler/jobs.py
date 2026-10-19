"""Задание «утренний отчёт»: health-check -> разбор -> письмо -> результат.

Порядок:

1. `execute_tool("get_infrastructure_health", {}, user_role="admin")` —
   единый путь к данным, как у агента (включая мок-режим и аудит);
2. `reports.parse_health_report` — факты для шаблона; сравнение списка
   проблем с предыдущим прогоном (снимок `scheduler.last_problem_keys`);
3. шаблоны темы и тела из `app_settings` + `reports.render_template`;
4. `delivery.send_email` — получатели из `scheduler.recipients`;
5. запись результата: `scheduler.last_run_at/status/message` и отдельная
   запись `audit_log` (`tool="morning_report"`, status ok/error), без
   содержимого секретов.

Контракт: `run_morning_report() -> dict` — `{ok, status, message, sent_to,
duration_ms}`, не бросает исключений наружу (ошибки — в status/message).

Запускается и планировщиком (в фоновом потоке), и кнопкой «Запустить
отчёт сейчас», и тестами — поэтому вся побочная работа обёрнута в try:
падение записи аудита/снимка не должно превращать отправленное письмо в
ошибку.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import time

from ..db import SessionLocal
from ..models import AuditLog
from ..settings_store import get_setting, set_settings
from . import reports
from .delivery import DeliveryError, send_email, validate_recipients
from .settings import DEFAULTS

log = logging.getLogger(__name__)

REPORT_TOOL = "get_infrastructure_health"
AUDIT_TOOL = "morning_report"

KEY_ENABLED = "scheduler.enabled"
KEY_TIME = "scheduler.time"
KEY_SUBJECT = "scheduler.subject_template"
KEY_BODY = "scheduler.body_template"
KEY_RECIPIENTS = "scheduler.recipients"
KEY_SNAPSHOT = "scheduler.last_problem_keys"
KEY_LAST_AT = "scheduler.last_run_at"
KEY_LAST_STATUS = "scheduler.last_run_status"
KEY_LAST_MESSAGE = "scheduler.last_run_message"

# Снимок проблем для следующего diff. 500 ключей * ~120 симв. ~ 60 КБ —
# с запасом помещается в TEXT app_settings.value.
SNAPSHOT_LIMIT = 500
# Длина сообщения о запуске в app_settings/аудите (без секретов, но не даём
# разрастись ошибочному тексту инструмента).
MESSAGE_MAX_CHARS = 1000
# C3: верхний предел итогового тела письма. Шаблон редактируется в админке,
# поэтому `{{ "A" * 3000000 }}` не должен уходить в SMTP/Zimbra. При
# превышении тело обрезается, в серверный лог пишется предупреждение
# (длина, без содержимого).
MAX_RENDERED_BODY_CHARS = 100_000


def _setting(key: str) -> str:
    """Значение из app_settings с дефолтом реестра (пустое -> дефолт)."""
    default = DEFAULTS.get(key, "")
    try:
        return get_setting(key, default)
    except Exception:
        log.warning("Не удалось прочитать настройку %s, использую дефолт", key)
        return default


def _load_snapshot() -> list[str]:
    """Список ключей проблем предыдущего успешного прогона."""
    raw = _setting(KEY_SNAPSHOT)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        log.warning("Снимок %s повреждён — считаю прошлый прогон пустым",
                    KEY_SNAPSHOT)
        return []
    if not isinstance(data, list):
        return []
    return [str(item) for item in data if str(item or "").strip()]


def _save_snapshot(keys: list[str]) -> None:
    """Записывает снимок проблем (лимит SNAPSHOT_LIMIT), не валит запуск."""
    payload = json.dumps(keys[:SNAPSHOT_LIMIT], ensure_ascii=False)
    try:
        set_settings({KEY_SNAPSHOT: payload})
    except Exception:
        log.exception("Не удалось сохранить снимок проблем %s", KEY_SNAPSHOT)


def _public_error(exc: Exception) -> str:
    """Короткий безопасный текст ошибки для пользователя и аудита.

    В `message`/`scheduler.last_run_message`/`audit_log.result` попадают
    только класс ошибки и короткий public-текст (design §5/§6.7): полное
    содержимое исключения (детали инструмента, ответы внешних систем) —
    только в серверный лог. Для `ReportError` берём явно заданный `.public`,
    для `DeliveryError` — уже безопасный текст (без реквизитов SMTP).
    """
    kind = type(exc).__name__
    if isinstance(exc, DeliveryError):
        text = str(exc).strip()
    else:
        public = getattr(exc, "public", None)
        text = public.strip() if isinstance(public, str) else ""
    return f"{kind}: {text}" if text else kind


def _fetch_health() -> str:
    """Отчёт инструмента `get_infrastructure_health` (JSON-строка).

    execute_tool импортируется внутри функции: модуль задания импортируется
    из lifespan, но не должен тянуть агентские зависимости при простом
    импорте (и остаётся легко подменяемым в тестах).

    Детали ошибки инструмента пишутся в серверный лог, а в исключение
    попадает только публичная часть (status), чтобы текст не утёк в
    последнее сообщение отчёта и аудит.

    Raises:
        reports.ReportError: инструмент вернул не-ok статус.
    """
    from ..agent.tools import execute_tool

    # truncate=False: утренний отчёт разбирает JSON целиком. Дефолтная обрезка
    # execute_tool (MAX_RESULT) режет длинный отчёт на середине -> битый JSON.
    raw, status = execute_tool(
        REPORT_TOOL, {}, user_id=None, conversation_id=None,
        user_role="admin", truncate=False)
    # Диагностика крупных ответов: только статус и длина, без содержимого.
    log.info("Инструмент %s: статус %s, длина ответа %d символов",
             REPORT_TOOL, status, len(raw or ""))
    if status != "ok":
        detail = str(raw or "").strip()
        if detail:
            # Полный текст ошибки инструмента — только серверный лог.
            log.warning("Инструмент %s вернул статус %s: %s", REPORT_TOOL,
                        status, detail[:MESSAGE_MAX_CHARS])
        raise reports.ReportError(
            f"инструмент {REPORT_TOOL}: статус {status}"
            + (f": {detail[:MESSAGE_MAX_CHARS]}" if detail else ""),
            public=f"инструмент {REPORT_TOOL} вернул статус {status}",
        )
    return raw


def _apply_diff(facts: dict) -> dict:
    """Дополняет факты новыми/устранёнными проблемами относительно снимка."""
    keys = list(facts.get("problem_keys") or [])
    previous = _load_snapshot()
    new, resolved = reports.diff_with_previous(keys, previous)
    facts["new_problems"] = new
    facts["resolved_problems"] = resolved
    facts["new_problems_count"] = len(new)
    facts["resolved_count"] = len(resolved)
    if new:
        facts["new_problems_text"] = "\n".join(f"- {key}" for key in new)
    else:
        facts["new_problems_text"] = "Новых проблем нет."
    if resolved:
        facts["resolved_text"] = "\n".join(f"- {key}" for key in resolved)
    else:
        facts["resolved_text"] = "Устранённых проблем нет."
    return facts


def _sanitize_subject(value: str) -> str:
    """Тема — одна строка без CR/LF (данные отчёта не должны ломать заголовок)."""
    return " ".join(str(value or "").split())


def _truncate_body(body: str) -> str:
    """C3: ограничивает итоговое тело письма MAX_RENDERED_BODY_CHARS.

    Шаблон из админки может раздуть письмо на мегабайты; SMTP/Zimbra такое
    не примет. Тело обрезается, в серверный лог пишется только длина
    (без содержимого и секретов).
    """
    text = body if isinstance(body, str) else str(body or "")
    if len(text) <= MAX_RENDERED_BODY_CHARS:
        return text
    log.warning("Тело утреннего отчёта обрезано: %d символов (лимит %d)",
                len(text), MAX_RENDERED_BODY_CHARS)
    return text[:MAX_RENDERED_BODY_CHARS]


def _render(key: str, facts: dict) -> str:
    """Рендер темы/тела по шаблону из админки (дефолт при пустом значении)."""
    template = _setting(key)
    return reports.render_template(template, facts)


def _record_result(status: str, message: str, duration_ms: int) -> str:
    """Пишет scheduler.last_run_* и audit_log; возвращает время запуска.

    `message` уже должен быть безопасным для показа в админке (класс ошибки +
    короткий текст); полные детали остаются в серверном логе. Никогда не
    поднимает исключение: результат задания важнее телеметрии.
    """
    finished_at = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    text = (message or "")[:MESSAGE_MAX_CHARS]
    try:
        set_settings({
            KEY_LAST_AT: finished_at,
            KEY_LAST_STATUS: status,
            KEY_LAST_MESSAGE: text,
        })
    except Exception:
        log.exception("Не удалось сохранить статус запуска утреннего отчёта")
    try:
        with SessionLocal() as db:
            db.add(AuditLog(
                user_id=None, conversation_id=None, tool=AUDIT_TOOL,
                arguments=json.dumps({"trigger": "scheduler"},
                                     ensure_ascii=False),
                result=text, status=status, duration_ms=duration_ms))
            db.commit()
    except Exception:
        log.exception("Не удалось записать аудит утреннего отчёта")
    return finished_at


def run_morning_report() -> dict:
    """Собрать и отправить утренний отчёт. Ошибки — в результате, не raise."""
    started = time.monotonic()
    status = "error"
    message = ""
    sent_to: list[str] = []

    try:
        raw = _fetch_health()
        facts = reports.parse_health_report(raw)
        facts = _apply_diff(facts)
        subject = _sanitize_subject(_render(KEY_SUBJECT, facts))
        body = _truncate_body(_render(KEY_BODY, facts))
        recipients = validate_recipients(_setting(KEY_RECIPIENTS))
        send_email(subject, body, recipients)
        _save_snapshot(list(facts.get("problem_keys") or []))
        sent_to = recipients
        status = "ok"
        message = (
            f"Отчёт отправлен: получателей {len(recipients)}; "
            f"критичных {facts.get('critical', 0)}, "
            f"предупреждений {facts.get('warning', 0)}, "
            f"новых {facts.get('new_problems_count', 0)}, "
            f"устранено {facts.get('resolved_count', 0)}."
        )
    except (reports.ReportError, DeliveryError) as exc:
        # Полный текст ошибки — только в серверный лог; в сообщение/аудит —
        # класс ошибки и короткий безопасный текст (design §5/§6.7).
        log.warning("Утренний отчёт: %s: %s", type(exc).__name__, exc)
        message = f"Отчёт не отправлен: {_public_error(exc)}"
    except Exception as exc:   # неизвестная ошибка — не валим задание/приложение
        message = (f"Отчёт не отправлен: внутренняя ошибка "
                   f"({type(exc).__name__}), подробности в логе сервера")
        log.exception("Утренний отчёт: неожиданная ошибка")

    duration_ms = int((time.monotonic() - started) * 1000)
    _record_result(status, message, duration_ms)
    log.info("Утренний отчёт: статус=%s, длительность=%d мс", status,
             duration_ms)
    return {
        "ok": status == "ok",
        "status": status,
        "message": message,
        "sent_to": sent_to,
        "duration_ms": duration_ms,
    }
