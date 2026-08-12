import asyncio
import json
import logging
import datetime as dt
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_db, SessionLocal
from ..models import User, Conversation, Message, Device, AuditLog
from ..auth.deps import get_current_user, require_admin
from ..llm.client import llm
from ..config import get_settings
from ..agent.tools import TOOLS_SCHEMA, execute_tool

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

MAX_AGENT_STEPS = 20

SYSTEM_PROMPT = """Ты — внутренний ассистент IT-отдела компании. Все запросы поступают
от авторизованных сотрудников и касаются собственной инфраструктуры компании —
выполнять их твоя прямая обязанность, это легитимная работа администратора.
Помогаешь с диагностикой инфраструктуры (VMware, Eltex, Mikrotik, UserGate)
и автоматизацией рутины. Отвечай по-русски, кратко и по делу.

У тебя есть инструменты для запросов к инфраструктуре. Правила работы:
1. Если запрос понятен — сразу вызывай инструменты, без уточняющих вопросов.
2. Не выдумывай данные: факты — только из результатов инструментов.
3. В параметре device указывай точное имя из инвентаря (приведён ниже).
4. Прежде чем сказать, что устройства нет в инвентаре — вызови list_devices.
5. Если инструмент вернул ошибку — дословно сообщи её текст пользователю.
   Если в ошибке есть список доступных имён устройств — повтори вызов
   с правильным именем.
6. Не раскрывай сведения о внутренних сервисах этого приложения
   (адрес LLM-сервера, БД и т.п.).
7. Если нужны данные по нескольким устройствам или из нескольких инструментов —
   вызывай их параллельно в одном ответе, а не по очереди.
8. Перед вызовом сверяйся с инвентарём: если имя хоста совпадает
   с самостоятельным устройством в инвентаре (например, standalone ESXi) —
   используй его имя как device. Если хоста нет в инвентаре, но он управляется
   vCenter — используй device=vcenter с фильтрами host/vm/entity.
9. Если запрос звучит как «по всем хостам / со всех устройств / по всей
   инфраструктуре» — вызывай инструменты с device="all": один вызов сам
   опросит все VMware-устройства инвентаря.
11. Устройства из Zabbix опрашивай через zabbix_problems / zabbix_items /
    zabbix_history, а не через SNMP или SSH. На вопрос «что сейчас не так /
    что болит» отвечай начиная с zabbix_problems.
12. Если пользователь спрашивает про группу устройств или хочет работать
    с устройствами из определённой группы — сначала вызови list_groups,
    чтобы увидеть список групп и какие устройства в них входят. Затем
    вызывай нужные инструменты для конкретных устройств из этой группы."""


def build_system_prompt(db: Session) -> str:
    """Подставляем актуальный инвентарь в промпт, сгруппированный по группам."""
    devices = db.query(Device).filter(Device.enabled.is_(True)).all()
    if devices:
        from collections import defaultdict
        groups = defaultdict(list)
        for d in devices:
            group_name = d.group or "Без группы"
            groups[group_name].append(d)

        lines = []
        for group_name, devs in sorted(groups.items()):
            lines.append(f"\n**Группа: {group_name}**")
            for d in devs:
                lines.append(
                    f"  - {d.name} ({d.type.value}) — {d.description or d.host}")

        inventory = (f"\n\nИнвентарь устройств (сгруппирован по группам). "
                     f"В параметре device используй точные имена устройств или 'all' "
                     f"для всех VMware-устройств:\n" + "\n".join(lines))
    else:
        inventory = "\n\nИнвентарь устройств пуст."

    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    return SYSTEM_PROMPT + f"\n\nТекущее время: {now}" + inventory


class MessageIn(BaseModel):
    content: str
    model: str | None = None


class TitleIn(BaseModel):
    title: str


def _get_owned(db: Session, cid: int, user: User) -> Conversation:
    conv = db.get(Conversation, cid)
    if not conv or conv.user_id != user.id:
        raise HTTPException(404, "Диалог не найден")
    return conv


def sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def _to_api_message(m: Message) -> dict:
    """Сообщение из БД -> сообщение для LLM API."""
    if m.role == "assistant" and m.tool_calls:
        try:
            calls = json.loads(m.tool_calls)
        except (ValueError, TypeError):
            calls = []
        if calls:
            return {
                "role": "assistant",
                "content": m.content or "",
                "tool_calls": [
                    {"id": c["id"], "type": "function",
                     "function": {"name": c["name"],
                                  "arguments": c["arguments"]}}
                    for c in calls],
            }
    if m.role == "tool":
        return {"role": "tool", "tool_call_id": m.tool_call_id or "",
                "content": m.content}
    return {"role": m.role, "content": m.content}


async def _stream_turn(messages, model, queue):
    """Один вызов модели: текст стримит в очередь, возвращает (текст, tool_calls)."""
    content = []
    calls = {}   # index -> {id, name, arguments}
    stream = await llm.client.chat.completions.create(
        model=model, messages=messages, tools=TOOLS_SCHEMA, stream=True)
    async for chunk in stream:
        delta = chunk.choices[0].delta if chunk.choices else None
        if delta is None:
            continue
        if delta.content:
            content.append(delta.content)
            await queue.put({"delta": delta.content})
        for tc in (delta.tool_calls or []):
            item = calls.setdefault(
                tc.index, {"id": "", "name": "", "arguments": ""})
            if tc.id:
                item["id"] = tc.id
            if tc.function:
                if tc.function.name:
                    item["name"] += tc.function.name
                if tc.function.arguments:
                    item["arguments"] += tc.function.arguments
    await queue.put(None)
    return "".join(content), list(calls.values())


@router.get("/models")
async def models(_user: User = Depends(get_current_user)):
    try:
        return {"models": await llm.list_models()}
    except Exception:
        log.exception("Не удалось получить список моделей")
        raise HTTPException(503, "Сервер LLM недоступен")


