"""Серверная админка на FastAPI + Jinja2 + HTMX (Фаза 2 миграции).

Маршруты /admin/*: логин/логаут (JWT в HttpOnly-cookie netops_token),
каркас разделов (dashboard/инвентарь/диалоги/аудит/настройки) с серверным
RBAC. Контент разделов наполняется в Фазах 3-5; старый SPA
(frontend/index.html) продолжает работать через /api/* без изменений.
"""
import asyncio
import logging
from datetime import datetime, time as dtime
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import SessionLocal, get_db
from ..models import AuditLog, Conversation, Device, DeviceType, Message, \
    Role, User
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
                  db: Session = Depends(get_db),
                  user: User = Depends(require_roles_page(Role.admin))):
    s = get_settings()
    vmware_devices = db.query(Device).filter(
        Device.type.in_([DeviceType.vcenter, DeviceType.esxi]),
        Device.enabled.is_(True)
    ).order_by(Device.name).all()
    ctx = {
        "settings": _settings_view(s),
        "vmware_devices": vmware_devices,
    }
    return _render(request, user, "pages/settings.html", ctx)


def _app_version() -> str:
    """Версия приложения: короткий хеш HEAD git (кэшируется).

    Отдельной константы версии в проекте нет; git-хеш — самый честный
    идентификатор сборки. Кэш, т.к. вызывается при каждом открытии страницы.
    """
    cached = getattr(_app_version, "_value", None)
    if cached is not None:
        return cached
    import subprocess
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=3
        )
        value = out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else "dev"
    except Exception:
        value = "dev"
    setattr(_app_version, "_value", value)
    return value


def _settings_view(s) -> dict:
    """Параметры конфигурации для страницы настроек, без секретов.

    Значения берутся из переменных окружения (app/config.py) и меняются
    только перезапуском с новым окружением — потому страница read-only.
    Секреты (jwt_secret, llm_api_key, zabbix_token, пароли) не показываем.
    """
    from ..api.chat import MAX_AGENT_STEPS
    return {
        "general": [
            ("Версия приложения", _app_version()),
            ("Режим разработки (DEV_MODE)", "вкл" if s.dev_mode else "выкл"),
            ("Мок-режим", "вкл" if s.mock_mode else "выкл"),
            ("Авто-регистрация пользователей", "вкл" if s.auto_register_users else "выкл"),
            ("Сообщений истории в контексте", s.history_messages),
            ("Лимит шагов агента", MAX_AGENT_STEPS),
        ],
        "llm": [
            ("URL", s.llm_base_url),
            ("Модель по умолчанию", s.llm_default_model or "первая доступная"),
            ("Таймаут, с", s.llm_timeout),
            ("API-ключ", "задан" if s.llm_api_key else "не задан"),
        ],
        "zabbix": [
            ("URL", s.zabbix_url or "не настроен"),
            ("Токен", "задан" if s.zabbix_token else "не задан"),
        ],
        "ldap": [
            ("Сервер AD", s.ad_server),
            ("Домен", s.ad_domain),
            ("Base DN поиска", s.ad_search_base),
        ],
        "llm_url": s.llm_base_url,
        "zabbix_url": s.zabbix_url or "не настроен",
    }


# --- Настройки: HTMX-проверки подключений (Фаза 5 миграции) ------------------

_CHECK_TIMEOUT = 5.0  # секунд на каждую проверку


def _check_tpl(request: Request, user: User, ok: bool, title: str, detail: str):
    return templates.TemplateResponse(
        request, "components/settings/check.html",
        _ctx(request, user, ok=ok, title=title, detail=detail))


@router.get("/settings/partial/check/llm")
async def settings_check_llm(request: Request,
                             user: User = Depends(require_roles_page(Role.admin))):
    s = get_settings()
    if s.mock_mode:
        return _check_tpl(request, user, True, "Мок-режим",
                          "Сетевые вызовы LLM отключены, проверка не требуется")
    try:
        from ..llm.client import llm
        models = await asyncio.wait_for(llm.list_models(),
                                        timeout=_CHECK_TIMEOUT)
        return _check_tpl(request, user, True, "Подключено",
                          "Модели: " + (", ".join(models[:5]) if models
                                        else "ни одной не загружено"))
    except Exception as e:
        return _check_tpl(request, user, False, "Недоступно",
                          "%s: %s" % (type(e).__name__, str(e)[:200]))


