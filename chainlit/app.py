"""NetOps-LLM чат: Chainlit UI -> FastAPI /internal/chat/stream.

Фаза 7: авторизация (auth.py — header-auth по секрету прокси или
логин-форма). Диалоги хранятся ТОЛЬКО в БД FastAPI: conversation_id
берём из заголовка X-Conversation-Id и держим в cl.user_session.
"""
import chainlit as cl

import adapters
import auth  # noqa: F401 — регистрирует колбэки login/header-auth
from client import BackendError, stream_chat


@cl.on_chat_start
async def on_chat_start() -> None:
    """Новая сессия чата: диалог ещё не создан."""
    cl.user_session.set("conversation_id", None)
    user = cl.user_session.get("user")
    if user is None:
        # Теоретически недостижимо (chainlit не пускает анонимов),
        # но без user_id чат работать не может.
        raise ValueError("Сессия без авторизации")
    cl.user_session.set("user_id", user.metadata["user_id"])
    await cl.Message(content="Задайте вопрос по инфраструктуре.").send()


@cl.on_message
async def on_message(message: cl.Message) -> None:
    """Сообщение пользователя -> стрим ответа + шаги инструментов."""
    user_id = cl.user_session.get("user_id")
    conv_id = cl.user_session.get("conversation_id")
    answer = cl.Message(content="")
    await answer.send()  # пустое сообщение-контейнер, поток пойдёт в него
    # FIX-04: индикатор работы агента на время tool-цикла
    thinking = cl.Step(name="Агент думает")
    await thinking.send()
    step_counter = 0
    got_delta = False
    try:
        async for new_conv, ev in stream_chat(
                content=message.content, conversation_id=conv_id,
                user_id=user_id):
            if new_conv is not None:
                cl.user_session.set("conversation_id", new_conv)
            if ev.kind == "delta":
                if not got_delta:
                    # первый токен — стадия «думает» закончилась
                    thinking.output = "Ответ формируется..."
                    await thinking.update()
                    got_delta = True
                await answer.stream_token(ev.data["delta"])
            elif ev.kind == "tool":
                # новый вызов инструмента — показать текущий шаг
                step_counter += 1
                thinking.output = f"Шаг {step_counter}: вызов {ev.data['tool']}"
                await thinking.update()
                await adapters.render_event(ev)
            elif ev.kind == "tool_result":
                await adapters.render_event(ev)
            elif ev.kind == "error":
                # дружелюбный текст от бэкенда — в общий поток
                await answer.stream_token("\n\n" + ev.data["error"])
                got_delta = True
            elif ev.kind == "done":
                break
        # финальный статус индикатора
        thinking.output = (f"Выполнено {step_counter} шаг(ов)"
                           if step_counter else "Ответ готов")
        await thinking.update()
        if not got_delta:
            # только шаги, без текста — закрыть пустое сообщение
            await answer.update()
    except BackendError as e:
        # дружественный текст, без стека/URL/деталей
        thinking.output = "Ошибка"
        await thinking.update()
        await answer.update(content=e.user_message)
