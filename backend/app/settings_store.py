"""Runtime-настройки приложения: единая точка чтения/записи `app_settings`.

Зачем отдельный модуль: настройки появились в Этапе 20 (параметры агента), в
Этапе 21 (RAG) и Этапе 22 (утренние отчёты) — дублировать код «достать/создать
строку AppSetting» в каждом роутере нельзя, а правила хранения секретов должны
быть в одном месте.

Правила:

* Значение в БД — строка. Булевы/целые конвертируются здесь, чтобы вьюшки и
  задания планировщика не повторяли `int()`/`lower()`.
* **Секреты** (имя ключа в наборе `SECRET_KEYS` модуля-потребителя, например
  `smtp.password`) хранятся зашифрованными Fernet (`app/crypto.py`, ключ
  `NETOPS_ENCRYPTION_KEY`) и наружу в открытом виде не отдаются: для UI есть
  `secret_state()` — «задан/не задан» и ДЛИНА, без значения.
* Значения никогда не пишутся в логи и в тексты ошибок: только имена ключей и
  длины (`.dsh/security/policy.md`, уровни S3).
* Схема таблицы общая: `key` (PK) + `value` (TEXT после авто-миграции в
  `main.py`). Какие ключи бывают — определяется реестрами потребителей.
"""
from __future__ import annotations

from typing import Iterable

from sqlalchemy.orm import Session

from . import crypto
from .db import SessionLocal
from .models import AppSetting

# Секретные ключи, известные приложению. Модуль-потребитель может объявить
# свои (scheduler/settings.py::SECRET_KEYS) — здесь только базовый реестр,
# чтобы забытый ключ не сохранился открытым текстом.
SECRET_KEYS: frozenset[str] = frozenset({"smtp.password"})


class SecretDecryptError(RuntimeError):
    """Секрет есть в БД, но не расшифровывается текущим ключом."""


class SettingsError(RuntimeError):
    """Ошибка сохранения/чтения runtime-настроек."""


def _is_secret(key: str, extra_secret_keys: Iterable[str] = ()) -> bool:
    return key in SECRET_KEYS or key in set(extra_secret_keys)


def _get_row(db: Session, key: str) -> AppSetting | None:
    return db.get(AppSetting, key)


def get_setting(key: str, default: str = "") -> str:
    """Сырое значение настройки (БЕЗ расшифровки; для секретов — шифротекст)."""
    with SessionLocal() as db:
        row = _get_row(db, key)
        return row.value if row is not None and row.value is not None else default


def get_bool_setting(key: str, default: bool = False) -> bool:
    raw = get_setting(key, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def get_int_setting(key: str, default: int = 0) -> int:
    raw = get_setting(key, "").strip()
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def get_secret_setting(key: str) -> str:
    """Расшифрованное значение секрета.

    Raises:
        SecretDecryptError: значение есть, но ключ шифрования другой/битый.
            Текст ошибки не содержит ни значения, ни ключа.
    """
    raw = get_setting(key, "")
    if not raw:
        return ""
    try:
        return crypto.decrypt_value(raw)
    except crypto.InvalidToken as exc:  # cryptography.InvalidToken
        raise SecretDecryptError(
            f"Секрет {key} не расшифровывается текущим NETOPS_ENCRYPTION_KEY "
            "(другой ключ или повреждённое значение)"
        ) from exc


def secret_state(key: str) -> dict:
    """Состояние секрета для UI: задан ли и какой длины (без значения)."""
    raw = get_setting(key, "")
    return {"key": key, "is_set": bool(raw), "length": len(raw) if raw else 0}


def set_setting(db: Session, key: str, value: str,
                extra_secret_keys: Iterable[str] = ()) -> None:
    """Записывает одну настройку (секрет — шифрует). Коммит — на вызывающем."""
    text = "" if value is None else str(value)
    if text and _is_secret(key, extra_secret_keys):
        text = crypto.encrypt_value(text)
    row = _get_row(db, key)
    if row is None:
        db.add(AppSetting(key=key, value=text))
    else:
        import datetime as _dt
        row.value = text
        row.updated_at = _dt.datetime.now(_dt.UTC)


def set_settings(values: dict[str, str],
                 extra_secret_keys: Iterable[str] = ()) -> None:
    """Записывает набор настроек одной транзакцией; пустые секреты не затирают
    сохранённое значение (форма админки приходит без пароля)."""
    with SessionLocal() as db:
        try:
            for key, value in values.items():
                if _is_secret(key, extra_secret_keys) and not value:
                    continue  # пустое поле = «не менять пароль»
                set_setting(db, key, value, extra_secret_keys)
            db.commit()
        except Exception:
            db.rollback()
            raise


def delete_setting(key: str) -> None:
    with SessionLocal() as db:
        row = _get_row(db, key)
        if row is not None:
            db.delete(row)
            db.commit()
