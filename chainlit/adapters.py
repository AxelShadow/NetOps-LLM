"""Маппинг событий внутреннего SSE-потока в элементы Chainlit UI."""
from typing import Optional

import chainlit as cl

from client import SSEEvent

# Сколько символов preview инструмента показывать (Фаза 8 сделает свёртку)
MAX_PREVIEW = 400


def _fmt_args(args: dict) -> str:
    """Аргументы инструмента одной строкой (кратко, без дампов)."""
    if not args:
        return "—"
    return ", ".join(f"{k}={v!r}" for k, v in list(args.items())[:5])


def _fmt_result(name: str, ok: bool, preview: str) -> str:
    """Результат инструмента: ✔/✖ как в старом SPA."""
    mark = "✔" if ok else "✖"
    text = (preview or "")[:MAX_PREVIEW]
    suffix = " …" if len(preview or "") > MAX_PREVIEW else ""
    return f"{mark} {name}: {text}{suffix}"


async def render_event(ev: SSEEvent) -> Optional[cl.Step]:
    """Одно SSE-событие -> UI. Возвращает открытый Step или None.

    delta -> None (текст стримит app.py в общий ответ)
    tool  -> новый cl.Step «Инструмент: {name}», сохранить в сессию
    tool_result -> обновить последний Step (✔/✖ + preview)
    error / done -> None (обрабатывает app.py)
    """
    if ev.kind == "tool":
        step = cl.Step(name=f"Инструмент: {ev.data['tool']}")
        step.output = f"Аргументы: {_fmt_args(ev.data.get('args') or {})}"
        await step.send()
        cl.user_session.set("current_step", step)
        return step
    if ev.kind == "tool_result":
        tr = ev.data["tool_result"]
        step = cl.user_session.get("current_step")
        if step is None:
            # tool_result без tool (не должно случаться) — свой шаг
            step = cl.Step(name=f"Инструмент: {tr.get('name', '?')}")
        step.output = _fmt_result(tr.get("name", "?"),
                                  bool(tr.get("ok")),
                                  tr.get("preview", ""))
        await step.update()
        cl.user_session.set("current_step", None)
        return step
    return None
