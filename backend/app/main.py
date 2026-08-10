import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from pathlib import Path
from fastapi.staticfiles import StaticFiles

from .api.devices import router as devices_router
from .db import Base, engine, SessionLocal
from .models import User, Role
from .auth.routes import router as auth_router
from .api.chat import router as chat_router
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


@asynccontextmanager
async def lifespan(_app: FastAPI):
    Base.metadata.create_all(engine)
    bootstrap_admin()
    yield


app = FastAPI(title="NetOps LLM", lifespan=lifespan)
app.include_router(auth_router)
app.include_router(chat_router)
app.include_router(devices_router)


FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR),
              html=True), name="frontend")
