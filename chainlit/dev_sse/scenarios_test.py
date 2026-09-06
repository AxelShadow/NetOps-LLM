"""Тест эталонных SSE-сценариев dev-режима (Фаза 12, migration.md):
python dev_sse/scenarios_test.py (venv chainlit).

Каждый .sse-файл в dev_sse/ прогоняется через РЕАЛЬНЫЙ парсер
Chainlit (client.parse_sse_line): все кадры должны распознаваться
(delta/tool/tool_result/error/done), без unknown и без потерь строк.
Сценарий timeout.sse — обрыв без [DONE] — единственный, где done
НЕ приходит (имитация зависшего стрима).

Конвенция проекта: глобальные PASS/FAIL, check(), sys.exit(0/1).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# client.py импортирует config -> обязательные env задаём ДО импорта
os.environ.setdefault("FASTAPI_INTERNAL_URL", "http://mock-backend:8000")
os.environ.setdefault("NETOPS_INTERNAL_SERVICE_TOKEN", "test-token")

from client import KNOWN_KINDS, parse_sse_line  # noqa: E402

PASS, FAIL = 0, 0
HERE = os.path.dirname(os.path.abspath(__file__))


def check(name: str, ok: bool, detail: str = ""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"PASS: {name}")
    else:
        FAIL += 1
        print(f"FAIL: {name} {detail}")


def parse_file(path: str) -> list[str]:
    """Прогнать файл через реальный парсер; вернуть список kinds.

    httpx.aiter_lines() отдаёт строки БЕЗ \\n (парсер ожидает именно
    такие), поэтому перевод строки срезаем до передачи.
    """
    kinds: list[str] = []
    buf: dict = {"data": []}
    with open(path, encoding="utf-8") as f:
        for line in f:
            ev = parse_sse_line(line.rstrip("\n"), buf)
            if ev is not None:
                kinds.append(ev.kind)
    return kinds


def main():
    # 1. success_tool_call.sse: полный цикл с одним инструментом
    kinds = parse_file(os.path.join(HERE, "success_tool_call.sse"))
    check("1: success = delta,tool,tool_result,delta,done",
          kinds == ["delta", "tool", "tool_result", "delta", "done"],
          f"kinds={kinds}")

    # 2. error_tool_call.sse: ok=false + финальный текст
    kinds = parse_file(os.path.join(HERE, "error_tool_call.sse"))
    check("2: error = delta,tool,tool_result,delta,done",
          kinds == ["delta", "tool", "tool_result", "delta", "done"],
          f"kinds={kinds}")

    # 3. timeout.sse: обрыв посреди ответа — done НЕ приходит,
    #    последний delta оборван (клиент покажет STREAM_INTERRUPTED)
    kinds = parse_file(os.path.join(HERE, "timeout.sse"))
    check("3: timeout = delta,tool,tool_result,delta (без done)",
          kinds == ["delta", "tool", "tool_result", "delta"],
          f"kinds={kinds}")

    # 4. multi_step.sse: два инструмента подряд
    kinds = parse_file(os.path.join(HERE, "multi_step.sse"))
    check("4: multi_step = delta,tool,tool_result,tool,tool_result,delta,done",
          kinds == ["delta", "tool", "tool_result", "tool", "tool_result",
                    "delta", "done"],
          f"kinds={kinds}")

    # 5. общий инвариант: во всех файлах только известные kinds
    for fname in ("success_tool_call.sse", "error_tool_call.sse",
                  "timeout.sse", "multi_step.sse"):
        kinds = parse_file(os.path.join(HERE, fname))
        unknowns = [k for k in kinds
                    if k not in KNOWN_KINDS and k != "done"]
        check(f"5: {fname} без unknown", not unknowns, f"unknowns={unknowns}")

    # 6. tool_result внутри сценариев содержит name/ok/preview
    buf: dict = {"data": []}
    sample = None
    with open(os.path.join(HERE, "success_tool_call.sse"), encoding="utf-8") as f:
        for line in f:
            ev = parse_sse_line(line.rstrip("\n"), buf)
            if ev is not None and ev.kind == "tool_result":
                sample = ev
    tr = sample.data["tool_result"] if sample else {}
    check("6: tool_result {name,ok,preview}",
          sample is not None and set(tr) >= {"name", "ok", "preview"}
          and tr["ok"] is True and tr["name"] == "ping",
          f"tr={tr}")

    print(f"\nИтого: PASS={PASS} FAIL={FAIL}")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
