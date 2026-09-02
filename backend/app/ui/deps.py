"""Зависимости для серверных страниц /admin/* (Фаза 2).

Отличие от API-зависимостей (auth/deps.py): JWT читается не только из
заголовка Authorization (старый SPA), но и из HttpOnly-cookie netops_token
(новая админка); при 401 — редирект на /admin/login вместо JSON-ошибки.
"""
import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import User, Role
from ..auth.jwt_utils import decode_token

COOKIE_NAME = "netops_token"


def load_user_from_token(token: str, db: Session) -> User | None:
    """Общая загрузка пользователя по JWT (используется и API, и страницами)."""
    try:
        payload = decode_token(token)
    except jwt.PyJWTError:
        return None
    user = db.get(User, int(payload["sub"]))
    if not user or not user.is_active:
        return None
    return user


def get_current_user_page(
    request: Request,
    db: Session = Depends(get_db),
) -> User:
    """Пользователь из cookie netops_token; иначе — редирект на логин.

    Cookie имеет приоритет: на страницах админки токен всегда берётся из
    неё. Заголовок Authorization не рассматривается, чтобы браузерная
    сессия не смешивалась с API-сессией SPA.
    """
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise _login_redirect()
    user = load_user_from_token(token, db)
    if not user:
        raise _login_redirect()
    return user


def _login_redirect() -> RedirectResponse:
    from fastapi import HTTPException as _HTTPException  # noqa: F401
    # FastAPI-редирект через HTTPException невозможен, поэтому используем
    # сигнальный статус: маршруты-обёртки ниже превращают 401 в редирект.
    raise HTTPException(status_code=401, detail="Не авторизован")


def require_roles_page(*allowed: Role):
    """Фабрика зависимостей: серверный RBAC для страниц /admin/*.

    403 рендерится страницей pages/403.html (обёртка в router.py ловит
    HTTPException(403) и отдаёт HTML).
    """
    def dep(user: User = Depends(get_current_user_page)) -> User:
        if user.role not in allowed:
            raise HTTPException(status_code=403, detail="Доступ запрещён")
        return user
    return dep
