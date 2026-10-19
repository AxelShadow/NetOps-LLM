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

Два независимых выключателя:
  * `NETOPS_SCHEDULER_ENABLED` (env) — мастер-выключатель на уровне сервера:
    `false` = планировщик не создаётся вообще, кнопки «Запустить» работают;
  * `scheduler.enabled` (app_settings, админка) — включает/выключает именно
    плановый запуск; при отсутствии строки наследует env-значение.

Публичный контракт (его используют админка и QA):

* `start_scheduler()`, `stop_scheduler()` — идемпотентны;
* `get_state() -> dict` — `{enabled, running, next_run, jobs, last_run}`;
* `apply_settings()` — пересоздаёт задание по текущим app_settings;
* `run_job_now() -> dict` — ручной запуск, возвращает результат задания.
"""
from __future__ import annotations

import logging
import re
import threading

from ..config import get_settings
from ..settings_store import get_bool_setting, get_setting
from .settings import DEFAULTS, TIME_RE

log = logging.getLogger(__name__)

JOB_ID = "morning_report"
JOB_NAME = "Утренний отчёт по инфраструктуре"

KEY_ENABLED = "scheduler.enabled"
KEY_TIME = "scheduler.time"
KEY_LAST_AT = "scheduler.last_run_at"
KEY_LAST_STATUS = "scheduler.last_run_status"
KEY_LAST_MESSAGE = "scheduler.last_run_message"

# Один экземпляр планировщика на процесс. Лок — на случай, если UI
# (fastapi threadpool) и lifespan дёрнут старт одновременно.
_scheduler = None
_lock = threading.RLock()
_last_start_error = ""


def _enabled() -> bool:
    """Мастер-выключатель сервера (env NETOPS_SCHEDULER_ENABLED)."""
    return bool(get_settings().scheduler_enabled)


def _safe_setting(key: str, default: str = "") -> str:
    """Чтение app_settings без падения: до create_all таблицы может не быть."""
    try:
        return get_setting(key, default)
    except Exception:
        log.debug("Настройка %s недоступна (нет таблицы app_settings?)", key)
        return default


def _safe_bool_setting(key: str, default: bool = False) -> bool:
    try:
        return get_bool_setting(key, default)
    except Exception:
        log.debug("Настройка %s недоступна (нет таблицы app_settings?)", key)
        return default


def _runtime_enabled() -> bool:
    """Плановый запуск включён в админке (по умолчанию — env-значение)."""
    return _safe_bool_setting(KEY_ENABLED, _enabled())


def _timezone():
    """Часовой пояс планировщика из NETOPS_SCHEDULER_TZ (fallback UTC)."""
    from zoneinfo import ZoneInfo

    name = (get_settings().scheduler_tz or "UTC").strip()
    try:
        return ZoneInfo(name)
    except Exception:
        log.warning("Неизвестный часовой пояс планировщика %r, использую UTC",
                    name)
        return ZoneInfo("UTC")


def _schedule_time() -> tuple[int, int, str]:
    """Время запуска HH:MM из app_settings; невалидное -> дефолт env/08:00."""
    default = ((get_settings().scheduler_time_default or "")
               .strip() or DEFAULTS[KEY_TIME])
    raw = (_safe_setting(KEY_TIME, default) or "").strip()
    match = re.match(TIME_RE, raw)
    if match is None:
        log.warning("Некорректное время запуска %r — использую %s",
                    raw, default)
        raw = default
        match = re.match(TIME_RE, raw)
    if match is None:   # дефолт тоже битый (env) — жёсткий запас 08:00
        raw = DEFAULTS[KEY_TIME]
        match = re.match(TIME_RE, raw)
    return int(match.group(1)), int(match.group(2)), raw


def _build_scheduler():
    """AsyncIOScheduler в таймзоне сервиса; None, если APScheduler недоступен."""
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
    except Exception:
        log.exception("APScheduler недоступен — планировщик отчётов не запущен")
        return None
    return AsyncIOScheduler(timezone=_timezone())


def _refresh_job(scheduler) -> dict:
    """Синхронизирует единственное задание с текущими app_settings."""
    try:
        scheduler.remove_job(JOB_ID)
    except Exception:
        pass  # задания ещё нет — это нормально

    if not _runtime_enabled():
        return {"scheduled": False, "message": "Плановый запуск выключен"}

    hour, minute, _raw = _schedule_time()
    try:
        from apscheduler.triggers.cron import CronTrigger

        from .jobs import run_morning_report

        scheduler.add_job(
            run_morning_report,
            CronTrigger(hour=hour, minute=minute, timezone=_timezone()),
            id=JOB_ID,
            name=JOB_NAME,
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=3600,
        )
    except Exception:
        log.exception("Не удалось добавить задание утреннего отчёта")
        return {"scheduled": False,
                "message": "Не удалось добавить задание отчёта (см. лог)"}
    return {"scheduled": True, "time": f"{hour:02d}:{minute:02d}"}


def start_scheduler() -> None:
    """Старт планировщика (no-op, если выключен; идемпотентно)."""
    global _scheduler, _last_start_error

    if not _enabled():
        log.info("Планировщик отчётов выключен (NETOPS_SCHEDULER_ENABLED=false)")
        return
    if not _runtime_enabled():
        log.info("Утренний отчёт выключен в настройках (scheduler.enabled=false)")
        return

    with _lock:
        if _scheduler is not None:
            info = _refresh_job(_scheduler)
            log.info("Планировщик уже запущен, задание обновлено: %s", info)
            return

        scheduler = None
        try:
            scheduler = _build_scheduler()
            if scheduler is None:
                _last_start_error = "APScheduler недоступен"
                return
            _refresh_job(scheduler)
            scheduler.start()
            _scheduler = scheduler
            _last_start_error = ""
            hour, minute, _raw = _schedule_time()
            log.info("Планировщик утренних отчётов запущен: %02d:%02d (%s)",
                     hour, minute, get_settings().scheduler_tz)
        except Exception as exc:
            _last_start_error = (
                f"Не удалось запустить планировщик ({type(exc).__name__})")
            log.exception("Не удалось запустить планировщик утренних "
                          "отчётов: приложение продолжает работу")
            _scheduler = None
            if scheduler is not None:
                try:
                    scheduler.shutdown(wait=False)
                except Exception:
                    pass


def stop_scheduler() -> None:
    """Остановка планировщика (идемпотентно, ошибки только логируются)."""
    global _scheduler

    with _lock:
        scheduler = _scheduler
        _scheduler = None
    if scheduler is None:
        return
    try:
        scheduler.shutdown(wait=False)
        log.info("Планировщик утренних отчётов остановлен")
    except Exception:
        log.exception("Ошибка при остановке планировщика утренних отчётов")


def _jobs_state(scheduler) -> list[dict]:
    jobs: list[dict] = []
    try:
        for job in scheduler.get_jobs():
            next_run = getattr(job, "next_run_time", None)
            jobs.append({
                "id": job.id,
                "name": job.name,
                "next_run": next_run.isoformat(timespec="seconds")
                if next_run else None,
            })
    except Exception:
        log.exception("Не удалось получить список заданий планировщика")
    return jobs


def _next_run(scheduler) -> str | None:
    try:
        job = scheduler.get_job(JOB_ID)
    except Exception:
        return None
    next_run = getattr(job, "next_run_time", None) if job is not None else None
    return next_run.isoformat(timespec="seconds") if next_run else None


def get_state() -> dict:
    """Состояние для дашборда/админки (без секретов, не бросает)."""
    env_enabled = _enabled()
    runtime_enabled = _runtime_enabled()
    scheduler = _scheduler
    running = False
    next_run: str | None = None
    jobs: list[dict] = []
    if scheduler is not None:
        try:
            running = bool(scheduler.running)
        except Exception:
            running = False
        jobs = _jobs_state(scheduler)
        next_run = _next_run(scheduler)

    hour, minute, time_text = _schedule_time()
    tz_name = (get_settings().scheduler_tz or "UTC").strip()
    last_run = {
        "at": _safe_setting(KEY_LAST_AT, DEFAULTS[KEY_LAST_AT]),
        "status": _safe_setting(KEY_LAST_STATUS, DEFAULTS[KEY_LAST_STATUS]),
        "message": _safe_setting(KEY_LAST_MESSAGE,
                                 DEFAULTS[KEY_LAST_MESSAGE]),
    }

    effective = env_enabled and runtime_enabled
    if not env_enabled:
        message = ("Планировщик выключен на сервере "
                   "(NETOPS_SCHEDULER_ENABLED=false)")
    elif not runtime_enabled:
        message = "Плановый запуск выключен в настройках отчёта"
    elif _last_start_error:
        message = _last_start_error
    elif running and next_run:
        message = f"Следующий запуск: {next_run}"
    elif running:
        message = "Планировщик запущен, задание не запланировано"
    else:
        message = "Планировщик не запущен"

    return {
        "enabled": effective,
        "env_enabled": env_enabled,
        "runtime_enabled": runtime_enabled,
        "running": running,
        "next_run": next_run,
        "jobs": jobs,
        "last_run": last_run,
        "time": f"{hour:02d}:{minute:02d}",
        "timezone": tz_name,
        "message": message,
        "start_error": _last_start_error,
    }


def apply_settings() -> dict:
    """Перечитать app_settings и пересоздать задание; возвращает статус.

    Вызывается админкой после сохранения формы «Утренний отчёт». Никогда не
    бросает: в ответе `applied=False` и понятное `message` при проблеме.
    """
    try:
        env_enabled = _enabled()
        runtime_enabled = _runtime_enabled()
        time_text = _schedule_time()[2]

        if not env_enabled:
            stop_scheduler()
            return {
                "applied": False,
                "enabled": False,
                "running": False,
                "next_run": None,
                "time": time_text,
                "message": ("Планировщик выключен на сервере "
                            "(NETOPS_SCHEDULER_ENABLED=false) — "
                            "настройки сохранены, плановый запуск не активен"),
            }

        if not runtime_enabled:
            stop_scheduler()
            return {
                "applied": True,
                "enabled": False,
                "running": False,
                "next_run": None,
                "time": time_text,
                "message": "Плановый запуск выключен в настройках",
            }

        if _scheduler is None:
            start_scheduler()
        else:
            with _lock:
                _refresh_job(_scheduler)

        state = get_state()
        return {
            "applied": True,
            "enabled": state["enabled"],
            "running": state["running"],
            "next_run": state["next_run"],
            "time": state["time"],
            "message": state["message"],
        }
    except Exception as exc:
        log.exception("Не удалось применить настройки планировщика")
        return {
            "applied": False,
            "enabled": False,
            "running": False,
            "next_run": None,
            "message": f"Не удалось применить настройки планировщика "
                       f"({type(exc).__name__})",
        }


def run_job_now() -> dict:
    """Ручной запуск утреннего отчёта (кнопка «Запустить отчёт сейчас»)."""
    try:
        from .jobs import run_morning_report

        result = run_morning_report()
    except Exception:
        log.exception("Ручной запуск утреннего отчёта завершился ошибкой")
        return {
            "ok": False,
            "status": "error",
            "message": "Не удалось выполнить отчёт: внутренняя ошибка "
                       "(подробности в логе сервера)",
            "sent_to": [],
            "duration_ms": 0,
        }
    log.info("Ручной запуск утреннего отчёта: статус=%s",
             result.get("status"))
    return result
