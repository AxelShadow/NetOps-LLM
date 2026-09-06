"""Дев-фикстуры Фазы 12 (migration.md): тестовые пользователи,
устройства, диалоги с сообщениями, записи аудита.

Идемпотентно: повторный запуск обновляет, но не дублирует.
Запуск (docker): docker compose -f docker-compose.dev.yml run --rm seed
Запуск (локально): NETOPS_DATABASE_URL=sqlite:///netops.db python seed_dev.py
  (env задаётся ДО импорта app — как в тестах).

Пользователи:
  admin@example.com    admin    (пароль в DEV_MODE любой)
  engineer@example.com engineer
  viewer@example.com   viewer
DEV_MODE пропускает AD и проверку пароля — роль берётся из БД,
поэтому пользователи должны существовать ДО первого входа.
"""
import json
import logging
import os

# Импорт app только после возможного переопределения NETOPS_DATABASE_URL
# (SessionLocal/engine создаются на импорте db.py).
from app.db import Base, SessionLocal, engine
from app.main import _ensure_audit_columns, _ensure_message_columns, bootstrap_admin
from app.models import (AuditLog, Conversation, Device, DeviceType,
                        Message, Role, User)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("seed_dev")


def seed_users(db) -> dict[str, User]:
    """Тестовые пользователи; admin берёт роль admin, не viewer."""
    users: dict[str, User] = {}
    for username, role in (
            ("admin@example.com", Role.admin),
            ("engineer@example.com", Role.engineer),
            ("viewer@example.com", Role.viewer)):
        u = db.query(User).filter(User.username == username).first()
        if not u:
            u = User(username=username,
                     display_name=username.split("@")[0].capitalize(),
                     role=role, granted_by="seed_dev")
            db.add(u)
            db.flush()
            log.info("Создан пользователь %s (%s)", username, role.value)
        users[username] = u
    return users


def seed_devices(db) -> None:
    """Несколько устройств разных типов/групп (idempotent upsert)."""
    for name, dtype, host, group, descr in (
            ("core-rtr1", DeviceType.eltex, "10.10.10.1", "Сеть",
             "Ядро сети, стенд"),
            ("sw-access-1", DeviceType.mikrotik, "10.10.20.2", "Сеть",
             "Доступ-свитч, стенд"),
            ("fw-usergate", DeviceType.usergate, "10.10.30.3", "Сеть",
             "Межсетевой экран, стенд"),
            ("vcenter-stand", DeviceType.vcenter, "10.10.40.4", "VMware",
             "vCenter стенда"),
            ("esxi-stand-1", DeviceType.esxi, "10.10.40.5", "VMware",
             "Узел ESXi стенда"),
            ("srv-old", DeviceType.other, "10.10.50.6", "Прочее",
             "Старый сервер без типа")):
        d = db.query(Device).filter(Device.name == name).first()
        if not d:
            db.add(Device(name=name, type=dtype, host=host, group=group,
                          description=descr, source="manual"))
            log.info("Создано устройство %s (%s)", name, dtype.value)


def seed_conversations(db, users) -> None:
    """Два диалога с сообщениями: plain-вопрос и с tool_call-шагами."""
    admin = users["admin@example.com"]
    engineer = users["engineer@example.com"]

    # Диалог 1 — без инструментов
    c1 = db.query(Conversation).filter(
        Conversation.title == "Пинг core-rtr1 (мок)").first()
    if not c1:
        c1 = Conversation(user_id=admin.id, title="Пинг core-rtr1 (мок)")
        db.add(c1)
        db.flush()
        db.add_all([
            Message(conversation_id=c1.id, role="user",
                    content="Проверь пинг до core-rtr1"),
            Message(conversation_id=c1.id, role="assistant",
                    content="Устройство core-rtr1 отвечает: 0% потерь, "
                            "RTT 0.42 мс (мок-режим)."),
        ])
        log.info("Создан диалог «%s»", c1.title)

    # Диалог 2 — с tool_calls в сообщении ассистента
    c2 = db.query(Conversation).filter(
        Conversation.title == "Инвентарь VMware (мок)").first()
    if not c2:
        c2 = Conversation(user_id=engineer.id, title="Инвентарь VMware (мок)")
        db.add(c2)
        db.flush()
        db.add_all([
            Message(conversation_id=c2.id, role="user",
                    content="Покажи инвентарь VMware"),
            Message(
                conversation_id=c2.id, role="assistant",
                content="Запрошен список ВМ через vCenter.",
                tool_calls=json.dumps([
                    {"id": "call_seed_1", "type": "function",
                     "function": {"name": "vmware_list_vms",
                                  "arguments": "{}"}}],
                    ensure_ascii=False),
                tool_call_id="call_seed_1",
                name="vmware_list_vms"),
            Message(conversation_id=c2.id, role="tool",
                    content='{"vms": ["vm-web-01 (poweredOn)", '
                            '"vm-db-01 (poweredOff)"]}'),
            Message(conversation_id=c2.id, role="assistant",
                    content="На vCenter-стенде 2 ВМ: vm-web-01 включена, "
                            "vm-db-01 выключена (мок-режим)."),
        ])
        log.info("Создан диалог «%s»", c2.title)


SEED_MARK = "seed_dev"


def seed_audit(db, users) -> None:
    """Записи аудита: ok / error / denied — по одной на сценарий."""
    if db.query(AuditLog).filter(AuditLog.tool == SEED_MARK).count():
        return  # уже сеяли (маркер — строка tool="seed_dev")
    admin = users["admin@example.com"]
    viewer = users["viewer@example.com"]
    now_args = json.dumps({"device": "core-rtr1"}, ensure_ascii=False)
    db.add_all([
        # маркер идемпотентности: невидимая строка seed_dev
        AuditLog(user_id=admin.id, tool=SEED_MARK,
                 arguments="{}", result="", status="ok",
                 duration_ms=None),
        # успешный вызов инструмента
        AuditLog(user_id=admin.id, tool="network_ping",
                 arguments=now_args,
                 result="0% потерь, RTT 0.42 мс (мок)",
                 status="ok", duration_ms=120),
        # ошибка инструмента (триггер «mock-ошибка»)
        AuditLog(user_id=admin.id, tool="network_ping",
                 arguments=now_args,
                 result="Ошибка выполнения: mock-ошибка (мок)",
                 status="error", duration_ms=15),
        # отказ по роли: viewer зовёт admin-инструмент
        AuditLog(user_id=viewer.id, tool="vmware_list_vms",
                 arguments="{}",
                 result="Недостаточно прав (роль: viewer) для выполнения "
                        "'vmware_list_vms'.",
                 status="denied", duration_ms=None),
    ])
    log.info("Создано 3 записи аудита (ok/error/denied)")


def main() -> None:
    # как lifespan() FastAPI: создать таблицы + донести колонки
    # (alembic нет — миграции на ALTER TABLE при старте)
    Base.metadata.create_all(engine)
    _ensure_message_columns()
    _ensure_audit_columns()
    bootstrap_admin()
    with SessionLocal() as db:
        users = seed_users(db)
        seed_devices(db)
        seed_conversations(db, users)
        seed_audit(db, users)
        db.commit()
    log.info("Дев-фикстуры: готово. Пользователи: admin@ / engineer@ / "
             "viewer@example.com (в DEV_MODE пароль любой).")


if __name__ == "__main__":
    if not os.environ.get("NETOPS_DATABASE_URL"):
        # локальный запуск из backend/ без docker: рабочая sqlite
        # (в docker/тестах URL всегда задан явно)
        log.warning("NETOPS_DATABASE_URL не задан — используется "
                    "умолчание из app.config")
    main()
