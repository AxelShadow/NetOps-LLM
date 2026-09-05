"""Авторизация Chainlit (Фаза 7).

Два пути:
1. header-auth (nginx, Фаза 10): nginx через auth_request GET
   /internal/auth-check проверяет сессию и пробрасывает заголовки
   X-User-Id/Email/Role/Display-Name + X-Proxy-Auth-Secret. Header-auth
   включается только при совпадении секрета (constant-time).
2. Логин-форма: POST /api/auth/login -> Bearer JWT -> GET /api/auth/me.
   JWT не сохраняется и не логируется — из колбэка выходят только
   числовой user_id и метаданные.
"""
import secrets
from urllib.parse import unquote

import chainlit as cl
import httpx

from config import config

_headers = {"X-Internal-Service-Token": config.internal_service_token}


def _secret_ok(header: str | None) -> bool:
    """Сверка секрета proxy-авторизации; пустой секрет/заголовок -> False."""
    if not config.proxy_auth_secret or not header:
        return False
    return secrets.compare_digest(
        header, config.proxy_auth_secret)


def _to_cl_user(user_id: int, username: str,
                display_name: str | None, role: str) -> cl.User:
    return cl.User(
        identifier=str(user_id),
        display_name=display_name or username,
        metadata={"user_id": user_id, "email": username,
                  "role": role, "display_name": display_name or ""})


@cl.header_auth_callback
async def header_auth(headers: dict) -> cl.User | None:
    """Заголовки от nginx (Фаза 10); принимаются только по секрету."""
    if not _secret_ok(headers.get("X-Proxy-Auth-Secret")):
        return None
    try:
        user_id = int(headers.get("X-User-Id", ""))
        role = headers["X-User-Role"]
    except (ValueError, KeyError):
        return None
    # email и display_name приходят percent-encoded (HTTP-заголовки latin-1)
    username = unquote(headers.get("X-User-Email") or "")
    display_name = unquote(headers.get("X-User-Display-Name") or "")
    if not username:
        return None
    return _to_cl_user(user_id, username, display_name, role)


@cl.password_auth_callback
async def password_auth(username: str, password: str) -> cl.User | None:
    """Логин-форма: /api/auth/login -> JWT -> /api/auth/me (id).

    Пароль нигде не логируется; JWT умирает в этом колбэке.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as http:
            r = await http.post(
                f"{config.fastapi_internal_url}/api/auth/login",
                headers=_headers, json={"username": username,
                                        "password": password})
            if r.status_code != 200:
                return None
            token = r.json()["token"]
            me = await http.get(
                f"{config.fastapi_internal_url}/api/auth/me",
                headers={**_headers,
                         "Authorization": f"Bearer {token}"})
            if me.status_code != 200:
                return None
            data = me.json()
            return _to_cl_user(data["id"], data["username"],
                               data.get("display_name"), data["role"])
    except (httpx.HTTPError, KeyError, ValueError):
        return None