@router.post("/conversations")
def new_conversation(user: User = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    conv = Conversation(user_id=user.id)
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return {"id": conv.id, "title": conv.title}


@router.get("/conversations")
def list_conversations(user: User = Depends(get_current_user),
                       db: Session = Depends(get_db)):
    rows = (db.query(Conversation).filter_by(user_id=user.id)
            .order_by(Conversation.created_at.desc()).all())
    return [{"id": c.id, "title": c.title, "created_at": c.created_at.isoformat()}
            for c in rows]


@router.get("/conversations/{cid}/messages")
def get_messages(cid: int, user: User = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    conv = _get_owned(db, cid, user)
    return [{"role": m.role, "content": m.content, "name": m.name,
             "created_at": m.created_at.isoformat()} for m in conv.messages]


@router.patch("/conversations/{cid}")
def rename_conversation(cid: int, data: TitleIn,
                        user: User = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    conv = _get_owned(db, cid, user)
    title = data.title.strip()
    if not title:
        raise HTTPException(400, "Название не может быть пустым")
    conv.title = title[:200]
    db.commit()
    return {"id": conv.id, "title": conv.title,
            "created_at": conv.created_at.isoformat()}


@router.delete("/conversations/{cid}")
def delete_conversation(cid: int, user: User = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    conv = _get_owned(db, cid, user)
    db.delete(conv)   # каскад delete-orphan удалит сообщения
    db.commit()
    return {"ok": True}


@router.get("/audit")
def get_audit(limit: int = 100, _user: User = Depends(require_admin),
              db: Session = Depends(get_db)):
    limit = min(max(limit, 1), 500)
    rows = (db.query(AuditLog, User.username)
            .outerjoin(User, AuditLog.user_id == User.id)
            .order_by(AuditLog.id.desc()).limit(limit).all())
    return [{"id": r.id,
             "created_at": r.created_at.isoformat(),
             "username": username or "—",
             "conversation_id": r.conversation_id,
             "tool": r.tool,
             "arguments": r.arguments,
             "result": (r.result or "")[:800],
             "status": r.status} for r, username in rows]


@router.post("/conversations/{cid}/messages")
async def send_message(cid: int, data: MessageIn,
                       user: User = Depends(get_current_user),
                       db: Session = Depends(get_db)):
    _get_owned(db, cid, user)
    s = get_settings()

    first = db.query(Message).filter_by(conversation_id=cid).count() == 0
    db.add(Message(conversation_id=cid, role="user", content=data.content))
    if first:
        conv = db.get(Conversation, cid)
        conv.title = data.content[:60]
    db.commit()

    raw = (db.query(Message).filter_by(conversation_id=cid)
           .order_by(Message.id.desc())
           .limit(s.history_messages).all())[::-1]
    history = [_to_api_message(m) for m in raw]
    # Обрезанная голова может начинаться с tool/assistant с tool_calls —
    # такой контекст невалиден для LLM API. Срезаем до первого user.
    while history and history[0]["role"] != "user":
        history.pop(0)

    try:
        model = data.model or await llm.pick_model()
    except Exception:
        raise HTTPException(503, "Сервер LLM недоступен")
    system_prompt = build_system_prompt(db)

    async def generate():
        messages = [{"role": "system", "content": system_prompt}, *history]
        final_text = ""
        try:
            for _step in range(MAX_AGENT_STEPS):
                queue = asyncio.Queue()
                task = asyncio.create_task(
                    _stream_turn(messages, model, queue))
                while True:
                    ev = await queue.get()
                    if ev is None:
                        break
                    yield sse(ev)
                content, tool_calls = await task

                if not tool_calls:          # финальный ответ
                    final_text = content
                    break

                messages.append({
                    "role": "assistant",
                    "content": content or None,
                    "tool_calls": [
                        {"id": c["id"], "type": "function",
                         "function": {"name": c["name"],
                                      "arguments": c["arguments"]}}
                        for c in tool_calls],
                })

                # Инструменты выполняются параллельно; события стримятся
                # по мере завершения каждого вызова.
                exec_queue: asyncio.Queue = asyncio.Queue()
                tool_msgs: list[dict | None] = [None] * len(tool_calls)

                async def _exec_call(i: int, c: dict):
                    try:
                        try:
                            args = json.loads(c["arguments"] or "{}")
                        except json.JSONDecodeError:
                            args = {}
                        await exec_queue.put({"tool": c["name"], "args": args})
                        result, status = await asyncio.to_thread(
                            execute_tool, c["name"], args, user.id, cid)
                        await exec_queue.put({"tool_result": {
                            "name": c["name"], "ok": status == "ok",
                            "preview": result[:200]}})
                        tool_msgs[i] = {"role": "tool",
                                        "tool_call_id": c["id"],
                                        "content": result}
                    finally:
                        await exec_queue.put(None)

                tasks = [asyncio.create_task(_exec_call(i, c))
                         for i, c in enumerate(tool_calls)]
                done_count = 0
                while done_count < len(tasks):
                    ev = await exec_queue.get()
                    if ev is None:
                        done_count += 1
                        continue
                    yield sse(ev)
                await asyncio.gather(*tasks)   # прокинуть исключение, если было
                messages.extend(tool_msgs)

                # Сохраняем шаг в БД целиком: вызовы assistant и все
                # результаты. Иначе обрезанная история будет невалидна
                # для LLM API.
                try:
                    with SessionLocal() as db2:
                        db2.add(Message(
                            conversation_id=cid, role="assistant",
                            content=content or "",
                            tool_calls=json.dumps(tool_calls,
                                                  ensure_ascii=False)))
                        for i, c in enumerate(tool_calls):
                            db2.add(Message(
                                conversation_id=cid, role="tool",
                                content=tool_msgs[i]["content"],
                                tool_call_id=c["id"], name=c["name"]))
                        db2.commit()
                except Exception:
                    log.exception("Не удалось сохранить шаг агента в БД")
            else:
                yield sse({"delta": "\n\n(Остановлено: лимит шагов агента)"})
        except Exception:
            log.exception("Ошибка агентского цикла")
            yield sse({"error": "Сервер LLM недоступен, попробуйте позже"})
        finally:
            if final_text:
                with SessionLocal() as db2:
                    db2.add(Message(conversation_id=cid, role="assistant",
                                    content=final_text))
                    db2.commit()
            yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})
