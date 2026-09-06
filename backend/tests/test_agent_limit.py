"""Интеграционный тест лимита шагов агента MAX_AGENT_STEPS=20 (Фаза 13).

Сценарий «лимит» мок-LLM (app/llm/client.py: «лимит» -> каждый ответ
tool_call get_current_time): агентский цикл /internal/chat/stream обязан
упереться ровно в 20 шагов и отдать кадр "(Остановлено: лимит шагов
агента)" (chat.py:356).

Фиксация поведения (не баг): текст «Остановлено» стримится в SSE,
но в БД не сохраняется — final_text="" и ветка chat.py:361 не
выполняется, финального assistant-сообщения в диалоге нет.

Стиль internal_api_test.py: TestClient на временной sqlite (env задаётся
ДО импорта app), авторизация сервисным токеном + X-User-Id (без логина).
Рабочая netops.db не затрагивается.

Запуск: .venv/Scripts/python.exe tests/test_agent_limit.py  (из backend/)
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

# Windows-консоль по умолчанию cp1251: кириллица в PASS/FAIL-строках
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TMP = tempfile.mkdtemp(prefix="netops_limit_")
os.environ["NETOPS_DATABASE_URL"] = f"sqlite:///{TMP}/limit_test.db".replace("\\", "/")
os.environ["NETOPS_MOCK_MODE"] = "true"
os.environ["NETOPS_DEV_MODE"] = "true"
os.environ["NETOPS_INTERNAL_SERVICE_TOKEN"] = "test-token"
os.environ["NETOPS_AD_DOMAIN"] = "mock.local"
os.environ["NETOPS_BOOTSTRAP_ADMIN"] = "admin@mock.local"
os.environ["NETOPS_JWT_SECRET"] = "agent-limit-test-secret"
os.environ["NETOPS_LLM_BASE_URL"] = "http://127.0.0.1:9/v1"  # ничего не слушает
# перекрывают backend/.env, если он задан (Zabbix в сценарии не участвует)
os.environ["NETOPS_ZABBIX_URL"] = ""
os.environ["NETOPS_ZABBIX_TOKEN"] = ""

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import AuditLog, Message, User  # noqa: E402

MAX_AGENT_STEPS = 20  # сверяется с app/api/chat.py:21
STOP_TEXT = "\n\n(Остановлено: лимит шагов агента)"  # chat.py:356

PASS, FAIL = 0, 0


def check(name: str, ok: bool, detail: str = ""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"PASS: {name}")
    else:
        FAIL += 1
        print(f"FAIL: {name} {detail}")


def parse_sse(resp) -> list:
    """Разбор SSE-кадров data: {...} + [DONE] в список полезной нагрузки."""
    events = []
    for line in resp.text.split("\n"):
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            events.append("[DONE]")
        elif payload:
            import json
            try:
                events.append(json.loads(payload))
            except json.JSONDecodeError:
                pass
    return events


def main():
    with TestClient(app) as client:
        # bootstrap-админ из lifespan (username="admin" — split_upn
        # от admin@mock.local); логин не нужен — /internal/* авторизуется
        # сервисным токеном + X-User-Id.
        with SessionLocal() as db:
            admin = db.query(User).filter_by(role="admin").first()
        assert admin is not None, "bootstrap-админ не создан"
        admin_id = admin.id

        r = client.post("/internal/chat/stream",
                        headers={"X-Internal-Service-Token": "test-token",
                                 "X-User-Id": str(admin_id)},
                        json={"content": "проверка лимита шагов агента"})

        # 1. ответ 200 + X-Conversation-Id
        conv_id_hdr = r.headers.get("X-Conversation-Id", "")
        check("200 + X-Conversation-Id числовой",
              r.status_code == 200 and conv_id_hdr.isdigit(),
              f"status={r.status_code} hdr={conv_id_hdr!r} {r.text[:120]}")
        conv_id = int(conv_id_hdr) if conv_id_hdr.isdigit() else -1

        events = parse_sse(r)

        # 2. ровно 20 кадров tool (каждый кадр — один ключ, chat.py:311/315)
        tool_frames = [e for e in events if isinstance(e, dict)
                       and "tool" in e]
        check("ровно 20 кадров tool, все get_current_time",
              len(tool_frames) == MAX_AGENT_STEPS
              and all(e["tool"] == "get_current_time"
                      for e in tool_frames),
              f"кадров: {len(tool_frames)}")

        # 3. ровно 20 кадров tool_result, все ok=true
        tr_frames = [e["tool_result"] for e in events
                     if isinstance(e, dict) and "tool_result" in e]
        check("ровно 20 кадров tool_result, все ok=true",
              len(tr_frames) == MAX_AGENT_STEPS
              and all(t.get("ok") is True
                      and t.get("name") == "get_current_time"
                      for t in tr_frames),
              f"кадров: {len(tr_frames)}")

        # 4. delta ровно один — уведомление об остановке (не текст модели)
        deltas = [e["delta"] for e in events
                  if isinstance(e, dict) and "delta" in e]
        check("delta ровно один: «(Остановлено: лимит шагов агента)»",
              len(deltas) == 1 and deltas[0] == STOP_TEXT,
              f"кадров: {len(deltas)} text={deltas[:1]!r}")

        # 5. [DONE] — последний (и единственный) кадр
        check("[DONE] — последний кадр",
              bool(events) and events[-1] == "[DONE]"
              and events.count("[DONE]") == 1,
              f"last={repr(events[-1]) if events else None}")

        # 6-8. БД и аудит по conversation_id из заголовка
        with SessionLocal() as db:
            n_user = (db.query(Message)
                      .filter_by(conversation_id=conv_id, role="user")
                      .count())
            assistants = (db.query(Message)
                          .filter(Message.conversation_id == conv_id,
                                  Message.role == "assistant")
                          .all())
            with_calls = [m for m in assistants if m.tool_calls]
            n_tool = (db.query(Message)
                      .filter_by(conversation_id=conv_id, role="tool")
                      .count())
            check("БД: 1 user + 20 assistant(tool_calls) + 20 role=tool",
                  n_user == 1 and len(assistants) == MAX_AGENT_STEPS
                  and len(with_calls) == MAX_AGENT_STEPS
                  and n_tool == MAX_AGENT_STEPS,
                  f"user={n_user} assistant={len(assistants)} "
                  f"with_calls={len(with_calls)} tool={n_tool}")

            # 7. финального assistant-сообщения с «Остановлено» НЕТ:
            # SSE-текст стримится (chat.py:356), но с final_text=""
            # не сохраняется (chat.py:361 не выполняется). Фиксация
            # поведения кода, не баг.
            stopped = [m for m in assistants
                       if "Остановлено" in (m.content or "")]
            check("БД: assistant-сообщения с «Остановлено» нет "
                  "(текст только в SSE)",
                  len(stopped) == 0,
                  f"найдено: {len(stopped)}")

            # 8. аудит: 20 записей get_current_time, все ok
            audit_rows = (db.query(AuditLog)
                          .filter_by(conversation_id=conv_id,
                                     tool="get_current_time").all())
            check("аудит: 20 записей get_current_time, все ok",
                  len(audit_rows) == MAX_AGENT_STEPS
                  and all(a.status == "ok" for a in audit_rows),
                  f"записей: {len(audit_rows)}")

    print(f"\nИтого: PASS={PASS} FAIL={FAIL}")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
