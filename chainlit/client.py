"""Асинхронный клиент внутреннего API FastAPI (/internal/chat/stream).

НЕ импортирует chainlit — тестируется независимо (sse_parser_test.py).
Сервисный токен живёт только в этом процессе (server-to-server),
в браузер не попадает никогда.

SSE-контракт (backend/app/api/chat.py, run_agent_cycle): кадры
`data: {json}\\n\\n` без имён событий; ключи delta / tool / tool_result /
error; терминатор `data: [DONE]` (не JSON).
"""
import asyncio
import json
import logging
from dataclasses import dataclass
from typing import AsyncIterator, Optional

import httpx
from tenacity import (AsyncRetrying, retry_if_exception_type,
                       stop_after_attempt, wait_exponential)

from config import config

log = logging.getLogger(__name__)


# ----- дружественные ошибки (без внутренней информации) -----

SERVICE_UNAVAILABLE = ("Сервис временно недоступен. "
                       "Попробуйте позже или обратитесь к администратору.")
UNAUTHORIZED = ("Пользовательская сессия недействительна. "
                "Обновите страницу и войдите заново.")
REQUEST_TOO_LONG = "Запрос слишком объёмный — попробуйте сократить сообщение."
STREAM_INTERRUPTED = ("Ответ оборвался. Сообщение сохранено, "
                      "можно продолжить.")
NETWORK_ERROR = ("Не удалось соединиться с сервером. "
                 "Проверьте подключение или обратитесь к администратору.")
AGENT_TIMEOUT = ("Агент слишком долго не отвечает. "
                 "Попробуйте упростить запрос или разбить его на части.")

# ----- таймауты / retry (Фаза 6, FIX-02) -----

CONNECT_TIMEOUT = 10.0        # установка TCP-соединения
READ_TIMEOUT = 60.0           # между SSE-кадрами: агент может думать
TOTAL_STREAM_TIMEOUT = 300.0   # весь стрим, агентский цикл (FastAPI лимит 20 шагов)

RETRY_ATTEMPTS = 3
RETRY_WAIT_MIN = 2.0          # backoff-пауза между попытками (тесты выставляют 0)
RETRY_WAIT_MAX = 10.0

# 5xx на установке соединения — ретраим как сетевую ошибку (FIX-02)
class _RetryableHTTPStatus(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


# Сетевые ошибки, которые стоит ретраить на ФАЗЕ ПОДКЛЮЧЕНИЯ.
# Обрыв посреди стрима (после первого события) НЕ ретраится:
# сообщение уже обработано FastAPI, повтор даст дубли в БД/аудите.
_RETRYABLE_NETWORK_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.NetworkError,
)


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

        async def _open() -> tuple[httpx.AsyncClient, httpx.Response]:
            """Открыть стрим: retry только на фазе подключения (tenacity).

            Обычная (не генератор) функция — исключения здесь видны
            tenacity. Константы читаются на момент вызова, тесты их
            подменяют. BackendError (401/413/4xx) не ретраится.
            """
            timeout = httpx.Timeout(connect=CONNECT_TIMEOUT,
                                    read=READ_TIMEOUT, write=30.0, pool=30.0)
            client = httpx.AsyncClient(timeout=timeout)
            try:
                req = client.build_request(
                    "POST",
                    f"{config.fastapi_internal_url}/internal/chat/stream",
                    json=payload, headers=headers)
                resp = await client.send(req, stream=True)
            except BaseException:
                await client.aclose()
                raise
            if resp.status_code in (401, 403):
                await resp.aclose()
                await client.aclose()
                raise BackendError(UNAUTHORIZED)
            if resp.status_code == 413:
                await resp.aclose()
                await client.aclose()
                raise BackendError(REQUEST_TOO_LONG)
            if resp.status_code >= 500:
                # FastAPI упал/перезапускается: ретраим (цель FIX-02 —
                # не показывать вечный лоадер при кратных сбоях backend)
                await resp.aclose()
                await client.aclose()
                raise _RetryableHTTPStatus(resp.status_code)
            # 404 не считаем ошибкой: разбор ниже, наружная логика
            # делает автоповтор с conversation_id=None
            if resp.status_code >= 400 and resp.status_code != 404:
                await resp.aclose()
                await client.aclose()
                raise BackendError(SERVICE_UNAVAILABLE)
            return client, resp

        # Retry фазы подключения: 3 попытки, экспоненциальный backoff.
        # AsyncRetrying создаётся на вызов — константы подменяемы тестами.
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(RETRY_ATTEMPTS),
            wait=wait_exponential(multiplier=1, min=RETRY_WAIT_MIN,
                                 max=RETRY_WAIT_MAX),
            retry=retry_if_exception_type(
                _RETRYABLE_NETWORK_ERRORS + (_RetryableHTTPStatus,)),
            reraise=True,
        ):
            with attempt:
                client, resp = await _open()

        try:
            # стрим уже открыт в _open() (client.send); закрываем всё в finally
            if resp.status_code == 404 and retry_on_404:
                # молча: наружу сигнал «нет событий» -> один повтор
                # с conversation_id=None
                return
            raw_conv = resp.headers.get("x-conversation-id", "")
            conv_id = int(raw_conv) if raw_conv.isdigit() else None
            buf: dict = {"data": []}
            reported = False
            got_done = False
            # Общий таймаут на весь стрим: агентский цикл ограничен
            # 20 шагами, дольше 300с ответ не придёт — не ждём вечно.
            try:
                async with asyncio.timeout(TOTAL_STREAM_TIMEOUT):
                    async for line in resp.aiter_lines():
                        ev = parse_sse_line(line, buf)
                        if ev is None:
                            continue
                        yield (conv_id if not reported else None), ev
                        reported = True
                        if ev.kind == "done":
                            got_done = True
                            return
            except asyncio.TimeoutError:
                log.warning("stream timeout after %.0fs (conv=%s)",
                            TOTAL_STREAM_TIMEOUT, conv)
                raise BackendError(AGENT_TIMEOUT)
            if not got_done:
                # тело кончилось без [DONE] — обрыв посреди ответа
                raise BackendError(STREAM_INTERRUPTED)
        finally:
            await resp.aclose()
            await client.aclose()

    got_any = False
    try:
        async for item in _do_stream(conversation_id):
            got_any = True
            yield item
    except BackendError:
        raise
    except _RetryableHTTPStatus:
        # попытки исчерпаны: FastAPI не поднялся за 3 retry
        raise BackendError(SERVICE_UNAVAILABLE)
    except httpx.HTTPError:
        # сетевая ошибка после исчерпания retry (или обрыв после
        # первого события): ретраить стрим нельзя — дубли в БД
        raise BackendError(STREAM_INTERRUPTED if got_any else NETWORK_ERROR)
    if got_any:
        return

    # 404 (диалог не найден/удалён): один повтор новым диалогом
    try:
        async for item in _do_stream(None):
            yield item
    except BackendError:
        raise
    except _RetryableHTTPStatus:
        raise BackendError(SERVICE_UNAVAILABLE)
    except httpx.HTTPError:
        raise BackendError(SERVICE_UNAVAILABLE)
