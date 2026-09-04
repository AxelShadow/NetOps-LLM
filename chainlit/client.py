"""Асинхронный клиент внутреннего API FastAPI (/internal/chat/stream).

НЕ импортирует chainlit — тестируется независимо (sse_parser_test.py).
Сервисный токен живёт только в этом процессе (server-to-server),
в браузер не попадает никогда.

SSE-контракт (backend/app/api/chat.py, run_agent_cycle): кадры
`data: {json}\\n\\n` без имён событий; ключи delta / tool / tool_result /
error; терминатор `data: [DONE]` (не JSON).
"""
import json
from dataclasses import dataclass
from typing import AsyncIterator, Optional

import httpx

from config import config


# ----- дружественные ошибки (без внутренней информации) -----

SERVICE_UNAVAILABLE = ("Сервис временно недоступен. "
                       "Попробуйте позже или обратитесь к администратору.")
UNAUTHORIZED = ("Пользовательская сессия недействительна. "
                "Обновите страницу и войдите заново.")
REQUEST_TOO_LONG = "Запрос слишком объёмный — попробуйте сократить сообщение."
STREAM_INTERRUPTED = ("Ответ оборвался. Сообщение сохранено, "
                      "можно продолжить.")


class BackendError(Exception):
    """Ошибка, ТЕКСТ которой можно показать пользователю."""

    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


# ----- SSE-парсер -----

KNOWN_KINDS = ("delta", "tool", "tool_result", "error")


@dataclass
class SSEEvent:
    """Распарсенный кадр потока."""

    kind: str                # delta | tool | tool_result | error | done | unknown
    data: dict | str         # dict для событий, "" для done


def parse_sse_line(line: str, buf: dict) -> Optional[SSEEvent]:
    """Одна строка SSE -> SSEEvent или None (накопление кадра).

    buf — состояние парсера: {"data": list[str]}.
      - "data: ..." -> накопление (мультистрочные data склеиваются)
      - пустая строка -> flush накопленного кадра
      - прочие поля (event:, id:, retry:) -> игнорируются
      - [DONE] -> kind="done"; известный ключ JSON -> свой kind;
        неизвестный JSON -> kind="unknown" (не падаем)
    """
    line = line.rstrip("\r")  # Windows-сервер может отдавать CRLF
    if not line:
        # конец кадра: собрать накопленные data
        if not buf.get("data"):
            return None
        raw = "\n".join(buf["data"])
        buf["data"] = []
        if raw == "[DONE]":
            return SSEEvent("done", "")
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            return SSEEvent("unknown", {"raw": raw})
        if isinstance(obj, dict):
            for kind in KNOWN_KINDS:
                if kind in obj:
                    return SSEEvent(kind, obj)
        return SSEEvent("unknown", obj if isinstance(obj, dict) else {"raw": raw})
    if line.startswith("data:"):
        payload = line[5:]
        if payload.startswith(" "):
            payload = payload[1:]
        buf.setdefault("data", []).append(payload)
    return None


async def stream_chat(
    content: str,
    conversation_id: Optional[int],
    user_id: int,
    model: Optional[str] = None,
    retry_on_404: bool = True,
) -> AsyncIterator[tuple[Optional[int], SSEEvent]]:
    """POST /internal/chat/stream -> yield (conversation_id, SSEEvent).

    conversation_id приходит из response-заголовка X-Conversation-Id
    и отдаётся один раз, с первым событием (дальше None). Единственный
    источник истины диалогов — БД FastAPI.

    Ошибки -> BackendError с текстом, который можно показать пользователю.
    Retry стрима НЕТ: сообщение уже обработано FastAPI (дубли в БД/аудите).
    404 (диалог удалён) -> единственный автоповтор с conversation_id=None.
    """
    async def _do_stream(conv: Optional[int]) -> AsyncIterator[tuple[Optional[int], SSEEvent]]:
        """Внутренний: yields (conversation_id_или_None, событие).

        При 404 и retry_on_404=True молча возвращается без событий —
        это сигнал наружу сделать один повтор с conversation_id=None.
        """
        payload = {"content": content, "conversation_id": conv, "model": model}
        headers = {"X-Internal-Service-Token": config.internal_service_token,
                   "X-User-Id": str(user_id)}
        # read=None: SSE — бесконечный поток, агент может думать минутами.
        timeout = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=30.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                    "POST",
                    f"{config.fastapi_internal_url}/internal/chat/stream",
                    json=payload, headers=headers) as resp:
                if resp.status_code in (401, 403):
                    raise BackendError(UNAUTHORIZED)
                if resp.status_code == 404 and retry_on_404:
                    return
                if resp.status_code == 413:
                    raise BackendError(REQUEST_TOO_LONG)
                if resp.status_code >= 400:
                    raise BackendError(SERVICE_UNAVAILABLE)
                raw_conv = resp.headers.get("x-conversation-id", "")
                conv_id = int(raw_conv) if raw_conv.isdigit() else None
                buf: dict = {"data": []}
                reported = False
                got_done = False
                async for line in resp.aiter_lines():
                    ev = parse_sse_line(line, buf)
                    if ev is None:
                        continue
                    yield (conv_id if not reported else None), ev
                    reported = True
                    if ev.kind == "done":
                        got_done = True
                        return
                if not got_done:
                    # тело кончилось без [DONE] — обрыв посреди ответа
                    raise BackendError(STREAM_INTERRUPTED)

    got_any = False
    try:
        async for item in _do_stream(conversation_id):
            got_any = True
            yield item
    except BackendError:
        raise
    except httpx.HTTPError:
        raise BackendError(SERVICE_UNAVAILABLE)
    if got_any:
        return

    # 404 (диалог не найден/удалён): один повтор новым диалогом
    try:
        async for item in _do_stream(None):
            yield item
    except BackendError:
        raise
    except httpx.HTTPError:
        raise BackendError(SERVICE_UNAVAILABLE)
