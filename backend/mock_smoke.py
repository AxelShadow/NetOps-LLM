"""Интеграционный smoke-тест мок-режима NETOPS_MOCK_MODE (Фаза 0b).

Изолированная БД во временном файле (netops.db НЕ трогается).
Сценарии (SSE-поток POST /api/conversations/{id}/messages):
  a) обычный: tool get_current_time → tool_result ok → delta → [DONE]
  b) «ошибка»: tool_result ok=false, в аудите запись status="error"
  c) «лимит»: остановка по лимиту шагов агента, >5 tool-вызовов в БД
  d) прямые вызовы мок-инструментов: ping/vmware_vms/zabbix_problems
     возвращают фейковые данные (без сети)

Запуск: python mock_smoke.py   (из каталога backend/)
"""
import json
import os
import sys
import tempfile

# --- изоляция БД и мок-режим: ДО импорта приложения ---
_TMP = tempfile.mkdtemp(prefix="netops_mock_")
os.environ["NETOPS_DATABASE_URL"] = f"sqlite:///{_TMP}/mock_test.db".replace("\\", "/")
os.environ["NETOPS_MOCK_MODE"] = "true"
os.environ["NETOPS_DEV_MODE"] = "true"
os.environ.setdefault("NETOPS_JWT_SECRET", "mock-smoke-secret")
os.environ.setdefault("NETOPS_LLM_BASE_URL", "http://localhost:1234/v1")
os.environ["NETOPS_AD_DOMAIN"] = "mock.local"
os.environ.setdefault("NETOPS_BOOTSTRAP_ADMIN", "admin@mock.local")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import Message  # noqa: E402
from app.agent.tools import execute_tool  # noqa: E402

client = TestClient(app)
client.__enter__()   # запуск lifespan: create_all + миграции + bootstrap-админ

RESULTS = []


def check(name: str, cond: bool, extra: str = ""):
    RESULTS.append((name, bool(cond), extra))
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f" — {extra}" if extra else ""))


def parse_sse(resp) -> list[dict]:
    """Кадры SSE -> список dict (data: {...} и [DONE])."""
    frames = []
    for block in resp.text.split("\n\n"):
        for line in block.splitlines():
            if not line.startswith("data: "):
                continue
            payload = line[len("data: "):]
            if payload.strip() == "[DONE]":
                frames.append({"_done": True})
            else:
                try:
                    frames.append(json.loads(payload))
                except json.JSONDecodeError:
                    frames.append({"_raw": payload})
    return frames


# --- логин (dev-режим) ---
r = client.post("/api/auth/login",
                json={"username": "admin@mock.local", "password": "x"})
check("dev-логин", r.status_code == 200, str(r.status_code))
token = r.json()["token"]
H = {"Authorization": f"Bearer {token}"}


def make_conv(title: str) -> int:
    r = client.post("/api/conversations", headers=H)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def send(cid: int, text: str) -> list[dict]:
    r = client.post(f"/api/conversations/{cid}/messages",
                    headers=H, json={"content": text})
    assert r.status_code == 200, r.text
    return parse_sse(r)


# --- c) сценарий «лимит» (отдельный диалог, чтобы не мешать аудиту a/b) ---
cid_lim = make_conv("mock limit")
frames_lim = send(cid_lim, "проверим лимит шагов агента")
tool_ev_lim = [f for f in frames_lim if "tool" in f]
done_lim = any(f.get("_done") for f in frames_lim)
texts_lim = "".join(f["delta"] for f in frames_lim if "delta" in f)
check("лимит: [DONE] присутствует", done_lim)
check("лимит: >5 tool-вызовов", len(tool_ev_lim) > 5, f"tool-событий: {len(tool_ev_lim)}")
check("лимит: сообщение о лимите шагов", "лимит шагов" in texts_lim.lower(), texts_lim[:120])

with SessionLocal() as db:
    n_tool_lim = (db.query(Message)
                  .filter(Message.conversation_id == cid_lim,
                          Message.role == "tool").count())
check("лимит: >5 tool-сообщений в БД", n_tool_lim > 5, f"в БД: {n_tool_lim}")

# --- a) обычный сценарий ---
cid = make_conv("mock normal")
frames = send(cid, "Здравствуй, покажи текущее время")
check("обычный: есть tool get_current_time",
      any(f.get("tool") == "get_current_time" for f in frames))
tr = [f for f in frames if "tool_result" in f]
check("обычный: tool_result ok=true",
      any(fr["tool_result"].get("ok") is True for fr in tr),
      str(tr[:1]))
delta_text = "".join(f["delta"] for f in frames if "delta" in f)
check("обычный: есть delta-текст", "мок-режима" in delta_text, delta_text[:100])
check("обычный: [DONE]", any(f.get("_done") for f in frames))

# --- b) сценарий «ошибка» ---
cid_err = make_conv("mock error")
frames_err = send(cid_err, "выполни проверку с ошибкой")
tr_err = [f["tool_result"] for f in frames_err if "tool_result" in f]
check("ошибка: tool_result ok=false",
      any(t.get("ok") is False for t in tr_err), str(tr_err[:2]))
r = client.get("/api/audit?limit=200", headers=H)
audit = r.json() if isinstance(r.json(), list) else r.json().get("items", r.json())
err_rows = [a for a in audit
            if a.get("status") == "error" and a.get("conversation_id") == cid_err]
check("ошибка: в аудите status=error", len(err_rows) >= 1,
      f"записей: {len(err_rows)}")

# --- d) прямые мок-вызовы инструментов ---
res, st = execute_tool("ping", {"host": "srv-app-01"}, user_role="admin")
check("tool: ping мок ok", st == "ok" and "reachable" in res, st)
res, st = execute_tool("vmware_vms", {}, user_role="admin")
check("tool: vmware_vms мок ok", st == "ok" and "srv-app-01" in res, st)
res, st = execute_tool("zabbix_problems", {}, user_role="admin")
check("tool: zabbix_problems мок ok", st == "ok" and "esxi-02" in res, st)
res, st = execute_tool("get_infrastructure_health", {}, user_role="admin")
check("tool: composite health мок ok", st == "ok" and "zabbix_problems" in res, st)

# --- итог ---
failed = [n for n, ok, _ in RESULTS if not ok]
print(f"\n{'='*50}\nИТОГ: {len(RESULTS) - len(failed)}/{len(RESULTS)} PASS")
if failed:
    print("ПРОВАЛЕНЫ:", ", ".join(failed))
    sys.exit(1)
print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
