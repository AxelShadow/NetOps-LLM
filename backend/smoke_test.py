"""Быстрая интеграционная проверка новых эндпоинтов без LLM."""
import os
import json
import tempfile

# Настройки ДО импорта приложения (префикс NETOPS_, перекрывает .env)
tmpdb = tempfile.mktemp(suffix=".db")
os.environ["NETOPS_DATABASE_URL"] = f"sqlite:///{tmpdb}"
os.environ["NETOPS_DEV_MODE"] = "true"
os.environ["NETOPS_BOOTSTRAP_ADMIN"] = "admin@id.samges.ru"
os.environ["NETOPS_JWT_SECRET"] = "test-secret"

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import Message, AuditLog  # noqa: E402

client = TestClient(app)
client.__enter__()   # запуск lifespan: create_all + миграция + bootstrap

# вход в dev-режиме (AD пропускается)
r = client.post("/api/auth/login", json={
    "username": "admin@id.samges.ru", "password": "x"})
assert r.status_code == 200, r.text
token = r.json()["token"]
H = {"Authorization": f"Bearer {token}"}

me = client.get("/api/auth/me", headers=H).json()
print("whoami:", me["username"], me["role"])
assert me["role"] == "admin", "bootstrap-админ должен быть admin"

# --- CRUD диалогов ---
cid = client.post("/api/conversations", headers=H).json()["id"]
print("создан диалог", cid)

r = client.patch(f"/api/conversations/{cid}", headers=H,
                 json={"title": "Тестовый заголовок"})
assert r.status_code == 200 and r.json()["title"] == "Тестовый заголовок", r.text

r = client.patch(f"/api/conversations/{cid}", headers=H, json={"title": "   "})
assert r.status_code == 400, "пустой title должен давать 400"

lst = client.get("/api/conversations", headers=H).json()
assert any(c["id"] == cid and c["title"] == "Тестовый заголовок" for c in lst)

# --- сообщения с tool-ролями ---
with SessionLocal() as db:
    db.add(Message(conversation_id=cid, role="user", content="привет"))
    db.add(Message(conversation_id=cid, role="assistant", content="",
                   tool_calls=json.dumps(
                       [{"id": "call_1", "name": "ping",
                         "arguments": '{"host":"x"}'}])))
    db.add(Message(conversation_id=cid, role="tool",
                   tool_call_id="call_1", name="ping", content="OK 1ms"))
    db.add(Message(conversation_id=cid, role="assistant", content="Готово"))
    db.add(AuditLog(user_id=None, conversation_id=cid, tool="ping",
                    arguments='{"host":"x"}', result="OK 1ms", status="ok"))
    db.commit()

msgs = client.get(f"/api/conversations/{cid}/messages", headers=H).json()
assert [m["role"] for m in msgs] == ["user", "assistant", "tool", "assistant"], msgs
tool_msg = [m for m in msgs if m["role"] == "tool"][0]
assert tool_msg["name"] == "ping", tool_msg
print("messages ok, tool-сообщение с name:", tool_msg["name"])

# конверсия истории для LLM API
from app.api.chat import _to_api_message  # noqa: E402
with SessionLocal() as db:
    all_msgs = db.query(Message).filter_by(conversation_id=cid).order_by(Message.id).all()
api_msgs = [_to_api_message(m) for m in all_msgs]
print(json.dumps(api_msgs, ensure_ascii=False, indent=1))
assert api_msgs[1]["tool_calls"][0]["function"]["name"] == "ping"
assert api_msgs[2]["role"] == "tool" and api_msgs[2]["tool_call_id"] == "call_1"

# --- аудит ---
audit = client.get("/api/audit?limit=10", headers=H).json()
mine = [a for a in audit if a["conversation_id"] == cid]
assert len(mine) == 1 and mine[0]["tool"] == "ping", audit
assert len(mine[0]["result"]) <= 800
print("audit ok:", mine[0]["tool"], mine[0]["status"])

# --- аудит доступен только админам ---
with SessionLocal() as db:
    from app.models import User, Role
    db.add(User(username="viewertest", role=Role.viewer, is_active=True))
    db.commit()
from app.auth.jwt_utils import create_token
with SessionLocal() as db:
    v = db.query(User).filter_by(username="viewertest").first()
    vt = create_token(v.id, v.username, v.role.value)
r = client.get("/api/audit", headers={"Authorization": f"Bearer {vt}"})
assert r.status_code == 403, r.text
print("аудит для не-админа: 403 ok")

# --- удаление диалога + каскад ---
assert client.delete(f"/api/conversations/{cid}", headers=H).status_code == 200
assert client.get(f"/api/conversations/{cid}/messages", headers=H).status_code == 404
with SessionLocal() as db:
    assert db.query(Message).filter_by(conversation_id=cid).count() == 0, \
        "каскад должен удалить сообщения"
print("delete + каскад ok")

# --- миграция колонок на существующей БД ---
from app.main import _ensure_message_columns
_ensure_message_columns()  # повторный вызов не должен падать
print("повторная миграция не падает")

client.__exit__(None, None, None)
print("\nВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
try:
    os.unlink(tmpdb)
except OSError:
    pass   # временный файл, блокируется на Windows
