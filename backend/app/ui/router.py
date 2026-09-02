"""Серверная админка на FastAPI + Jinja2 + HTMX (Фаза 2 миграции).

Маршруты /admin/*: логин/логаут (JWT в HttpOnly-cookie netops_token),
каркас разделов (dashboard/инвентарь/диалоги/аудит/настройки) с серверным
RBAC. Контент разделов наполняется в Фазах 3-5; старый SPA
(frontend/index.html) продолжает работать через /api/* без изменений.
"""
import logging
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import SessionLocal
from ..models import Role, User
from ..auth.ldap_auth import ad_authenticate
from ..auth.jwt_utils import create_token
from .deps import COOKIE_NAME, load_user_from_token, get_current_user_page, \
    require_roles_page

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin")

_TPL_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TPL_DIR))

# Пары (имя-пункта, путь, роли с доступом) для navbar и dashboard.
NAV_ITEMS = [
    ("Dashboard", "/admin/", {Role.admin, Role.engineer, Role.viewer}),
    ("Чат", "/chat", {Role.admin, Role.engineer, Role.viewer}),  # Chainlit (Фаза 6+)
    ("Инвентарь", "/admin/inventory", {Role.admin, Role.engineer}),
    ("История диалогов", "/admin/conversations", {Role.admin, Role.engineer}),
    ("Аудит", "/admin/audit", {Role.admin}),
    ("Настройки", "/admin/settings", {Role.admin}),
]


def _ctx(request: Request, user: User, **extra) -> dict:
    """Общий контекст шаблонов: пользователь, пункты меню по роли, настройки."""
    nav = [(label, href) for label, href, roles in NAV_ITEMS
           if user.role in roles]
    ctx = {
        "request": request,
        "user": user,
        "nav": nav,
        "mock_mode": get_settings().mock_mode,
        "is_dev": get_settings().dev_mode,
    }
    ctx.update(extra)
    return ctx


def _render(request: Request, user: User, template: str, ctx: dict,
            status_code: int = 200):
    return templates.TemplateResponse(
        request, template, _ctx(request, user, **ctx), status_code=status_code)


@router.get("/login")
def login_page(request: Request):
    token = request.cookies.get(COOKIE_NAME)
    if token:
        with SessionLocal() as db:
            if load_user_from_token(token, db):
                return RedirectResponse("/admin/", status_code=303)
    return templates.TemplateResponse(request, "pages/login.html",
                                      {"request": request, "error": None})


@router.post("/login")
def login_submit(request: Request, username: str = Form(...),
                 password: str = Form(...)):
    # Тот же вход, что /api/auth/login: ad_authenticate учитывает
    # NETOPS_DEV_MODE (пропуск AD для bootstrap-админа dev-окружения).
    user_data = ad_authenticate(username, password)
    if not user_data:
        return templates.TemplateResponse(
            request, "pages/login.html",
            {"request": request, "error": "Неверный логин или пароль"},
            status_code=401)

    upn = user_data["upn"]
    username_only = upn.split("@")[0].lower()
    with SessionLocal() as db:
        user = db.query(User).filter(
            (User.username == upn.lower()) |
            (User.username == username_only)
        ).first()

        if not user:
            # Авто-регистрация viewer, как в /api/auth/login
            if not get_settings().auto_register_users:
                return templates.TemplateResponse(
                    request, "pages/login.html",
                    {"request": request,
                     "error": "Доступ не предоставлен. Обратитесь к администратору."},
                    status_code=403)
            s = get_settings()
            user = User(username=upn.lower(),
                        display_name=user_data.get("display_name") or upn,
                        role=Role.viewer, granted_by="auto")
            db.add(user)
            db.commit()
            db.refresh(user)
        elif not user.is_active:
            return templates.TemplateResponse(
                request, "pages/login.html",
                {"request": request, "error": "Учётная запись отключена"},
                status_code=403)

        token = create_token(user.id, user.username, user.role.value)

    resp = RedirectResponse("/admin/", status_code=303)
    resp.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax",
                    max_age=get_settings().jwt_hours * 3600)
    return resp


@router.post("/logout")
def logout():
    resp = RedirectResponse("/admin/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME)
    return resp


@router.get("/")
def dashboard(request: Request,
              user: User = Depends(get_current_user_page)):
    return _render(request, user, "pages/dashboard.html", {})


@router.get("/inventory")
def inventory_page(request: Request,
                   user: User = Depends(require_roles_page(
                       Role.admin, Role.engineer))):
    return _render(request, user, "pages/inventory.html", {})


@router.get("/conversations")
def conversations_page(request: Request,
                       user: User = Depends(require_roles_page(
                           Role.admin, Role.engineer))):
    return _render(request, user, "pages/conversations.html", {})


@router.get("/audit")
def audit_page(request: Request,
               user: User = Depends(require_roles_page(Role.admin))):
    return _render(request, user, "pages/audit.html", {})


@router.get("/settings")
def settings_page(request: Request,
                  user: User = Depends(require_roles_page(Role.admin))):
    return _render(request, user, "pages/settings.html", {})


# 401/403 обрабатываются на уровне приложения (main.py): FastAPI не
# позволяет вешать exception_handler на APIRouter. Для /admin/* 401
# превращается в редирект на логин, 403 — в HTML-страницу (не JSON).
