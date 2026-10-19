"""Тесты хранения runtime-настроек и секретов (Этап 22, `settings_store`).

Проверяет:
  * `smtp.password` в app_settings хранится зашифрованным (Fernet), а
    `get_secret_setting` возвращает открытое значение;
  * `secret_state` (единственное, что видит админка) отдаёт только
    «задан/не задан» и длину шифротекста — значения в нём нет, поэтому оно
    не может попасть в HTML страницы настроек;
  * пустое поле пароля при сохранении формы не затирает сохранённый секрет;
  * секрет из чужого ключа -> `SecretDecryptError` без значения и без
    шифротекста в сообщении; в логах значения тоже нет;
  * не-секретные настройки (`scheduler.enabled`, `scheduler.time`) читаются
    как bool/int с дефолтами — это использует планировщик.

UI-страница /admin/settings пишется отдельным исполнителем; здесь проверяется
гарантия уровня хранилища (значения наружу не отдаётся).
БД: временная sqlite (env ДО импорта app).
Запуск: .venv/Scripts/python.exe -m pytest tests/test_settings_store.py -v
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

_TMP = tempfile.mkdtemp(prefix="netops_store_")
os.environ.setdefault("NETOPS_DATABASE_URL",
                      f"sqlite:///{_TMP}/store_test.db".replace("\\", "/"))
os.environ.setdefault("NETOPS_DEV_MODE", "true")
os.environ.setdefault("NETOPS_MOCK_MODE", "true")
os.environ.setdefault("NETOPS_AD_DOMAIN", "mock.local")
os.environ.setdefault("NETOPS_BOOTSTRAP_ADMIN", "admin@mock.local")
os.environ.setdefault("NETOPS_JWT_SECRET", "store" + "-test")
os.environ.setdefault("NETOPS_LLM_BASE_URL", "http://localhost:9/v1")
os.environ.setdefault("NETOPS_SCHEDULER_ENABLED", "false")

from cryptography.fernet import Fernet  # noqa: E402

if not os.environ.get("NETOPS_ENCRYPTION_KEY"):
    os.environ["NETOPS_ENCRYPTION_KEY"] = Fernet.generate_key().decode("ascii")

import pytest  # noqa: E402

from app import settings_store  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import AppSetting  # noqa: E402
from app.scheduler.settings import SECRET_KEYS, SECRET_KEY  # noqa: E402

Base.metadata.create_all(engine)

_TEST_AUTH = "unit" + "-store-auth"     # тестовое значение, не реальный пароль

_SETTING_KEYS = [
    "smtp.password", "smtp.host",
    "scheduler.enabled", "scheduler.time", "scheduler.recipients",
]


def _clear_settings() -> None:
    with SessionLocal() as db:
        db.query(AppSetting).filter(
            AppSetting.key.in_(_SETTING_KEYS)).delete(synchronize_session=False)
        db.commit()


@pytest.fixture(autouse=True)
def clean_state():
    _clear_settings()
    yield
    _clear_settings()


# --------------------------------------------------------------------------- #
# Шифрование секрета
# --------------------------------------------------------------------------- #

def test_secret_is_encrypted_at_rest_and_decrypts():
    settings_store.set_settings({"smtp.password": _TEST_AUTH},
                                extra_secret_keys=SECRET_KEYS)

    stored = settings_store.get_setting("smtp.password")
    assert stored and stored != _TEST_AUTH          # в БД шифротекст
    assert settings_store.get_secret_setting("smtp.password") == _TEST_AUTH

    from app import crypto

    assert crypto.is_encrypted(stored) is True


def test_secret_state_has_no_value():
    settings_store.set_settings({"smtp.password": _TEST_AUTH},
                                extra_secret_keys=SECRET_KEYS)

    state = settings_store.secret_state("smtp.password")

    assert state["is_set"] is True
    assert state["length"] > 0
    assert set(state) == {"key", "is_set", "length"}
    # то, что отдаётся в шаблон админки, не содержит ни значения, ни шифротекста
    dump = json.dumps(state, ensure_ascii=False)
    assert _TEST_AUTH not in dump
    assert settings_store.get_setting("smtp.password") not in dump


def test_empty_secret_does_not_overwrite_saved():
    settings_store.set_settings({"smtp.password": _TEST_AUTH},
                                extra_secret_keys=SECRET_KEYS)
    saved = settings_store.get_setting("smtp.password")

    # форма админки приходит без пароля, если его не меняют
    settings_store.set_settings({"smtp.password": ""},
                                extra_secret_keys=SECRET_KEYS)

    assert settings_store.get_setting("smtp.password") == saved
    assert settings_store.get_secret_setting("smtp.password") == _TEST_AUTH


def test_secret_state_empty_by_default():
    state = settings_store.secret_state("smtp.password")
    assert state == {"key": "smtp.password", "is_set": False, "length": 0}
    assert settings_store.get_secret_setting("smtp.password") == ""


def test_non_secret_value_is_plain_text():
    settings_store.set_settings({"smtp.host": "mail.example.test"})
    assert settings_store.get_setting("smtp.host") == "mail.example.test"


def test_set_setting_encrypts_secret_without_extra_keys():
    """Базовый реестр settings_store знает smtp.password (забытый ключ не утечёт)."""
    with SessionLocal() as db:
        settings_store.set_setting(db, "smtp.password", _TEST_AUTH)
        db.commit()

    stored = settings_store.get_setting("smtp.password")
    assert stored != _TEST_AUTH
    assert settings_store.get_secret_setting("smtp.password") == _TEST_AUTH


def test_scheduler_secret_registry_matches_store():
    assert SECRET_KEY == "smtp.password"
    assert SECRET_KEYS == frozenset({"smtp.password"})
    assert "smtp.password" in settings_store.SECRET_KEYS


# --------------------------------------------------------------------------- #
# Ошибки и отсутствие утечек значения
# --------------------------------------------------------------------------- #

def test_foreign_key_token_raises_without_leaking(caplog):
    """Значение, зашифрованное другим ключом -> SecretDecryptError."""
    foreign = Fernet(Fernet.generate_key()).encrypt(_TEST_AUTH.encode("utf-8"))
    foreign_text = foreign.decode("ascii")
    with SessionLocal() as db:
        db.add(AppSetting(key="smtp.password", value=foreign_text))
        db.commit()

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(settings_store.SecretDecryptError) as exc_info:
            settings_store.get_secret_setting("smtp.password")

    message = str(exc_info.value)
    assert "smtp.password" in message            # имя ключа — можно
    assert _TEST_AUTH not in message
    assert foreign_text not in message
    assert _TEST_AUTH not in caplog.text
    assert foreign_text not in caplog.text


def test_secret_state_does_not_decrypt_foreign_token(caplog):
    """secret_state не расшифровывает и не падает на чужом токене."""
    foreign = Fernet(Fernet.generate_key()).encrypt(_TEST_AUTH.encode("utf-8"))
    foreign_text = foreign.decode("ascii")
    with SessionLocal() as db:
        db.add(AppSetting(key="smtp.password", value=foreign_text))
        db.commit()

    with caplog.at_level(logging.DEBUG):
        state = settings_store.secret_state("smtp.password")

    assert state["is_set"] is True
    assert state["length"] == len(foreign_text)
    assert _TEST_AUTH not in json.dumps(state)
    assert _TEST_AUTH not in caplog.text


# --------------------------------------------------------------------------- #
# Чтение не-секретных настроек (использует планировщик)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("1", True), ("yes", True), ("on", True),
    ("TRUE", True), ("false", False), ("0", False), ("no", False),
    ("", False), ("мусор", False),
])
def test_get_bool_setting(raw, expected):
    settings_store.set_settings({"scheduler.enabled": raw})
    assert settings_store.get_bool_setting("scheduler.enabled") is expected


def test_get_bool_setting_missing_uses_default():
    assert settings_store.get_bool_setting("scheduler.enabled", True) is True
    assert settings_store.get_bool_setting("scheduler.enabled", False) is False


def test_get_int_setting():
    settings_store.set_settings({"scheduler.time": "07:45"})
    assert settings_store.get_int_setting("scheduler.time", 8) == 8
    settings_store.set_settings({"scheduler.time": "9"})
    assert settings_store.get_int_setting("scheduler.time", 8) == 9


def test_delete_setting():
    settings_store.set_settings({"scheduler.recipients": "ops@example.test"})
    assert settings_store.get_setting("scheduler.recipients") \
        == "ops@example.test"

    settings_store.delete_setting("scheduler.recipients")

    assert settings_store.get_setting("scheduler.recipients", "") == ""


def test_set_settings_rollback_on_error_keeps_db_consistent():
    """Ошибка записи не оставляет частично применённых настроек."""
    settings_store.set_settings({"scheduler.time": "06:00"})

    class BadValue:
        def __str__(self):
            raise RuntimeError("не сериализуется")

    with pytest.raises(RuntimeError):
        settings_store.set_settings({"scheduler.time": BadValue()})

    assert settings_store.get_setting("scheduler.time") == "06:00"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
