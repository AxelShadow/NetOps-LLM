import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from pathlib import Path
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from .api.devices import router as devices_router
from .ui.home import router as home_router
from .ui.router import router as ui_router
from .db import Base, engine, SessionLocal
from .models import User, Role
from .auth.routes import router as auth_router
from .api.chat import router as chat_router
from .api.internal import router as internal_router
from .auth.ldap_auth import split_upn
from .config import get_settings

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


def bootstrap_admin():
    """Первый администратор из NETOPS_BOOTSTRAP_ADMIN, если таблицы пустые."""
    s = get_settings()
    parsed = split_upn(s.bootstrap_admin) if s.bootstrap_admin else None
    if not parsed:
        return
    name, _ = parsed
    with SessionLocal() as db:
        if db.query(User).filter(User.username == name).first():
            return
        db.add(User(username=name, display_name=name, role=Role.admin,
                    granted_by="bootstrap"))
        db.commit()
        log.info("Создан bootstrap-администратор: %s", name)


# Колонки messages, добавленные после создания таблицы (alembic нет):
# имя -> тип для ALTER TABLE ADD COLUMN
_MESSAGE_MIGRATIONS = [
    ("tool_calls", "TEXT"),
    ("tool_call_id", "VARCHAR(64)"),
    ("name", "VARCHAR(64)"),
]


def _ensure_message_columns():
    """Добавляет новые колонки в существующую таблицу messages."""
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    if "messages" not in insp.get_table_names():
        return
    existing = {c["name"] for c in insp.get_columns("messages")}
    with engine.begin() as conn:
        for col, col_type in _MESSAGE_MIGRATIONS:
            if col not in existing:
                conn.execute(text(
                    f"ALTER TABLE messages ADD COLUMN {col} {col_type}"))
                log.info("Добавлена колонка messages.%s", col)


# Колонка audit_log.duration_ms, добавленная после создания таблицы:
_AUDIT_MIGRATIONS = [
    ("duration_ms", "INTEGER"),
]


def _ensure_audit_columns():
    """Добавляет новые колонки в существующую таблицу audit_log."""
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    if "audit_log" not in insp.get_table_names():
        return
    existing = {c["name"] for c in insp.get_columns("audit_log")}
    with engine.begin() as conn:
        for col, col_type in _AUDIT_MIGRATIONS:
            if col not in existing:
                conn.execute(text(
                    f"ALTER TABLE audit_log ADD COLUMN {col} {col_type}"))
                log.info("Добавлена колонка audit_log.%s", col)


# Колонки devices, добавленные на этапе SNMP (эти же DDL валидны для sqlite и
# postgres: DEFAULT применяется и к существующим строкам, и к новым вставкам).
_DEVICE_MIGRATIONS = [
    ("snmp_version", "VARCHAR(4) DEFAULT '2c'"),
    ("snmp_community", "VARCHAR(64) DEFAULT 'public'"),
    # Этап 20: протоколы, MAC (discovery-дедуп), DNS-имя (discovery)
    ("protocol", "VARCHAR(16) DEFAULT ''"),
    ("mac", "VARCHAR(17)"),
    ("dns_name", "VARCHAR(128) DEFAULT ''"),
]


def _ensure_device_columns(eng=None):
    """Добавляет SNMP-колонки в существующую таблицу devices (прод без alembic).

    eng передаётся только из теста (проверка «старой» БД на отдельном
    движке); при старте приложения/сида используется глобальный engine.
    """
    from sqlalchemy import inspect, text
    eng = eng or engine
    insp = inspect(eng)
    if "devices" not in insp.get_table_names():
        return
    existing = {c["name"] for c in insp.get_columns("devices")}
    with eng.begin() as conn:
        for col, col_type in _DEVICE_MIGRATIONS:
            if col not in existing:
                conn.execute(text(
                    f"ALTER TABLE devices ADD COLUMN {col} {col_type}"))
                log.info("Добавлена колонка devices.%s", col)


def _ensure_printer_enum_value(eng=None):
    """Расширяет postgres-ENUM devicetype новыми значениями.

    На postgres колонка devices.type — нативный тип devicetype: новое
    значение Python-enum без ALTER TYPE делает INSERT невозможным
    ('printer' — этап SNMP; 'aruba'/'edgecore' — Этап 20). На sqlite
    тип — VARCHAR (значение проходит всегда), тихо пропускаем.
    eng — только для тестов, как выше.
    """
    from sqlalchemy import text
    eng = eng or engine
    if eng.dialect.name != "postgresql":
        return
    for value in ("printer", "aruba", "edgecore"):
        try:
            # ALTER TYPE ADD VALUE не управляется транзакцией (до PG 12),
            # поэтому отдельное соединение с автокоммитом, а не begin()
            with eng.connect().execution_options(
                    isolation_level="AUTOCOMMIT") as conn:
                conn.execute(text(
                    f"ALTER TYPE devicetype ADD VALUE IF NOT EXISTS "
                    f"'{value}'"))
            log.info("ENUM devicetype расширен значением '%s'", value)
        except Exception as e:
            # IF NOT EXISTS покрывает «значение уже есть» на PG 9.5+;
            # на старых версиях это DuplicateObject — тоже норма
            log.warning("Не удалось расширить ENUM devicetype значением "
                        "%s (возможно, уже есть): %s", value, e)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    Base.metadata.create_all(engine)
    _ensure_message_columns()
    _ensure_audit_columns()
    _ensure_device_columns()
    _ensure_printer_enum_value()
    bootstrap_admin()
    yield


app = FastAPI(title="NetOps LLM", lifespan=lifespan)
app.include_router(auth_router)
app.include_router(chat_router)
app.include_router(devices_router)
app.include_router(internal_router)
app.include_router(ui_router)
app.include_router(home_router)

from fastapi.responses import JSONResponse  # noqa: E402


@app.exception_handler(StarletteHTTPException)
async def _admin_http_exception(request, exc: StarletteHTTPException):
    """Для страниц /admin/*: 401 — редирект на логин, 403 — HTML-страница.

    API-роуты (/api/*, /internal/*) продолжают получать дефолтный JSON-ответ.
    """
    if request.url.path.startswith("/admin"):
        if exc.status_code == 401:
            from fastapi.responses import RedirectResponse
            return RedirectResponse("/admin/login", status_code=303)
        if exc.status_code == 403:
            from .ui.router import templates
            return templates.TemplateResponse(
                request, "pages/403.html", {"request": request},
                status_code=403)
    # Дефолтное поведение (как без обработчика): JSON с деталью
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                        headers=exc.headers)


_ADMIN_STATIC = Path(__file__).resolve().parent / "static"
app.mount("/admin/static", StaticFiles(directory=str(_ADMIN_STATIC)),
          name="admin_static")

# Старый SPA — доступен на /legacy (Фаза 14: NETOPS_USE_NEW_UI=false
# делает его основным). В прод-контейнере frontend/ нет — статику
# отдаёт nginx, mount тихо пропускается.
FRONTEND_LEGACY = Path(__file__).resolve().parents[2] / "frontend" / "legacy"
if FRONTEND_LEGACY.exists():
    app.mount("/legacy", StaticFiles(directory=str(FRONTEND_LEGACY),
              html=True), name="frontend_legacy")
