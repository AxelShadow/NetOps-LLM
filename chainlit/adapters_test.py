"""Тест chainlit-адаптера SSE-событий (Фаза 13): python adapters_test.py (venv chainlit).

Проверяет adapters.py: _fmt_args, _fmt_result и async render_event
(SSEEvent -> cl.Step). chainlit подменяется MagicMock (adapters.cl = mock):
сервер НЕ запускается, реальный cl.Step вне живой chainlit-сессии падает
(ChainlitContextException), поэтому Step/user_session тестируем на моках.
Асинхронность — через asyncio.run.

Конвенция проекта: глобальные PASS/FAIL, check(), sys.exit(0/1).
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Имена проверок и ожидания содержат ✔/✖/→/…, а stdout в Windows-пайпе
# по умолчанию cp1251 — переводим на UTF-8 до первого print.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (ValueError, OSError):
        pass

# client.py импортирует config -> обязательные env задаём ДО импорта
os.environ.setdefault("FASTAPI_INTERNAL_URL", "http://mock-backend:8000")
os.environ.setdefault("NETOPS_INTERNAL_SERVICE_TOKEN", "test-token")

from unittest.mock import AsyncMock, MagicMock  # noqa: E402

import adapters  # noqa: E402
from client import SSEEvent  # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, ok: bool, detail: str = ""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"PASS: {name}")
    else:
        FAIL += 1
        print(f"FAIL: {name} {detail}")


def fresh_cl() -> tuple[MagicMock, dict]:
    """Новый mock chainlit: подменяет adapters.cl целиком.

    - cl.Step — MagicMock-«класс»; cl.Step(...) возвращает один и тот же
      step-объект (return_value) с AsyncMock send/update;
    - cl.user_session.get/set работают поверх dict session_store
      (side_effect на dict.get / dict.__setitem__).

    Возвращает (mock_cl, session_store). Переприсвоение adapters.cl ПОСЛЕ
    import adapters действует на все обращения cl.X внутри render_event.
    """
    mock_cl = MagicMock()
    session_store: dict = {}
    mock_cl.user_session.get.side_effect = session_store.get
    mock_cl.user_session.set.side_effect = session_store.__setitem__
    step = MagicMock()
    step.send = AsyncMock()
    step.update = AsyncMock()
    mock_cl.Step.return_value = step
    adapters.cl = mock_cl
    return mock_cl, session_store


def test_fmt_args():
    # 1: пустые аргументы -> «—»
    got = adapters._fmt_args({})
    check("1: _fmt_args: пустые аргументы → «—»", got == "—", f"got {got!r}")

    # 2: 7 аргументов -> ровно первые 5 пар, формат k=v!r (python-repr)
    args7 = {"host": "mock-host", "count": 3, "verbose": True,
             "tags": ["a", "b"], "mode": None, "extra1": 1, "extra2": 2}
    expected5 = ("host='mock-host', count=3, verbose=True, "
                "tags=['a', 'b'], mode=None")
    got = adapters._fmt_args(args7)
    check("2: _fmt_args: 7 аргументов → ровно первые 5 пар (k=v!r)",
          got == expected5 and "extra1" not in got and "extra2" not in got,
          f"got {got!r}")


def test_fmt_result():
    got = adapters._fmt_result("ping", True, "pong")
    check("3: _fmt_result: ok → «✔ ping: pong» без суффикса",
          got == "✔ ping: pong", f"got {got!r}")

    got = adapters._fmt_result("ping", False, "timeout")
    check("4: _fmt_result: не-ok → «✖ ping: timeout»",
          got == "✖ ping: timeout", f"got {got!r}")

    # 5: усечение до MAX_PREVIEW=400 (+ « …» только при превышении)
    over = adapters._fmt_result("t", True, "a" * 401)
    exact = adapters._fmt_result("t", True, "b" * 400)
    ok_over = over == "✔ t: " + "a" * 400 + " …"
    ok_exact = exact == "✔ t: " + "b" * 400 and not exact.endswith(" …")
    check("5: усечение: 401 символ → 400 + « …»; ровно 400 → без « …»",
          ok_over and ok_exact,
          f"over_tail={over[-5:]!r} over_ok={ok_over}; "
          f"exact_ok={ok_exact} exact_len={len(exact)}")


def test_render_other_kinds():
    # 6: delta -> None (текст стримит app.py), Step не создаётся
    mock_cl, _ = fresh_cl()
    res = asyncio.run(adapters.render_event(
        SSEEvent(kind="delta", data={"delta": "Привет"})))
    check("6: render_event(delta) → None, Step не создавался",
          res is None and mock_cl.Step.call_count == 0,
          f"res={res!r} step_calls={mock_cl.Step.call_count}")

    # 7: error и done -> None (обрабатывает app.py), Step не создаётся
    mock_cl, _ = fresh_cl()
    res_err = asyncio.run(adapters.render_event(
        SSEEvent(kind="error", data={"error": "Сервер LLM недоступен"})))
    res_done = asyncio.run(adapters.render_event(
        SSEEvent(kind="done", data="")))
    check("7: render_event(error) и (done) → None, Step не создавался",
          res_err is None and res_done is None
          and mock_cl.Step.call_count == 0,
          f"error={res_err!r} done={res_done!r} "
          f"step_calls={mock_cl.Step.call_count}")


def test_render_tool_steps():
    # 8: tool -> новый Step, output с аргументами, send, session, return
    mock_cl, store = fresh_cl()
    step = asyncio.run(adapters.render_event(
        SSEEvent(kind="tool",
                 data={"tool": "ping", "args": {"host": "mock-host"}})))
    step_obj = mock_cl.Step.return_value
    name_ok = (mock_cl.Step.call_count == 1
               and mock_cl.Step.call_args is not None
               and mock_cl.Step.call_args.kwargs == {"name": "Инструмент: ping"})
    check("8: tool → Step «Инструмент: ping», output «Аргументы: …», "
          "send awaited, session set, return step",
          name_ok and step is step_obj
          and step_obj.output == "Аргументы: host='mock-host'"
          and step_obj.send.await_count == 1
          and step_obj.update.await_count == 0
          and store.get("current_step") is step_obj,
          f"name_ok={name_ok} same={step is step_obj} "
          f"output={step_obj.output!r} send={step_obj.send.await_count} "
          f"session={store.get('current_step') is step_obj}")

    # 9: tool, затем tool_result при открытом current_step (нормальный цикл)
    mock_cl, store = fresh_cl()
    step_tool = asyncio.run(adapters.render_event(
        SSEEvent(kind="tool",
                 data={"tool": "ping", "args": {"host": "mock-host"}})))
    step_res = asyncio.run(adapters.render_event(
        SSEEvent(kind="tool_result",
                 data={"tool_result": {"name": "ping", "ok": True,
                                       "preview": "pong"}})))
    check("9: tool_result при открытом current_step → output «✔ ping: pong», "
          "update awaited, сброс в None",
          step_res is step_tool and mock_cl.Step.call_count == 1
          and step_res.output == "✔ ping: pong"
          and step_res.update.await_count == 1
          and step_res.send.await_count == 1
          and store.get("current_step") is None,
          f"same={step_res is step_tool} step_calls={mock_cl.Step.call_count} "
          f"output={step_res.output!r} update={step_res.update.await_count} "
          f"current_step={store.get('current_step')!r}")

    # 10: tool_result без tool (current_step=None) -> собственный Step
    mock_cl, store = fresh_cl()
    step_orphan = asyncio.run(adapters.render_event(
        SSEEvent(kind="tool_result",
                 data={"tool_result": {"name": "ping", "ok": True,
                                       "preview": "pong"}})))
    step_obj = mock_cl.Step.return_value
    name_ok = (mock_cl.Step.call_count == 1
               and mock_cl.Step.call_args is not None
               and mock_cl.Step.call_args.kwargs == {"name": "Инструмент: ping"})
    check("10: tool_result без tool (current_step=None) → создан собственный "
          "Step «Инструмент: ping»",
          name_ok and step_orphan is step_obj
          and step_orphan.output == "✔ ping: pong"
          and step_orphan.update.await_count == 1
          and store.get("current_step") is None,
          f"name_ok={name_ok} same={step_orphan is step_obj} "
          f"output={step_orphan.output!r} "
          f"update={step_orphan.update.await_count}")

    # 11: tool_result без name -> «?» в заголовке Step и в output
    mock_cl, store = fresh_cl()
    step_unk = asyncio.run(adapters.render_event(
        SSEEvent(kind="tool_result",
                 data={"tool_result": {"ok": False, "preview": "timeout"}})))
    step_obj = mock_cl.Step.return_value
    name_ok = (mock_cl.Step.call_count == 1
               and mock_cl.Step.call_args is not None
               and mock_cl.Step.call_args.kwargs == {"name": "Инструмент: ?"})
    check("11: tool_result без name → «?» в заголовке Step",
          name_ok and step_unk is step_obj
          and step_unk.output == "✖ ?: timeout",
          f"name_ok={name_ok} output={step_unk.output!r}")


def main():
    test_fmt_args()
    test_fmt_result()
    test_render_other_kinds()
    test_render_tool_steps()
    print(f"\nИтого: PASS={PASS} FAIL={FAIL}")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
