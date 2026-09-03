import enum
import datetime as dt
from sqlalchemy import String, Boolean, DateTime, ForeignKey, Integer, Text, Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .db import Base


class Role(str, enum.Enum):
    admin = "admin"
    engineer = "engineer"
    viewer = "viewer"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(128), default="")
    role: Mapped[Role] = mapped_column(SAEnum(Role), default=Role.viewer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    granted_by: Mapped[str] = mapped_column(String(64), default="bootstrap")
    granted_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=lambda: dt.datetime.now(dt.UTC))

    conversations: Mapped[list["Conversation"]] = relationship(
        back_populates="user", cascade="all, delete-orphan")


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(200), default="Новый диалог")
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=lambda: dt.datetime.now(dt.UTC))

    user: Mapped[User] = relationship(back_populates="conversations")
    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan")


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))   # user | assistant | tool
    content: Mapped[str] = mapped_column(Text)
    # Для role="assistant" с tool-вызовами: JSON [{id, name, arguments}]
    tool_calls: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Для role="tool": id вызова и имя инструмента
    tool_call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=lambda: dt.datetime.now(dt.UTC))

    conversation: Mapped[Conversation] = relationship(
        back_populates="messages")


class DeviceType(str, enum.Enum):
    eltex = "eltex"
    mikrotik = "mikrotik"
    usergate = "usergate"
    vcenter = "vcenter"
    esxi = "esxi"
    other = "other"


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)   # core-rtr1
    type: Mapped[DeviceType] = mapped_column(SAEnum(DeviceType))
    host: Mapped[str] = mapped_column(String(128))
    port: Mapped[int] = mapped_column(default=0)       # 0 = дефолтный для типа
    username: Mapped[str] = mapped_column(String(64), default="")
    password: Mapped[str] = mapped_column(String(128), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    description: Mapped[str] = mapped_column(String(256), default="")
    source: Mapped[str] = mapped_column(
        String(16), default="manual")      # manual | zabbix
    zabbix_hostid: Mapped[str | None] = mapped_column(
        String(32), unique=True, default=None)
    group: Mapped[str] = mapped_column("group_name", String(128), default="")


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=lambda: dt.datetime.now(dt.UTC))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey("conversations.id"))
    tool: Mapped[str] = mapped_column(String(64))       # имя инструмента
    arguments: Mapped[str] = mapped_column(Text)        # JSON аргументов
    result: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16))     # ok | error | denied
    duration_ms: Mapped[int | None] = mapped_column(Integer)  # длительность вызова