@router.get("/settings/partial/check/zabbix")
async def settings_check_zabbix(request: Request,
                                user: User = Depends(require_roles_page(Role.admin))):
    s = get_settings()
    if not s.zabbix_url or not s.zabbix_token:
        return _check_tpl(request, user, False, "Не настроен",
                          "Задайте NETOPS_ZABBIX_URL и NETOPS_ZABBIX_TOKEN")
    try:
        async with httpx.AsyncClient(timeout=_CHECK_TIMEOUT) as client:
            resp = await client.post(
                s.zabbix_url,
                json={"jsonrpc": "2.0", "method": "apiinfo.version",
                      "params": {}, "id": 1})
            resp.raise_for_status()
            data = resp.json()
        version = (data.get("result") or "?") if isinstance(data, dict) else "?"
        return _check_tpl(request, user, True, "Подключено",
                          "Версия API Zabbix: " + str(version))
    except Exception as e:
        return _check_tpl(request, user, False, "Недоступно",
                          "%s: %s" % (type(e).__name__, str(e)[:200]))


@router.get("/settings/partial/check/vmware")
async def settings_check_vmware(request: Request,
                                db: Session = Depends(get_db),
                                user: User = Depends(require_roles_page(Role.admin))):
    device = db.query(Device).filter(
        Device.type.in_([DeviceType.vcenter, DeviceType.esxi]),
        Device.enabled.is_(True)
    ).order_by(Device.name).first()
    if not device:
        return _check_tpl(request, user, False, "Нет устройств",
                          "В инвентаре нет включённых VMware-устройств")
    try:
        from ..devices.vmware import get_adapter
        def _ping():
            get_adapter(device).get_hosts()
        # to_thread сам не прерывает блокирующий pyvmomi, поэтому wait_for:
        # поток останется дорабатывать, но запрос вернётся через таймаут
        await asyncio.wait_for(asyncio.to_thread(_ping),
                               timeout=_CHECK_TIMEOUT)
        return _check_tpl(request, user, True, "Подключено",
                          "%s: список хостов получен" % device.name)
    except asyncio.TimeoutError:
        return _check_tpl(request, user, False, "Недоступно",
                          "%s: нет ответа за %.0f с" % (device.name,
                                                        _CHECK_TIMEOUT))
    except Exception as e:
        return _check_tpl(request, user, False, "Недоступно",
                          "%s: %s: %s" % (device.name, type(e).__name__,
                                          str(e)[:200]))


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


# --- Диалоги: HTMX-фрагменты (Фаза 9 миграции) ------------------------------

_CONV_PAGE_SIZE = 25


def _conv_query(db: Session, user_f: str | None, q_f: str | None,
                date_from: datetime | None, date_to: datetime | None):
    """Список диалогов с агрегатами (join User + count/max по Message).

    «Дата обновления» — время последнего сообщения (MAX(messages.created_at),
    для пустого диалога — created_at): отдельного столбца в схеме нет,
    источник истины — текущая БД, без миграций.
    """
    last_msg = sa_func.coalesce(sa_func.max(Message.created_at),
                                Conversation.created_at)
    q = (
        db.query(
            Conversation,
            User.username,
            sa_func.count(Message.id).label("msg_count"),
            last_msg.label("updated_at"),
        )
        .outerjoin(User, Conversation.user_id == User.id)
        .outerjoin(Message, Message.conversation_id == Conversation.id)
        # User.username в GROUP BY обязателен для Postgres (only_full_group_by)
        .group_by(Conversation.id, User.username)
        .order_by(last_msg.desc(), Conversation.id.desc())
    )
    if user_f:
        q = q.filter(User.username.ilike(f"%{user_f}%"))
    if q_f:
        q = q.filter(Conversation.title.ilike(f"%{q_f}%"))
    if date_from:
        q = q.filter(Conversation.created_at >= date_from)
    if date_to:
        # date_to — inclusive: включаем весь указанный день
        q = q.filter(Conversation.created_at <
                      datetime.combine(date_to, dtime(hour=23, minute=59,
                                                       second=59)))
    return q


