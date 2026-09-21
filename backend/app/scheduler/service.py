"""
APScheduler модуль для планирования задач.
Обеспечивает автоматическую генерацию отчетов и проверки устройств.
"""

import os
from datetime import datetime
from typing import Optional, Callable, Dict, Any
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTION

try:
    from app.db import get_db_session
    from app.models import Device, TaskLog
    from app.config import settings
except ImportError:
    # Для случаев когда импорты недоступны
    get_db_session = None
    Device = None
    TaskLog = None
    settings = type('Settings', (), {
        'SCHEDULER_ENABLED': False,
        'DATABASE_URL': 'sqlite:///./test.db'
    })()


class SchedulerService:
    """Сервис управления планировщиком задач."""

    def __init__(self):
        self.enabled = getattr(settings, 'SCHEDULER_ENABLED', False)
        self.scheduler: Optional[AsyncIOScheduler] = None
        self.jobs: Dict[str, Any] = {}

        if self.enabled:
            self._initialize()

    def _initialize(self):
        """Инициализация планировщика."""
        try:
            self.scheduler = AsyncIOScheduler()
            
            # Подписка на события
            self.scheduler.add_listener(self._job_error_handler, EVENT_JOB_ERROR)
            self.scheduler.add_listener(self._job_execution_handler, EVENT_JOB_EXECUTION)
            
            # Регистрация стандартных задач
            self._register_default_jobs()
            
            print("[Scheduler] APScheduler initialized successfully")
        except Exception as e:
            print(f"[Scheduler] Initialization failed: {e}")
            self.enabled = False

    def _register_default_jobs(self):
        """Регистрация задач по умолчанию."""
        # Еженедельный отчет о статусе устройств (каждый понедельник в 9:00)
        self.add_job(
            job_id="weekly_device_report",
            func=self._generate_weekly_report,
            trigger=CronTrigger(day_of_week='mon', hour=9, minute=0),
            description="Еженедельный отчет о статусе устройств"
        )

        # Ежедневная проверка доступности устройств (каждый день в 8:00)
        self.add_job(
            job_id="daily_device_check",
            func=self._check_devices_availability,
            trigger=CronTrigger(hour=8, minute=0),
            description="Ежедневная проверка доступности устройств"
        )

    def add_job(self, job_id: str, func: Callable, trigger: Any, 
                description: str = "", **kwargs) -> bool:
        """Добавление задачи в планировщик."""
        if not self.enabled or not self.scheduler:
            return False

        try:
            job = self.scheduler.add_job(
                func=func,
                trigger=trigger,
                id=job_id,
                name=description,
                replace_existing=True,
                kwargs=kwargs
            )
            self.jobs[job_id] = job
            print(f"[Scheduler] Job '{job_id}' added: {description}")
            return True
        except Exception as e:
            print(f"[Scheduler] Error adding job '{job_id}': {e}")
            return False

    def remove_job(self, job_id: str) -> bool:
        """Удаление задачи из планировщика."""
        if not self.scheduler or job_id not in self.jobs:
            return False

        try:
            self.scheduler.remove_job(job_id)
            del self.jobs[job_id]
            print(f"[Scheduler] Job '{job_id}' removed")
            return True
        except Exception as e:
            print(f"[Scheduler] Error removing job '{job_id}': {e}")
            return False

    def start(self):
        """Запуск планировщика."""
        if not self.enabled or not self.scheduler:
            return

        try:
            self.scheduler.start()
            print("[Scheduler] Scheduler started")
        except Exception as e:
            print(f"[Scheduler] Error starting scheduler: {e}")

    def shutdown(self, wait: bool = True):
        """Остановка планировщика."""
        if not self.scheduler:
            return

        try:
            self.scheduler.shutdown(wait=wait)
            print("[Scheduler] Scheduler shutdown complete")
        except Exception as e:
            print(f"[Scheduler] Error during shutdown: {e}")

    def _job_error_handler(self, event):
        """Обработчик ошибок выполнения задач."""
        if event.exception:
            print(f"[Scheduler] Job '{event.job_id}' failed with error: {event.traceback}")
            self._log_task_event(event.job_id, "error", str(event.traceback))

    def _job_execution_handler(self, event):
        """Обработчик успешного выполнения задач."""
        print(f"[Scheduler] Job '{event.job_id}' executed successfully")
        self._log_task_event(event.job_id, "success", "Job completed")

    def _log_task_event(self, job_id: str, status: str, message: str):
        """Логирование события выполнения задачи."""
        if not get_db_session or not TaskLog:
            return

        try:
            session = get_db_session()
            log_entry = TaskLog(
                task_name=job_id,
                status=status,
                message=message,
                executed_at=datetime.utcnow()
            )
            session.add(log_entry)
            session.commit()
            session.close()
        except Exception as e:
            print(f"[Scheduler] Error logging task event: {e}")

    async def _generate_weekly_report(self):
        """Генерация еженедельного отчета о статусе устройств."""
        print("[Scheduler] Generating weekly device report...")
        
        if not get_db_session or not Device:
            return

        try:
            session = get_db_session()
            devices = session.query(Device).all()
            
            total = len(devices)
            active = sum(1 for d in devices if getattr(d, 'is_active', False))
            inactive = total - active
            
            report = f"""
            Weekly Device Report
            ====================
            Date: {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}
            
            Total Devices: {total}
            Active: {active}
            Inactive: {inactive}
            
            Status Breakdown:
            -----------------
            """
            
            # Группировка по типам
            by_type = {}
            for device in devices:
                dtype = getattr(device, 'device_type', 'Unknown')
                by_type[dtype] = by_type.get(dtype, 0) + 1
            
            for dtype, count in by_type.items():
                report += f"- {dtype}: {count}\n"
            
            print(report)
            
            # Логирование отчета
            self._log_task_event("weekly_device_report", "success", report)
            
            session.close()
            
        except Exception as e:
            error_msg = f"Error generating report: {str(e)}"
            print(f"[Scheduler] {error_msg}")
            self._log_task_event("weekly_device_report", "error", error_msg)

    async def _check_devices_availability(self):
        """Проверка доступности устройств."""
        print("[Scheduler] Checking device availability...")
        
        if not get_db_session or not Device:
            return

        try:
            session = get_db_session()
            devices = session.query(Device).all()
            
            checked = 0
            unreachable = []
            
            for device in devices:
                checked += 1
                # Здесь можно добавить реальную проверку (ping, SNMP и т.д.)
                # Пока просто эмуляция
                host = getattr(device, 'host', '')
                # if not self._ping_host(host):
                #     unreachable.append(host)
            
            report = f"Checked {checked} devices. Unreachable: {len(unreachable)}"
            if unreachable:
                report += f"\nUnreachable hosts: {', '.join(unreachable)}"
            
            print(f"[Scheduler] {report}")
            self._log_task_event("daily_device_check", "success", report)
            
            session.close()
            
        except Exception as e:
            error_msg = f"Error checking devices: {str(e)}"
            print(f"[Scheduler] {error_msg}")
            self._log_task_event("daily_device_check", "error", error_msg)

    def get_status(self) -> Dict[str, Any]:
        """Получение статуса планировщика."""
        if not self.scheduler:
            return {"enabled": False, "running": False, "jobs": []}

        try:
            jobs_info = []
            for job_id, job in self.jobs.items():
                jobs_info.append({
                    "id": job.id,
                    "name": job.name,
                    "next_run": str(job.next_run_time) if job.next_run_time else None
                })

            return {
                "enabled": self.enabled,
                "running": self.scheduler.running,
                "job_count": len(self.jobs),
                "jobs": jobs_info
            }
        except Exception as e:
            return {"enabled": False, "error": str(e)}


# Глобальный экземпляр сервиса
scheduler_service = SchedulerService()
