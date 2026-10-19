"""Тесты жизненного цикла планировщика утренних отчётов (Этап 22).

Проверяет `app/scheduler/service.py`:
  * `start_scheduler` не стартует при NETOPS_SCHEDULER_ENABLED=false и при
    выключенном `scheduler.enabled`; идемпотентен (второй вызов не создаёт
    второй планировщик и не дублирует задание);
  * задание `jobs.run_morning_report` ставится по времени `scheduler.time`
    (CronTrigger hour/minute) в таймзоне NETOPS_SCHEDULER_TZ;
  * `apply_settings` пересоздаёт/снимает задание при сохранении формы;
  * `get_state` отдаёт enabled/running/next_run/jobs/last_run;
  * ошибка старта логируется и НЕ роняет приложение;
  * `run_job_now` делегирует заданию и не бросает.

Реальный AsyncIOScheduler поднимается внутри `asyncio.run` (задание ни разу
не выполняется — сеть/SMTP не нужны) и останавливается в том же event loop.
БД: временная sqlite (env ДО импорта app).
Запуск: .venv/Scripts/python.exe -m pytest tests/test_scheduler_service.py -v
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

_TMP = tempfile.mkdtemp(prefix="netops_svc_")
os.environ.setdefault("NETOPS_DATABASE_URL",
                      f"sqlite:///{_TMP}/svc_test.db".replace("\\", "/"))
os.environ.setdefault("NETOPS_DEV_MODE", "true")
os.environ.setdefault("NETOPS_MOCK_MODE", "true")
os.environ.setdefault("NETOPS_AD_DOMAIN", "mock.local")
os.environ.setdefault("NETOPS_BOOTSTRAP_ADMIN", "admin@mock.local")
os.environ.setdefault("NETOPS_JWT_SECRET", "svc" + "-test")
os.environ.setdefault("NETOPS_LLM_BASE_URL", "http://localhost:9/v1")
os.environ.setdefault("NETOPS_SCHEDULER_ENABLED", "false")
os.environ.setdefault("NETOPS_SCHEDULER_TZ", "Europe/Moscow")

from cryptography.fernet import Fernet  # noqa: E402

if not os.environ.get("NETOPS_ENCRYPTION_KEY"):
    os.environ["NETOPS_ENCRYPTION_KEY"] = Fernet.generate_key().decode("ascii")

import pytest  # noqa: E402

from app import settings_store  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import AppSetting  # noqa: E402
from app.scheduler import jobs, service  # noqa: E402

Base.metadata.create_all(engine)

_SETTING_KEYS = [
    "scheduler.enabled", "scheduler.time", "scheduler.recipients",
    "scheduler.subject_template", "scheduler.body_template",
    "scheduler.last_problem_keys", "scheduler.last_run_at",
    "scheduler.last_run_status", "scheduler.last_run_message",
]


def _clear_settings() -> None:
    with SessionLocal() as db:
        db.query(AppSetting).filter(
            AppSetting.key.in_(_SETTING_KEYS)).delete(synchronize_session=False)
        db.commit()


def _put_settings(values: dict) -> None:
    settings_store.set_settings(values)


def _reset_module_state() -> None:
    service.stop_scheduler()
    service._scheduler = None
    service._last_start_error = ""


@pytest.fixture(autouse=True)
def clean_state():
    _reset_module_state()
    _clear_settings()
    yield
    _reset_module_state()
    _clear_settings()


def _enable(monkeypatch, time_text: str = "07:45") -> None:
    """Мастер-выключатель env=true + включённый плановый запуск в админке."""
    monkeypatch.setattr(service, "_enabled", lambda: True)
    _put_settings({"scheduler.enabled": "true", "scheduler.time": time_text})


def _cron_fields() -> dict[str, str]:
    scheduler = service._scheduler
    assert scheduler is not None, "планировщик не запущен"
    job = scheduler.get_job(service.JOB_ID)
    assert job is not None, "задание утреннего отчёта не создано"
    return {field.name: str(field) for field in job.trigger.fields}


# --------------------------------------------------------------------------- #
# Выключатели и идемпотентность
# --------------------------------------------------------------------------- #

def test_start_disabled_by_env(monkeypatch):
    monkeypatch.setattr(service, "_enabled", lambda: False)

    service.start_scheduler()   # не бросает
    service.start_scheduler()   # идемпотентно

    state = service.get_state()
    assert service._scheduler is None
    assert state["running"] is False
    assert state["enabled"] is False
    assert state["env_enabled"] is False
    assert "выключен на сервере" in state["message"]


def test_start_disabled_in_admin_settings(monkeypatch):
    monkeypatch.setattr(service, "_enabled", lambda: True)
    _put_settings({"scheduler.enabled": "false", "scheduler.time": "07:00"})

    service.start_scheduler()

    state = service.get_state()
    assert service._scheduler is None
    assert state["running"] is False
    assert state["enabled"] is False
    assert state["runtime_enabled"] is False
    assert "выключен в настройках" in state["message"]


def test_start_and_stop_idempotent(monkeypatch):
    _enable(monkeypatch)

    async def scenario():
        service.start_scheduler()
        first_scheduler = service._scheduler
        assert first_scheduler is not None

        state = service.get_state()
        assert state["running"] is True
        assert state["enabled"] is True
        assert state["env_enabled"] is True
        assert state["runtime_enabled"] is True
        assert len(state["jobs"]) == 1
        assert state["jobs"][0]["id"] == service.JOB_ID
        assert state["next_run"]

        service.start_scheduler()   # повторный старт
        assert service._scheduler is first_scheduler
        assert len(service.get_state()["jobs"]) == 1

        service.stop_scheduler()
        assert service._scheduler is None
        assert service.get_state()["running"] is False

        service.stop_scheduler()   # повторная остановка
        assert service.get_state()["running"] is False

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# Расписание и таймзона
# --------------------------------------------------------------------------- #

def test_job_scheduled_at_configured_time(monkeypatch):
    _enable(monkeypatch, time_text="07:45")

    async def scenario():
        service.start_scheduler()
        fields = _cron_fields()
        assert fields["hour"] == "7"
        assert fields["minute"] == "45"
        state = service.get_state()
        assert state["time"] == "07:45"
        assert state["timezone"] == "Europe/Moscow"
        assert state["next_run"]
        service.stop_scheduler()

    asyncio.run(scenario())


def test_invalid_time_falls_back_to_default(monkeypatch):
    _enable(monkeypatch, time_text="25:99")

    async def scenario():
        service.start_scheduler()
        fields = _cron_fields()
        assert fields["hour"] == "8"
        assert fields["minute"] == "0"
        assert service.get_state()["time"] == "08:00"
        service.stop_scheduler()

    asyncio.run(scenario())


def test_default_time_used_when_setting_absent(monkeypatch):
    monkeypatch.setattr(service, "_enabled", lambda: True)
    _put_settings({"scheduler.enabled": "true"})

    async def scenario():
        service.start_scheduler()
        assert service.get_state()["time"] == "08:00"
        fields = _cron_fields()
        assert fields["hour"] == "8" and fields["minute"] == "0"
        service.stop_scheduler()

    asyncio.run(scenario())


def test_apply_settings_reschedules_and_toggles(monkeypatch):
    _enable(monkeypatch, time_text="07:45")

    async def scenario():
        service.start_scheduler()

        # Пересохранение формы: новое время
        _put_settings({"scheduler.time": "09:30"})
        info = service.apply_settings()
        assert info["applied"] is True
        assert info["enabled"] is True and info["running"] is True
        fields = _cron_fields()
        assert fields["hour"] == "9" and fields["minute"] == "30"
        assert "09:30" == service.get_state()["time"]

        # Выключение планового запуска
        _put_settings({"scheduler.enabled": "false"})
        info = service.apply_settings()
        assert info["applied"] is True
        assert info["enabled"] is False and info["running"] is False
        assert service._scheduler is None
        assert "выключен" in info["message"]

        # Включение снова с новым временем
        _put_settings({"scheduler.enabled": "true", "scheduler.time": "06:05"})
        info = service.apply_settings()
        assert info["applied"] is True and info["running"] is True
        fields = _cron_fields()
        assert fields["hour"] == "6" and fields["minute"] == "5"
        service.stop_scheduler()

    asyncio.run(scenario())


def test_apply_settings_when_env_disabled(monkeypatch):
    monkeypatch.setattr(service, "_enabled", lambda: False)
    _put_settings({"scheduler.enabled": "true", "scheduler.time": "07:00"})

    info = service.apply_settings()

    assert info["applied"] is False
    assert info["enabled"] is False
    assert info["running"] is False
    assert "выключен на сервере" in info["message"]
    assert service._scheduler is None


# --------------------------------------------------------------------------- #
# get_state
# --------------------------------------------------------------------------- #

def test_get_state_shape_and_last_run(monkeypatch):
    monkeypatch.setattr(service, "_enabled", lambda: False)
    _put_settings({
        "scheduler.last_run_at": "2026-10-19T05:00:00+00:00",
        "scheduler.last_run_status": "ok",
        "scheduler.last_run_message": "Отчёт отправлен: получателей 1",
    })

    state = service.get_state()

    for key in ("enabled", "running", "next_run", "jobs", "last_run"):
        assert key in state
    assert state["next_run"] is None
    assert state["jobs"] == []
    assert state["last_run"] == {
        "at": "2026-10-19T05:00:00+00:00",
        "status": "ok",
        "message": "Отчёт отправлен: получателей 1",
    }
    assert state["time"] == "08:00"


# --------------------------------------------------------------------------- #
# Ошибки старта не роняют приложение
# --------------------------------------------------------------------------- #

def test_start_failure_does_not_raise(monkeypatch):
    _enable(monkeypatch)

    def broken_build_scheduler():
        raise RuntimeError("apscheduler сломался")

    monkeypatch.setattr(service, "_build_scheduler", broken_build_scheduler)

    service.start_scheduler()   # не бросает

    state = service.get_state()
    assert service._scheduler is None
    assert state["running"] is False
    assert state["start_error"]
    assert "Не удалось запустить" in state["message"]


def test_start_without_apscheduler(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(service, "_build_scheduler", lambda: None)

    service.start_scheduler()

    state = service.get_state()
    assert state["running"] is False
    assert "APScheduler" in state["start_error"]


# --------------------------------------------------------------------------- #
# run_job_now
# --------------------------------------------------------------------------- #

def test_run_job_now_delegates(monkeypatch):
    expected = {"ok": True, "status": "ok", "message": "Отчёт отправлен",
                "sent_to": ["ops@example.test"], "duration_ms": 12}
    monkeypatch.setattr(jobs, "run_morning_report", lambda: dict(expected))

    assert service.run_job_now() == expected


def test_run_job_now_does_not_raise(monkeypatch):
    def broken():
        raise RuntimeError("сбой задания")

    monkeypatch.setattr(jobs, "run_morning_report", broken)

    result = service.run_job_now()

    assert result["ok"] is False and result["status"] == "error"
    assert "внутренняя ошибка" in result["message"]
    assert result["sent_to"] == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
