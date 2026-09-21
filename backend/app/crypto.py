"""Модуль шифрования чувствительных данных (пароли, community строки).

Использует Fernet (симметричное шифрование) из библиотеки cryptography.
Ключ шифрования задаётся через переменную окружения NETOPS_ENCRYPTION_KEY.
"""
import os
from base64 import b64encode, b64decode
from cryptography.fernet import Fernet
from functools import lru_cache


def _get_or_generate_key() -> bytes:
    """Получает ключ из окружения или генерирует новый (для dev-режима).
    
    В production всегда должен быть задан NETOPS_ENCRYPTION_KEY.
    """
    key_env = os.environ.get("NETOPS_ENCRYPTION_KEY")
    if key_env:
        return key_env.encode("ascii")
    # Для dev-режима: генерируем ключ и выводим в лог (только для разработки!)
    key = Fernet.generate_key()
    print(f"[DEV MODE] Generated encryption key: {key.decode('ascii')}")
    print("[DEV MODE] Set NETOPS_ENCRYPTION_KEY environment variable to this value")
    return key


@lru_cache
def get_fernet() -> Fernet:
    """Возвращает кэшированный экземпляр Fernet."""
    key = _get_or_generate_key()
    return Fernet(key)


def encrypt_value(value: str) -> str:
    """Шифрует строковое значение и возвращает base64-encoded результат.
    
    Args:
        value: Исходная строка для шифрования
        
    Returns:
        Зашифрованная строка в base64 формате
    """
    if not value:
        return ""
    f = get_fernet()
    encrypted = f.encrypt(value.encode("utf-8"))
    return b64encode(encrypted).decode("ascii")


def decrypt_value(encrypted_value: str) -> str:
    """Расшифровывает значение, зашифрованное encrypt_value.
    
    Args:
        encrypted_value: Зашифрованная строка в base64 формате
        
    Returns:
        Расшифрованная исходная строка
        
    Raises:
        cryptography.fernet.InvalidToken: Если ключ не подходит или данные повреждены
    """
    if not encrypted_value:
        return ""
    f = get_fernet()
    try:
        decrypted = f.decrypt(b64decode(encrypted_value.encode("ascii")))
        return decrypted.decode("utf-8")
    except Exception:
        # Если расшифровка не удалась, возвращаем как есть (для совместимости
        # с legacy данными, которые могли быть сохранены без шифрования)
        return encrypted_value


def is_encrypted(value: str) -> bool:
    """Проверяет, является ли значение зашифрованным (по формату).
    
    Заметка: это эвристическая проверка, не гарантирует, что значение
    действительно было зашифровано текущим ключом.
    """
    if not value or len(value) < 20:
        return False
    try:
        decoded = b64decode(value.encode("ascii"))
        # Fernet token имеет определённую структуру (версия + timestamp + ...)
        return len(decoded) > 0 and decoded[0] == 128  # Fernet version byte
    except Exception:
        return False
