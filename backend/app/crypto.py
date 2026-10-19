"""Шифрование чувствительных данных в БД (пароли устройств, SNMP community).

Модель хранения: Fernet (симметричное шифрование, `cryptography`), ключ — из
переменной окружения `NETOPS_ENCRYPTION_KEY`. Схема — «обязательный ключ,
явный отказ»:

* ключ задан — работаем как обычно;
* ключа нет и `NETOPS_DEV_MODE=true` — генерируем временный ключ на процесс,
  пишем предупреждение. Значения, записанные с таким ключом, станут нечитаемы
  после рестарта (ключ больше не тот) — это осознанно только для dev;
* ключа нет и `NETOPS_DEV_MODE` выключен (прод) — `KeyConfigurationError`
  при первом же обращении к шифруемому полю. Молчаливая ротация ключа
  запрещена: она превращает уже записанные пароли в мусор, который потом
  уходит на устройство как «неверный пароль».

Ключи и значения не попадают в логи и тексты ошибок: пишем только имена
переменных и длины (см. `.dsh/security/policy.md`, уровни S3).

Генерация ключа для .env описана в `.env.example` — сам ключ в этом модуле
не хранится и не печатается.
"""
from __future__ import annotations

import logging
import os
from base64 import urlsafe_b64decode
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger(__name__)

ENV_KEY = "NETOPS_ENCRYPTION_KEY"
ENV_DEV_MODE = "NETOPS_DEV_MODE"

# Временный ключ dev-режима: генерируется один раз на процесс. Иначе каждый
# вызов encrypt/decrypt брал бы НОВЫЙ ключ, и значение не читалось бы даже
# внутри одного запуска.
_dev_fallback: bytes | None = None


class KeyConfigurationError(RuntimeError):
    """Ключ шифрования не задан там, где он обязателен.

    Текст исключения не содержит значений: только имя переменной и то, как
    её задать.
    """


def _env_flag(name: str) -> bool:
    """Разбирает булев флаг окружения как pydantic-settings (true/1/yes/on)."""
    truthy = {"1", "true", "yes", "on"}
    return os.environ.get(name, "").strip().lower() in truthy


def _get_or_generate_key() -> bytes:
    """Возвращает ключ Fernet из окружения.

    Raises:
        KeyConfigurationError: ключ не задан и dev-режим выключен.
    """
    global _dev_fallback
    from_environment = os.environ.get(ENV_KEY, "").strip()
    if from_environment:
        return from_environment.encode("ascii")

    if _env_flag(ENV_DEV_MODE):
        if _dev_fallback is None:
            _dev_fallback = Fernet.generate_key()
            # Значение ключа в лог НЕ пишем — только факт и способ исправления.
            log.warning(
                "%s не задан, сгенерирован временный ключ (%d байт) на время "
                "процесса: данные, записанные сейчас, будут нечитаемы после "
                "перезапуска. Задайте %s в файле backend\\"
                ".env, чтобы ключ был постоянным.",
                ENV_KEY, len(_dev_fallback), ENV_KEY,
            )
        return _dev_fallback

    raise KeyConfigurationError(
        f"{ENV_KEY} не задан, а {ENV_DEV_MODE} выключен (прод-режим). "
        f"Задайте {ENV_KEY} — иначе шифровать пароли устройств и SNMP "
        "community нельзя. Способ генерации описан в файле-примере."
    )


@lru_cache
def _cached_fernet(key: str) -> Fernet:
    """Кэш по значению ключа: ключ процесса не меняется, но смена значения
    (тесты, перезапуск с другим .env) не должна отдавать старый экземпляр."""
    return Fernet(key.encode("ascii"))


def get_fernet() -> Fernet:
    """Экземпляр Fernet для текущего ключа окружения.

    Raises:
        KeyConfigurationError: ключ не задан в прод-режиме.
    """
    return _cached_fernet(_get_or_generate_key().decode("ascii"))


def reset_cache() -> None:
    """Сбрасывает кэш Fernet — для тестов и смены ключа в процессе."""
    _cached_fernet.cache_clear()


def get_key_info() -> dict[str, object]:
    """Метаданные ключа без самого ключа: для диагностики и health-check."""
    raw = os.environ.get(ENV_KEY, "").strip()
    configured = bool(raw)
    return {
        "env_var": ENV_KEY,
        "configured": configured,
        "key_chars": len(raw) if configured else 0,
        "dev_fallback": not configured and _env_flag(ENV_DEV_MODE),
    }


def encrypt_value(value: str) -> str:
    """Шифрует значение; пустая строка остаётся пустой.

    Возвращает сам токен Fernet (он уже URL-safe base64): повторное
    кодирование base64 добавляло бы лишний слой.

    Raises:
        KeyConfigurationError: ключ не задан в прод-режиме.
    """
    if not value:
        return ""
    return get_fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_value(stored_value: str) -> str:
    """Расшифровывает значение, записанное `encrypt_value`.

    Пустая строка (и None) — пустая строка: «значение не задано» не является
    ошибкой.

    Legacy: значения, записанные до внедрения шифрования (обычный текст),
    возвращаются как есть — их можно читать, а при следующей записи устройства
    они уже шифруются.

    Raises:
        KeyConfigurationError: ключ не задан в прод-режиме.
        InvalidToken: значение похоже на токен Fernet, но не расшифровывается
            текущим ключом (другой ключ или порча данных). Молча отдавать
            шифротекст нельзя: он ушёл бы на устройство вместо пароля.
    """
    if not stored_value:
        return ""
    try:
        return get_fernet().decrypt(
            stored_value.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeEncodeError):
        # Не токен Fernet (обычный текст, в том числе не-ASCII) — это
        # legacy-значение до внедрения шифрования.
        if is_encrypted(stored_value):
            # Похоже на токен, но ключ не тот или данные повреждены.
            raise
        log.warning(
            "Обнаружено незашифрованное (legacy) значение в БД — "
            "прочитано как есть; будет зашифровано при следующей записи."
        )
        return stored_value


def _token_bytes(value: str) -> bytes:
    """Байты токена Fernet (url-safe base64, паддинг восстанавливается).

    Fernet кодирует токен url-safe алфавитом ('-' и '_'). Стандартный
    b64decode молча выбрасывает эти символы, из-за чего разбор случайных
    токенов падал (флейк: ~20% токенов) и decrypt_value возвращал
    шифротекст как legacy-plaintext. Поэтому используем urlsafe-декодер:

    * сначала пытаемся как есть (токен уже с паддингом от Fernet);
    * если паддинг потерян/испорчен — добиваем до кратного 4 и повторяем.
    """
    try:
        return urlsafe_b64decode(value.encode("ascii"))
    except Exception:
        padded = value + "=" * (-len(value) % 4)
        return urlsafe_b64decode(padded.encode("ascii"))


def is_encrypted(value: str) -> bool:
    """Похоже ли значение на токен Fernet.

    Проверка эвристическая и не подтверждает, что токен создан ТЕКУЩИМ
    ключом: она нужна только чтобы отличить legacy-plaintext от шифротекста.
    Признак — валидный base64 и версия токена Fernet (0x80) в первом байте;
    длину не проверяем, чтобы не зависеть от деталей реализации.
    """
    if not value:
        return False
    try:
        decoded = _token_bytes(value)
    except Exception:
        return False
    return bool(decoded) and decoded[0] == 0x80

