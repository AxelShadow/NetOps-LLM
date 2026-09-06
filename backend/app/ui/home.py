"""Главная страница / — выбор интерфейса (Фаза 14 миграции).

NETOPS_USE_NEW_UI=false (по умолчанию): старый SPA — основной, /
редиректит (303) на /legacy/. true: лендинг со ссылками на чат (/chat),
админку (/admin/) и старый интерфейс (/legacy/).
"""
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from ..config import get_settings
from .router import templates

router = APIRouter()


@router.get("/")
def landing_page(request: Request):
    s = get_settings()
    if not s.use_new_ui:
        # Старый SPA — основной интерфейс
        return RedirectResponse("/legacy/", status_code=303)
    return templates.TemplateResponse(request, "pages/landing.html",
                                      {"request": request,
                                       "is_dev": s.dev_mode})
