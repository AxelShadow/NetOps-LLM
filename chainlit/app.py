"""NetOps-LLM чат: Chainlit UI -> FastAPI /internal/chat/stream.

Фаза 6: dev-заглушка user_id (CHAINLIT_DEV_USER_ID); авторизация — Фаза 7.
Диалоги хранятся ТОЛЬКО в БД FastAPI: conversation_id берём из заголовка
X-Conversation-Id и держим в cl.user_session.
"""
import chainlit as cl

import adapters
from client import BackendError, stream_chat
from config import config


@cl.on_chat_start
async def on_chat_start() -> None:
    """Новая сессия чата: диалог ещё не создан."""
    cl.user_session.set("conversation_id", None)
    # Фаза 6 (только ENVIRONMENT=development): заглушка user_id.
    # Фаза 7 заменит на user из cl.user_session.
    cl.user_session.set("user_id", config.dev_user_id)
    await cl.Message(content="Задайте вопрос по инфраструктуре.").send()


@cl.on_message
async def on_message(message: cl.Message) -> None:
    """Сообщение пользователя -> стрим ответа + шаги инструментов."""
    user_id = cl.user_session.get("user_id")
    conv_id = cl.user_session.get("conversation_id")
    answer = cl.Message(content="")
    await answer.send()  # пустое сообщение-контейнер, поток пойдёт в него
    got_delta = False
    try:
        async for new_conv, ev in stream_chat(
                content=message.content, conversation_id=conv_id,
                user_id=user_id):
            if new_conv is not None:
                cl.user_session.set("conversation_id", new_conv)
            if ev.kind == "delta":
                await answer.stream_token(ev.data["delta"])
                got_delta = True
            elif ev.kind in ("tool", "tool_result"):
                await adapters.render_event(ev)
            elif ev.kind == "error":
                # дружелюбный текст от бэкенда — в общий поток
                await answer.stream_token("\n\n" + ev.data["error"])
                got_delta = True
            elif ev.kind == "done":
                break
        if not got_delta:
            # только шаги, без текста — закрыть пустое сообщение
            await answer.update()
    except BackendError as e:
        # дружественный текст, без стека/URL/деталей
        await answer.update(content=e.user_message)
