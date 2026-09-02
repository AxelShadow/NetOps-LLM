"""Внутренний server-to-server API для сервиса Chainlit (Фаза 1).

Доступ только по статическому сервисному токену NETOPS_INTERNAL_SERVICE_TOKEN
(заголовок X-Internal-Service-Token, сравнение constant-time). Пользователь
определяется по заголовку X-User-Id — Chainlit сам аутентифицирует его
через основной /api/auth/login и передаёт сюда только id.

При пустом NETOPS_INTERNAL_SERVICE_TOKEN все /internal/*-маршруты выключены.
"""
import secrets
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import User, Conversation
from ..config import get_settings
from .chat import run_agent_cycle

router = APIRouter(prefix="/internal")


class InternalChatIn(BaseModel):
    content: str
    conversation_id: int | None = None
    model: str | None = None


def _check_service_token(x_internal_service_token: str | None = Header(default=None)):
    """401 если маршрут выключен или токен не совпал."""
    expected = get_settings().internal_service_token
    if not expected or not x_internal_service_token or \
            not secrets.compare_digest(expected, x_internal_service_token):
        raise HTTPException(401, "Недействительный сервисный токен")


def _load_user_by_id(x_user_id: str | None = Header(default=None),
                     db: Session = Depends(get_db)) -> User:
    """Пользователь из БД по X-User-Id; проверка активна."""
    if x_user_id is None or not x_user_id.isdigit():
        raise HTTPException(401, "Не передан X-User-Id")
    user = db.get(User, int(x_user_id))
    if not user or not user.is_active:
        raise HTTPException(403, "Пользователь не найден или отключён")
    return user


@router.post("/chat/stream",
             dependencies=[Depends(_check_service_token)])
async def internal_chat_stream(
        data: InternalChatIn,
        user: User = Depends(_load_user_by_id),
        db: Session = Depends(get_db)):
    if not (data.content or "").strip():
        raise HTTPException(400, "Пустое сообщение")

    if data.conversation_id:
        conv = db.get(Conversation, data.conversation_id)
        if not conv or conv.user_id != user.id:
            raise HTTPException(404, "Диалог не найден")
    else:
        conv = Conversation(user_id=user.id,
                           title=data.content[:60])
        db.add(conv)
        db.commit()
        db.refresh(conv)
    # ВАЖНО: db (request-scoped) закрывается после return; генератор
    # использует собственные сессии SessionLocal внутри.
    return StreamingResponse(
        run_agent_cycle(user, conv, data.content, data.model),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache",
                 "X-Accel-Buffering": "no"})
