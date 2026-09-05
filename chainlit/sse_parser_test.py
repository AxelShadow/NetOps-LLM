"""Тест SSE-парсера и stream_chat: python sse_parser_test.py (venv chainlit).

НЕ импортирует chainlit: проверяет parse_sse_line и stream_chat из client.py
(для stream_chat — через httpx.MockTransport, без сети и без FastAPI).
Конвенция проекта: глобальные PASS/FAIL, check(), sys.exit(0/1).
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# client.py импортирует config -> обязательные env задаём ДО импорта
os.environ.setdefault("FASTAPI_INTERNAL_URL", "http://mock-backend:8000")
os.environ.setdefault("NETOPS_INTERNAL_SERVICE_TOKEN", "test-token")

import httpx  # noqa: E402

import client  # noqa: E402
from client import SSEEvent, parse_sse_line, BackendError  # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, ok: bool, detail: str = ""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"PASS: {name}")
    else:
        FAIL += 1
        print(f"FAIL: {name} {detail}")


def ev(*lines: str) -> SSEEvent | None:
    """Прогнать строки через парсер; вернуть последний SSEEvent (или None)."""
    result = None
    buf: dict = {"data": []}
    for line in lines:
        got = parse_sse_line(line, buf)
        if got is not None:
            result = got
    return result


def test_parser():
    # 1. delta
    e = ev('data: {"delta": "Привет"}', "")
    check("1: delta", e is not None and e.kind == "delta"
          and e.data["delta"] == "Привет", f"got {e}")

    # 2. мультистрочный data (кадр из двух data:-строк)
    e = ev('data: {"tool_result": {"name": "ping",',
          'data:  "ok": true, "preview": "pong"}}', "")
    check("2: мультистрочный data", e is not None and e.kind == "tool_result"
          and e.data["tool_result"]["ok"] is True, f"got {e}")

    # 3. tool
    e = ev('data: {"tool": "ping", "args": {"host": "mock-host"}}', "")
    check("3: tool", e is not None and e.kind == "tool"
          and e.data["tool"] == "ping" and e.data["args"]["host"] == "mock-host",
          f"got {e}")

    # 4. tool_result ok=false
    e = ev('data: {"tool_result": {"name": "ping", "ok": false, "preview": "timeout"}}', "")
    check("4: tool_result ok=false", e is not None and e.kind == "tool_result"
          and e.data["tool_result"]["ok"] is False, f"got {e}")

    # 5. error
    e = ev('data: {"error": "Сервер LLM недоступен, попробуйте позже"}', "")
    check("5: error", e is not None and e.kind == "error"
          and "недоступен" in e.data["error"], f"got {e}")

    # 6. [DONE] — не JSON
    e = ev("data: [DONE]", "")
    check("6: [DONE]", e is not None and e.kind == "done", f"got {e}")

    # 7. мусорный JSON -> unknown, не падает
    e = ev("data: {не json}", "")
    check("7: мусор -> unknown", e is not None and e.kind == "unknown", f"got {e}")

    # 8. служебные поля игнорируются
    e = ev("event: message", "id: 7", "retry: 1000")
    check("8: event/id/retry -> None", e is None, f"got {e}")

    # 9. пустая строка без накопленных data -> None
    buf: dict = {"data": []}
    check("9: пустой flush -> None", parse_sse_line("", buf) is None)

    # 10. CRLF (Windows)
    e = ev('data: {"delta": "ок"}\r', "\r")
    check("10: CRLF", e is not None and e.kind == "delta"
          and e.data["delta"] == "ок", f"got {e}")

    # 11. кириллица
    e = ev('data: {"delta": "кириллица ✓"}', "")
    check("11: кириллица", e is not None and e.data["delta"] == "кириллица ✓",
          f"got {e}")

    # 12. неизвестный JSON-ключ -> unknown
    e = ev('data: {"foo": "bar"}', "")
    check("12: неизвестный ключ -> unknown", e is not None and e.kind == "unknown",
          f"got {e}")


SSE_BODY = (
    'data: {"delta": "Смотрю."}\n\n'
    'data: {"tool": "ping", "args": {"host": "mock-host"}}\n\n'
    'data: {"tool_result": {"name": "ping", "ok": true, "preview": "pong"}}\n\n'
    'data: {"delta": "Готово."}\n\n'
    "data: [DONE]\n\n"
)


def test_stream_mock():
    """stream_chat через MockTransport: заголовок X-Conversation-Id,
    порядок событий, done; + 401; + 404-повтор; + обрыв без [DONE]."""
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append({"url": request.url.path})
        if len(calls) == 1:
            return httpx.Response(200, headers={
                "Content-Type": "text/event-stream",
                "X-Conversation-Id": "42"},
                content=SSE_BODY.encode("utf-8"))
        return httpx.Response(200, headers={
            "Content-Type": "text/event-stream",
            "X-Conversation-Id": "43"},
            content=SSE_BODY.encode("utf-8"))

    async def run():
        orig = httpx.AsyncClient
        transport = httpx.MockTransport(handler)

        class PatchedClient(orig):
            def __init__(self, *a, **kw):
                kw["transport"] = transport
                super().__init__(*a, **kw)

        httpx.AsyncClient = PatchedClient
        try:
            got = []
            async for conv, e in client.stream_chat("пинг", None, 1):
                got.append((conv, e))
            return got
        finally:
            httpx.AsyncClient = orig

    got = asyncio.run(run())
    kinds = [e.kind for _, e in got]
    convs = [c for c, _ in got]
    check("m1: поток полный", kinds == ["delta", "tool", "tool_result", "delta", "done"],
          f"kinds={kinds}")
    check("m2: conv_id=42 только с первым событием",
          convs == [42, None, None, None, None], f"convs={convs}")

    # 401 -> BackendError(UNAUTHORIZED)
    def h401(request):
        return httpx.Response(401, json={"detail": "x"})

    async def run401():
        orig = httpx.AsyncClient
        transport = httpx.MockTransport(h401)

        class PatchedClient(orig):
            def __init__(self, *a, **kw):
                kw["transport"] = transport
                super().__init__(*a, **kw)

        httpx.AsyncClient = PatchedClient
        try:
            async for _ in client.stream_chat("x", None, 1):
                pass
        except BackendError as e:
            return e.user_message
        finally:
            httpx.AsyncClient = orig
        return None

    msg = asyncio.run(run401())
    check("m3: 401 -> UNAUTHORIZED", msg == client.UNAUTHORIZED, f"got {msg!r}")

    # 404 -> автоповтор новым диалогом (2 запроса, оба кончились done)
    calls.clear()
    state = {"n": 0}

    def h404(request):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(404, json={"detail": "not found"})
        return httpx.Response(200, headers={
            "Content-Type": "text/event-stream",
            "X-Conversation-Id": "7"},
            content=SSE_BODY.encode("utf-8"))

    async def run404():
        orig = httpx.AsyncClient
        transport = httpx.MockTransport(h404)

        class PatchedClient(orig):
            def __init__(self, *a, **kw):
                kw["transport"] = transport
                super().__init__(*a, **kw)

        httpx.AsyncClient = PatchedClient
        try:
            got = []
            async for conv, e in client.stream_chat("x", 999, 1):
                got.append((conv, e))
            return got
        finally:
            httpx.AsyncClient = orig

    got = asyncio.run(run404())
    kinds = [e.kind for _, e in got]
    check("m4: 404 -> повтор, conv=7, done", kinds == ["delta", "tool", "tool_result", "delta", "done"]
          and got[0][0] == 7, f"kinds={kinds} conv0={got[0][0] if got else None}")

    # обрыв: тело без [DONE] -> STREAM_INTERRUPTED
    def h_chop(request):
        body = SSE_BODY.replace("data: [DONE]\n\n", "")
        return httpx.Response(200, headers={
            "Content-Type": "text/event-stream",
            "X-Conversation-Id": "5"},
            content=body.encode("utf-8"))

    async def run_chop():
        orig = httpx.AsyncClient
        transport = httpx.MockTransport(h_chop)

        class PatchedClient(orig):
            def __init__(self, *a, **kw):
                kw["transport"] = transport
                super().__init__(*a, **kw)

        httpx.AsyncClient = PatchedClient
        try:
            async for _ in client.stream_chat("x", None, 1):
                pass
        except BackendError as e:
            return e.user_message
        finally:
            httpx.AsyncClient = orig
        return None

    msg = asyncio.run(run_chop())
    check("m5: обрыв без [DONE] -> STREAM_INTERRUPTED",
          msg == client.STREAM_INTERRUPTED, f"got {msg!r}")

    # ---- FIX-02: retry-логика и таймауты ----

    def _patch_transport(handler):
        """PatchedClient: подменяет httpx.AsyncClient на MockTransport."""
        orig = httpx.AsyncClient
        transport = httpx.MockTransport(handler)

        class PatchedClient(orig):
            def __init__(self, *a, **kw):
                kw["transport"] = transport
                super().__init__(*a, **kw)

        return orig, PatchedClient

    # m6: сетевая ошибка на 1-й и 2-й попытке -> успех на 3-й
    state = {"n": 0}

    def h_flaky(request):
        state["n"] += 1
        if state["n"] <= 2:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, headers={
            "Content-Type": "text/event-stream",
            "X-Conversation-Id": "9"},
            content=SSE_BODY.encode("utf-8"))

    async def run_flaky():
        orig, patched = _patch_transport(h_flaky)
        httpx.AsyncClient = patched
        try:
            got = []
            async for conv, e in client.stream_chat("x", None, 1):
                got.append((conv, e.kind))
            return got
        finally:
            httpx.AsyncClient = orig

    saved = (client.RETRY_WAIT_MIN, client.RETRY_WAIT_MAX)
    client.RETRY_WAIT_MIN = 0.0
    client.RETRY_WAIT_MAX = 0.0
    try:
        got = asyncio.run(run_flaky())
    finally:
        client.RETRY_WAIT_MIN, client.RETRY_WAIT_MAX = saved
    check("m6: сетевая ошибка x2 -> успех на 3-й попытке",
          [k for _, k in got] == ["delta", "tool", "tool_result", "delta", "done"]
          and state["n"] == 3 and got[0][0] == 9,
          f"n={state['n']} kinds={[k for _, k in got]}")

    # m7: все попытки падают -> NETWORK_ERROR (критерий готовности FIX-02)
    def h_dead(request):
        raise httpx.ConnectError("backend is down")

    async def run_dead():
        orig, patched = _patch_transport(h_dead)
        httpx.AsyncClient = patched
        try:
            async for _ in client.stream_chat("x", None, 1):
                pass
        except BackendError as e:
            return e.user_message
        finally:
            httpx.AsyncClient = orig
        return None

    client.RETRY_WAIT_MIN = 0.0
    client.RETRY_WAIT_MAX = 0.0
    try:
        msg = asyncio.run(run_dead())
    finally:
        client.RETRY_WAIT_MIN, client.RETRY_WAIT_MAX = saved
    check("m7: все retry исчерпаны -> NETWORK_ERROR",
          msg == client.NETWORK_ERROR, f"got {msg!r}")

    # m8: общий таймаут стрима -> BackendError(AGENT_TIMEOUT)
    async def h_slow_forever(request):
        """События идут, но [DONE] никогда не приходит.

        aiter_lines() у ответа зациклен: стрим никогда не иссякает,
        пока asyncio.timeout не погасит его на 0.1с.
        """
        resp = httpx.Response(200, headers={
            "Content-Type": "text/event-stream",
            "X-Conversation-Id": "11"},
            content='data: {"delta": "думаю..."}\n\n'.encode("utf-8"))

        async def endless_lines():
            while True:
                yield 'data: {"delta": "думаю..."}'
                yield ""
                await asyncio.sleep(0.5)

        resp.aiter_lines = endless_lines  # type: ignore[method-assign]
        return resp

    async def run_slow():
        orig, patched = _patch_transport(h_slow_forever)
        httpx.AsyncClient = patched
        try:
            async for _ in client.stream_chat("x", None, 1):
                pass
        except BackendError as e:
            return e.user_message
        finally:
            httpx.AsyncClient = orig
        return None

    saved_t = client.TOTAL_STREAM_TIMEOUT
    client.TOTAL_STREAM_TIMEOUT = 0.1
    try:
        msg = asyncio.run(run_slow())
    finally:
        client.TOTAL_STREAM_TIMEOUT = saved_t
    check("m8: таймаут стрима -> AGENT_TIMEOUT",
          msg == client.AGENT_TIMEOUT, f"got {msg!r}")

    # m9: 503 на подключении ретраится (FastAPI кратко упал)
    state503 = {"n": 0}

    def h_503_then_ok(request):
        state503["n"] += 1
        if state503["n"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, headers={
            "Content-Type": "text/event-stream",
            "X-Conversation-Id": "12"},
            content=SSE_BODY.encode("utf-8"))

    async def run_503():
        orig, patched = _patch_transport(h_503_then_ok)
        httpx.AsyncClient = patched
        try:
            got = []
            async for conv, e in client.stream_chat("x", None, 1):
                got.append(e.kind)
            return got
        finally:
            httpx.AsyncClient = orig

    client.RETRY_WAIT_MIN = 0.0
    client.RETRY_WAIT_MAX = 0.0
    try:
        got = asyncio.run(run_503())
    finally:
        client.RETRY_WAIT_MIN, client.RETRY_WAIT_MAX = saved
    check("m9: 503 -> retry -> успех",
          got == ["delta", "tool", "tool_result", "delta", "done"]
          and state503["n"] == 2,
          f"n={state503['n']} kinds={got}")


def main():
    test_parser()
    test_stream_mock()
    print(f"\nИтого: PASS={PASS} FAIL={FAIL}")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
