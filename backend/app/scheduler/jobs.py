"""Задание «утренний отчёт»: health-check -> разбор -> письмо -> результат.

Порядок (реализует backend-dev-scheduler):

1. `execute_tool("get_infrastructure_health", {}, user_role="admin")` —
   единый путь к данным, как у агента (включая мок-режим и аудит);
2. `reports.parse_health_report` — факты для шаблона; сравнение списка
   проблем с предыдущим прогоном (`scheduler.last_run_*` / снимок);
3. шаблоны темы и тела из `app_settings` + `reports.render_template`;
4. `delivery.send_email` — получатели из `scheduler.recipients`;
5. запись результата: `scheduler.last_run_at/status/message` и отдельная
   запись `audit_log` (`tool="morning_report"`, status ok/error), без
   содержимого секретов.

Контракт: `run_morning_report() -> dict` — `{ok, status, message, sent_to,
duration_ms}`, не бросает исключений наружу (ошибки — в status/message).
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def run_morning_report() -> dict:
    """Собрать и отправить утренний отчёт. Ошибки — в результате, не raise."""
    return {
        "ok": False,
        "status": "error",
        "message": "Утренний отчёт ещё не реализован (Этап 22 в работе)",
        "sent_to": [],
        "duration_ms": 0,
    }
