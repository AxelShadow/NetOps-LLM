import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from pathlib import Path
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from .api.devices import router as devices_router
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


@asynccontextmanager
async def lifespan(_app: FastAPI):
    Base.metadata.create_all(engine)
    _ensure_message_columns()
    bootstrap_admin()
    yield


app = FastAPI(title="NetOps LLM", lifespan=lifespan)
app.include_router(auth_router)
app.include_router(chat_router)
app.include_router(devices_router)
app.include_router(internal_router)
app.include_router(ui_router)

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

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR),
              html=True), name="frontend")
