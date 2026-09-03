"""Серверная админка на FastAPI + Jinja2 + HTMX (Фаза 2 миграции).

Маршруты /admin/*: логин/логаут (JWT в HttpOnly-cookie netops_token),
каркас разделов (dashboard/инвентарь/диалоги/аудит/настройки) с серверным
RBAC. Контент разделов наполняется в Фазах 3-5; старый SPA
(frontend/index.html) продолжает работать через /api/* без изменений.
"""
import logging
from datetime import datetime, time as dtime
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import SessionLocal, get_db
from ..models import AuditLog, Role, User
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
               db: Session = Depends(get_db),
               user: User = Depends(require_roles_page(Role.admin))):
    tools = [row[0] for row in db.query(AuditLog.tool).distinct().order_by(AuditLog.tool)]
    return _render(request, user, "pages/audit.html", {"tools": tools})


@router.get("/settings")
def settings_page(request: Request,
                  user: User = Depends(require_roles_page(Role.admin))):
    return _render(request, user, "pages/settings.html", {})


# --- Аудит: HTMX-фрагменты (Фаза 4 миграции) -------------------------------

_AUDIT_PAGE_SIZE = 25


def _duration_display(ms: int | None) -> str:
    """Человеческий вид длительности: 123 мс / 1.5 с / «—» (None)."""
    if ms is None:
        return "—"
    if ms < 1000:
        return f"{ms} мс"
    return f"{ms / 1000:.1f} с"


def _audit_query(db: Session, user_f: str | None, tool_f: str | None,
                 status_f: str | None, date_from: datetime | None,
                 date_to: datetime | None, dialog: int | None):
    """Базовый запрос аудита с фильтрами (join User для username)."""
    q = (
        db.query(AuditLog, User.username)
        .outerjoin(User, AuditLog.user_id == User.id)
        .order_by(AuditLog.id.desc())
    )
    if user_f:
        q = q.filter(User.username.ilike(f"%{user_f}%"))
    if tool_f:
        q = q.filter(AuditLog.tool == tool_f)
    if status_f:
        q = q.filter(AuditLog.status == status_f)
    if date_from:
        q = q.filter(AuditLog.created_at >= date_from)
    if date_to:
        # date_to — inclusive: включаем весь указанный день
        q = q.filter(AuditLog.created_at <
                      datetime.combine(date_to, dtime(hour=23, minute=59,
                                                       second=59)))
    if dialog:
        q = q.filter(AuditLog.conversation_id == dialog)
    return q


@router.get("/audit/partial/table")
def audit_table(request: Request,
                db: Session = Depends(get_db),
                user: User = Depends(require_roles_page(Role.admin)),
                page: int = Query(default=1, ge=1),
                user_f: str | None = Query(default=None, alias="user"),
                tool_f: str | None = Query(default=None, alias="tool"),
                status_f: str | None = Query(default=None, alias="status"),
                date_from: str | None = Query(default=None),
                date_to: str | None = Query(default=None),
                dialog: int | None = Query(default=None)):
    # input[type=date] шлёт YYYY-MM-DD; битые значения игнорируем
    try:
        d_from = datetime.strptime(date_from, "%Y-%m-%d") if date_from else None
    except ValueError:
        d_from = None
    try:
        d_to = datetime.strptime(date_to, "%Y-%m-%d") if date_to else None
    except ValueError:
        d_to = None
    rows = _audit_query(db, user_f, tool_f, status_f, d_from, d_to, dialog)
    total = rows.count()
    pages = max(1, (total + _AUDIT_PAGE_SIZE - 1) // _AUDIT_PAGE_SIZE)
    page = min(page, pages)
    offset = (page - 1) * _AUDIT_PAGE_SIZE
    entries = []
    for log, username in rows.offset(offset).limit(_AUDIT_PAGE_SIZE):
        created = log.created_at
        if created.tzinfo is not None:
            created = created.astimezone().replace(tzinfo=None)
        result = log.result or ""
        entries.append({
            "id": log.id,
            "created_at_local": created.strftime("%d.%m.%Y %H:%M:%S"),
            "username": username,
            "conversation_id": log.conversation_id,
            "tool": log.tool,
            "status": log.status,
            "duration_display": _duration_display(log.duration_ms),
            "result_short": result[:60],
        })
    return templates.TemplateResponse(request, "components/audit/table.html", {
        "request": request,
        "entries": entries,
        "total": total,
        "page": page,
        "pages": pages,
        "filters": {
            "user": user_f or "",
            "tool": tool_f or "",
            "status": status_f or "",
            "date_from": date_from or "",
            "date_to": date_to or "",
            "dialog": dialog or "",
        },
    })


@router.get("/audit/{entry_id}/details")
def audit_details(request: Request,
                  entry_id: int,
                  db: Session = Depends(get_db),
                  user: User = Depends(require_roles_page(Role.admin))):
    row = (
        db.query(AuditLog, User.username)
        .outerjoin(User, AuditLog.user_id == User.id)
        .filter(AuditLog.id == entry_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Запись аудита не найдена")
    log, username = row
    created = log.created_at
    if created.tzinfo is not None:
        created = created.astimezone().replace(tzinfo=None)
    return templates.TemplateResponse(request, "components/audit/details.html", {
        "request": request,
        "e": {
            "id": log.id,
            "created_at_local": created.strftime("%d.%m.%Y %H:%M:%S"),
            "username": username,
            "conversation_id": log.conversation_id,
            "tool": log.tool,
            "status": log.status,
            "duration_display": _duration_display(log.duration_ms),
            "arguments": log.arguments or "",
            "result": log.result or "",
        },
    })


# 401/403 обрабатываются на уровне приложения (main.py): FastAPI не
# позволяет вешать exception_handler на APIRouter. Для /admin/* 401
# превращается в редирект на логин, 403 — в HTML-страницу (не JSON).