@router.get("/conversations/partial/table")
def conversations_table(request: Request,
                        db: Session = Depends(get_db),
                        user: User = Depends(require_roles_page(
                            Role.admin, Role.engineer)),
                        page: int = Query(default=1, ge=1),
                        user_f: str | None = Query(default=None, alias="user"),
                        q_f: str | None = Query(default=None, alias="q"),
                        date_from: str | None = Query(default=None),
                        date_to: str | None = Query(default=None)):
    # input[type=date] шлёт YYYY-MM-DD; битые значения игнорируем
    try:
        d_from = datetime.strptime(date_from, "%Y-%m-%d") if date_from else None
    except ValueError:
        d_from = None
    try:
        d_to = datetime.strptime(date_to, "%Y-%m-%d") if date_to else None
    except ValueError:
        d_to = None
    rows = _conv_query(db, user_f, q_f, d_from, d_to)
    total = rows.count()
    pages = max(1, (total + _CONV_PAGE_SIZE - 1) // _CONV_PAGE_SIZE)
    page = min(page, pages)
    offset = (page - 1) * _CONV_PAGE_SIZE
    entries = []
    for conv, username, msg_count, updated in \
            rows.offset(offset).limit(_CONV_PAGE_SIZE):
        created = conv.created_at
        if created.tzinfo is not None:
            created = created.astimezone().replace(tzinfo=None)
        if updated.tzinfo is not None:
            updated = updated.astimezone().replace(tzinfo=None)
        entries.append({
            "id": conv.id,
            "username": username,
            "title": conv.title,
            "created_at_local": created.strftime("%d.%m.%Y %H:%M"),
            "updated_at_local": updated.strftime("%d.%m.%Y %H:%M"),
            "msg_count": msg_count,
        })
    return templates.TemplateResponse(
        request, "components/conversations/table.html", {
            "request": request,
            "entries": entries,
            "total": total,
            "page": page,
            "pages": pages,
            "filters": {
                "user": user_f or "",
                "q": q_f or "",
                "date_from": date_from or "",
                "date_to": date_to or "",
            },
        })


@router.get("/conversations/{conv_id}/details")
def conversation_details(request: Request,
                         conv_id: int,
                         db: Session = Depends(get_db),
                         user: User = Depends(require_roles_page(
                             Role.admin, Role.engineer))):
    row = (
        db.query(Conversation, User.username)
        .outerjoin(User, Conversation.user_id == User.id)
        .filter(Conversation.id == conv_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Диалог не найден")
    conv, username = row
    msgs = (db.query(Message)
            .filter(Message.conversation_id == conv_id)
            .order_by(Message.id.asc())
            .all())
    created = conv.created_at
    if created.tzinfo is not None:
        created = created.astimezone().replace(tzinfo=None)
    updated = max((m.created_at for m in msgs), default=conv.created_at)
    if updated.tzinfo is not None:
        updated = updated.astimezone().replace(tzinfo=None)
    messages = []
    for m in msgs:
        m_created = m.created_at
        if m_created.tzinfo is not None:
            m_created = m_created.astimezone().replace(tzinfo=None)
        messages.append({
            "role": m.role,
            "name": m.name or "",
            "tool_calls": m.tool_calls or "",
            "content": m.content,
            "created_at_local": m_created.strftime("%d.%m.%Y %H:%M:%S"),
        })
    return templates.TemplateResponse(
        request, "components/conversations/details.html", {
            "request": request,
            "e": {
                "id": conv.id,
                "username": username,
                "title": conv.title,
                "created_at_local": created.strftime("%d.%m.%Y %H:%M:%S"),
                "updated_at_local": updated.strftime("%d.%m.%Y %H:%M:%S"),
                "msg_count": len(msgs),
            },
            "messages": messages,
        })


# 401/403 обрабатываются на уровне приложения (main.py): FastAPI не
# позволяет вешать exception_handler на APIRouter. Для /admin/* 401
# превращается в редирект на логин, 403 — в HTML-страницу (не JSON).


# --- Инвентарь: HTMX-фрагменты и CRUD (Фаза 3 миграции) ---------------------

_INV_PAGE_SIZE = 20

# Показ устройств в UI: пароль никогда не уходит в HTML.
_INV_DEVICE_TYPES = [t.value for t in DeviceType]


def _inv_query(db: Session, q_f: str | None, type_f: str | None,
               source_f: str | None, status_f: str | None, group_f: str | None):
    """Базовый запрос устройств с фильтрами, сортировка по имени."""
    query = db.query(Device).order_by(Device.name)
    if q_f:
        query = query.filter(Device.name.ilike(f"%{q_f}%"))
    if type_f:
        query = query.filter(Device.type == type_f)
    if source_f:
        query = query.filter(Device.source == source_f)
    if status_f:
        # "on" = только включённые, "off" = только выключенные
        query = query.filter(Device.enabled == (status_f == "on"))
    if group_f:
        query = query.filter(Device.group.ilike(f"%{group_f}%"))
    return query


def _inv_rows(request: Request, user: User, db: Session, flash: str | None = None,
              **filters):
    """Страница таблицы инвентаря: query -> данные -> шаблон components/inventory/table.html."""
    page = filters.pop("page", 1)
    rows = _inv_query(db, **filters)
    total = rows.count()
    pages = max(1, (total + _INV_PAGE_SIZE - 1) // _INV_PAGE_SIZE)
    page = min(max(1, page), pages)
    offset = (page - 1) * _INV_PAGE_SIZE
    devices = rows.offset(offset).limit(_INV_PAGE_SIZE).all()
    # Текущие фильтры без page — для ссылок пагинации (как в audit)
    keep = {k: v for k, v in request.query_params.items()
            if k != "page" and v}
    return templates.TemplateResponse(
        request, "components/inventory/table.html",
        _ctx(request, user,
             devices=devices, total=total, page=page, pages=pages,
             filters=keep, device_types=_INV_DEVICE_TYPES, flash=flash))


def _inv_form(request: Request, user: User, device: Device | None,
              error: str | None = None, status_code: int = 200):
    """Фрагмент формы добавления/редактирования (модалка)."""
    action = "/admin/inventory" if device is None \
        else f"/admin/inventory/{device.id}"
    method = "post" if device is None else "put"
    return templates.TemplateResponse(
        request, "components/inventory/form.html",
        _ctx(request, user, device=device, action=action, method=method,
             error=error, device_types=_INV_DEVICE_TYPES),
        status_code=status_code)


@router.get("/inventory/partial/table")
def inventory_table(request: Request,
                    db: Session = Depends(get_db),
                    user: User = Depends(require_roles_page(
                        Role.admin, Role.engineer)),
                    page: int = Query(default=1, ge=1),
                    q_f: str | None = Query(default=None, alias="q"),
                    type_f: str | None = Query(default=None, alias="type"),
                    source_f: str | None = Query(default=None, alias="source"),
                    status_f: str | None = Query(default=None, alias="status"),
                    group_f: str | None = Query(default=None, alias="group")):
    return _inv_rows(request, user, db, page=page, q_f=q_f, type_f=type_f,
                     source_f=source_f, status_f=status_f, group_f=group_f)


@router.get("/inventory/new")
def inventory_new(request: Request,
                  user: User = Depends(require_roles_page(Role.admin))):
    return _inv_form(request, user, device=None)


@router.get("/inventory/{device_id}/edit")
def inventory_edit(device_id: int, request: Request,
                   db: Session = Depends(get_db),
                   user: User = Depends(require_roles_page(Role.admin))):
    d = db.get(Device, device_id)
    if not d:
        raise HTTPException(404, "Устройство не найдено")
    return _inv_form(request, user, device=d)


def _inv_upsert(db: Session, data: dict, existing: Device | None) -> Device:
    """Общая для create/update логика сохранения. Кидает HTTPException при ошибках."""
    name = (data.get("name") or "").strip().lower()
    if not name:
        raise HTTPException(400, "Имя устройства обязательно")
    busy = db.query(Device).filter(
        Device.name == name,
        Device.id != (existing.id if existing else -1)).first()
    if busy:
        raise HTTPException(400, f"Имя «{name}» уже занято")
    try:
        dtype = DeviceType(data.get("type", ""))
    except ValueError:
        raise HTTPException(400, "Неверный тип устройства")
    host = (data.get("host") or "").strip()
    if not host:
        raise HTTPException(400, "Адрес (host) обязателен")
    d = existing or Device()
    d.name = name
    d.type = dtype
    d.host = host
    try:
        d.port = int(data.get("port") or 0)
    except ValueError:
        raise HTTPException(400, "Порт должен быть числом")
    d.username = (data.get("username") or "").strip()
    if data.get("password"):            # пустой пароль = оставить прежний
        d.password = data["password"]
    d.enabled = data.get("enabled") == "on"
    d.description = (data.get("description") or "").strip()
    if existing is None or existing.source == "manual":
        d.group = (data.get("group") or "").strip()
    if existing is None:
        db.add(d)
    db.commit()
    return d


def _invalidate_vmware_cache():
    """Безопасная инвалидация кэша сессий VMware. Падение не ломает CRUD."""
    try:
        from ..devices.vmware import clear_cache
        clear_cache()
        log.info("VMware cache invalidated after device change")
    except ImportError:
        log.debug("vmware module not available, skip cache clear")
    except Exception as e:
        log.warning("Failed to clear VMware cache: %s", e)


@router.post("/inventory")
async def inventory_create(request: Request,
                      db: Session = Depends(get_db),
                      user: User = Depends(require_roles_page(Role.admin))):
    data = dict(await request.form())
    try:
        _inv_upsert(db, data, existing=None)
    except HTTPException as e:
        # форма вернётся с текстом ошибки поверх модалки
        return _inv_form(request, user, device=None,
                        error=e.detail, status_code=e.status_code)
    # FIX-03: новый vCenter не должен вечно жить со stale-кэшем,
    # оставшимся от прежних подключений
    _invalidate_vmware_cache()
    return _inv_rows(request, user, db, page=1, q_f=None, type_f=None,
                     source_f=None, status_f=None, group_f=None,
                     flash="Устройство добавлено")


@router.post("/inventory/sync-zabbix")
def inventory_sync_zabbix(request: Request,
                          db: Session = Depends(get_db),
                          user: User = Depends(require_roles_page(Role.admin))):
    # Роль уже проверена (admin); вызываем ту же логику, что /api/devices/sync-zabbix
    from ..api.devices import sync_zabbix as api_sync  # локальный импорт: без циклов
    s = get_settings()
    if not s.zabbix_url or not s.zabbix_token:
        return _inv_rows(request, user, db, page=1, q_f=None, type_f=None,
                         source_f=None, status_f=None, group_f=None,
                         flash="Zabbix не настроен: задайте NETOPS_ZABBIX_URL "
                               "и NETOPS_ZABBIX_TOKEN")
    try:
        result = api_sync(db=db, _admin=user)
    except HTTPException as e:
        return _inv_rows(request, user, db, page=1, q_f=None, type_f=None,
                         source_f=None, status_f=None, group_f=None,
                         flash=f"Ошибка синхронизации: {e.detail}")
    message = ("Zabbix: добавлено %d, обновлено %d, отключено %d"
              % (result["added"], result["updated"], result["disabled_gone"]))
    return _inv_rows(request, user, db, page=1, q_f=None, type_f=None,
                     source_f=None, status_f=None, group_f=None, flash=message)


@router.put("/inventory/{device_id}")
async def inventory_update(device_id: int, request: Request,
                     db: Session = Depends(get_db),
                     user: User = Depends(require_roles_page(Role.admin))):
    d = db.get(Device, device_id)
    if not d:
        raise HTTPException(404, "Устройство не найдено")
    data = dict(await request.form())
    if d.source == "zabbix" and data.get("name") and data["name"] != d.name:
        # Zabbix-устройствам можно менять только enabled (как в /api/devices)
        return _inv_form(request, user, device=d,
                         error="Устройства из Zabbix: можно менять только "
                               "включение/выключение", status_code=400)
    if d.source == "zabbix" and not data.get("name"):
        # Быстрый переключатель из таблицы: только enabled
        d.enabled = data.get("enabled") == "on"
        db.commit()
        return _inv_rows(request, user, db, page=1, q_f=None, type_f=None,
                         source_f=None, status_f=None, group_f=None,
                         flash=f"Устройство обновлено: {d.name}")
    try:
        _inv_upsert(db, data, existing=d)
    except HTTPException as e:
        return _inv_form(request, user, device=d,
                         error=e.detail, status_code=e.status_code)
    # FIX-03: изменились креды/адрес vCenter — прежняя сессия недействительна
    _invalidate_vmware_cache()
    return _inv_rows(request, user, db, page=1, q_f=None, type_f=None,
                     source_f=None, status_f=None, group_f=None,
                     flash="Устройство обновлено")


@router.delete("/inventory/{device_id}")
def inventory_delete(device_id: int, request: Request,
                     db: Session = Depends(get_db),
                     user: User = Depends(require_roles_page(Role.admin))):
    d = db.get(Device, device_id)
    if not d:
        raise HTTPException(404, "Устройство не найдено")
    if d.source == "zabbix":
        return _inv_rows(request, user, db, page=1, q_f=None, type_f=None,
                         source_f=None, status_f=None, group_f=None,
                         flash="Устройства из Zabbix удаляются только "
                               "синхронизацией")
    name = d.name
    db.delete(d)
    db.commit()
    # FIX-03: единая точка сброса VMware-сессий (было inline в delete)
    _invalidate_vmware_cache()
    return _inv_rows(request, user, db, page=1, q_f=None, type_f=None,
                     source_f=None, status_f=None, group_f=None,
                     flash=f"Устройство удалено: {name}")
