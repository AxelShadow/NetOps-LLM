from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from .config import get_settings

s = get_settings()
connect_args = {"check_same_thread": False} if s.database_url.startswith(
    "sqlite") else {}

engine = create_engine(s.database_url, pool_pre_ping=True,
                       connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
