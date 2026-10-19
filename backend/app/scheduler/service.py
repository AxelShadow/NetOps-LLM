"""Жизненный цикл APScheduler для утреннего отчёта.

Правила:

* старт/остановка вызываются из `main.py::lifespan`; при
  `NETOPS_SCHEDULER_ENABLED=false` (и в тестах) планировщик не создаётся —
  импорт модуля не требует БД, SMTP и сети;
* настройки читаются из `app_settings` (см. `scheduler/settings.py`), поэтому
  изменение времени/включённости в админке не требует перезапуска: админка
  вызывает `apply_settings()` после сохранения;
* одна и та же точка входа — `jobs.run_morning_report()` — для планового
  запуска, ручной кнопки «Отправить сейчас» и тестов.

Публичный контракт (его используют админка и QA):

* `start_scheduler()`, `stop_scheduler()` — идемпотентны;
* `get_state() -> dict` — `{enabled, running, next_run, jobs, last_run}`;
* `apply_settings()` — пересоздаёт задание по текущим app_settings;
* `run_job_now() -> dict` — ручной запуск, возвращает результат задания.
"""
from __future__ import annotations

import logging

from ..config import get_settings

log = logging.getLogger(__name__)


def _enabled() -> bool:
    return bool(get_settings().scheduler_enabled)


def start_scheduler() -> None:
    """Старт планировщика (no-op, если выключен)."""
    if not _enabled():
        log.info("Планировщик отчётов выключен (NETOPS_SCHEDULER_ENABLED=false)")
        return
    log.warning("Планировщик отчётов ещё не реализован (Этап 22 в работе)")


def stop_scheduler() -> None:
    """Остановка планировщика (идемпотентно)."""
    return None


def apply_settings() -> dict:
    """Перечитать app_settings и пересоздать задание; возвращает статус."""
    return {"applied": False,
            "message": "Планировщик отчётов ещё не реализован (Этап 22)"}


def get_state() -> dict:
    """Состояние для дашборда (без секретов)."""
    return {
        "enabled": _enabled(),
        "running": False,
        "next_run": None,
        "jobs": [],
        "last_run": {
            "at": "",
            "status": "",
            "message": "",
        },
        "message": "Планировщик отчётов ещё не реализован (Этап 22)",
    }


def run_job_now() -> dict:
    """Ручной запуск утреннего отчёта (кнопка «Отправить сейчас»)."""
    return {"ok": False,
            "message": "Планировщик отчётов ещё не реализован (Этап 22)"}
