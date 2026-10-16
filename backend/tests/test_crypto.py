"""Тест шифрования чувствительных данных (app/crypto.py).

Проверяются свойства, из-за которых модуль переделывался:

1. round-trip: encrypt_value -> decrypt_value возвращает исходное значение,
   включая кириллицу и «длинные» пароли;
2. пустая строка остаётся пустой (и в шифровании, и в расшифровке) —
   «значение не задано» не ошибка;
3. legacy-plaintext (значения, записанные до внедрения шифрования) читаются
   как есть и при этом не путаются с шифротекстом;
4. ключ не задан + прод-режим -> KeyConfigurationError с внятным текстом
   (никакой молчаливой генерации нового ключа);
5. ключ не задан + dev-режим -> временный ключ, шифрование работает;
6. шифротекст, записанный ДРУГИМ ключом, не превращается молча в мусор:
   decrypt_value падает InvalidToken (а не отдаёт base64 на устройство);
7. is_encrypted отличает токен Fernet от обычного текста;
8. в текстах ошибок и метаданных нет значения ключа.

Изоляция: NETOPS_* задаются ДО импорта app.crypto; get_fernet.cache_clear()
между сценариями. Рабочая netops.db не затрагивается — модуль её не читает.

Запуск (из backend/): .venv/Scripts/python.exe tests/test_crypto.py
"""
import os
import sys
from base64 import b64decode, b64encode
from pathlib import Path

# Windows-консоль по умолчанию cp1251: кириллица в выводе не кодируется
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

# --- env ДО импорта app.* (ключ задаём тестовый, не боевой) ---
TEST_KEY_ENV = "NETOPS_ENCRYPTION_KEY"
DEV_MODE_ENV = "NETOPS_DEV_MODE"
os.environ.pop(TEST_KEY_ENV, None)
os.environ[DEV_MODE_ENV] = "false"

from cryptography.fernet import Fernet, InvalidToken  # noqa: E402

from app import crypto  # noqa: E402

FAILED = 0
PASSED = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    """Печатает результат проверки; счётчики — для итоговой сводки."""
    global FAILED, PASSED
    if condition:
        PASSED += 1
        print(f"  OK   {name}")
    else:
        FAILED += 1
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))


def reset_key(value: str | None, dev_mode: bool) -> None:
    """Переключает окружение модуля и сбрасывает кэш Fernet."""
    os.environ[DEV_MODE_ENV] = "true" if dev_mode else "false"
    if value is None:
        os.environ.pop(TEST_KEY_ENV, None)
    else:
        os.environ[TEST_KEY_ENV] = value
    crypto._dev_fallback = None  # noqa: SLF001 — сброс dev-ключа между сценариями
    crypto.reset_cache()


def main() -> int:
    print("1. Ключ задан: round-trip и пустые значения")
    reset_key(Fernet.generate_key().decode("ascii"), dev_mode=False)

    for sample in ("p@ssw0rd", "пароль-с-кириллицей", "x" * 200, "public"):
        encrypted = crypto.encrypt_value(sample)
        check(f"round-trip {sample[:12]!r}", crypto.decrypt_value(encrypted) == sample)
        check(f"значение зашифровано {sample[:12]!r}", encrypted != sample)
    check("пустая строка шифруется в пустую", crypto.encrypt_value("") == "")
    check("пустая строка расшифровывается в пустую", crypto.decrypt_value("") == "")

    print("2. is_encrypted и legacy-plaintext")
    encoded = crypto.encrypt_value("secret-value")
    decoded = b64decode(encoded + "=" * (-len(encoded) % 4))
    check("первый байт токена — версия 0x80", decoded[0] == 0x80)
    check("токен Fernet распознан", crypto.is_encrypted(encoded))
    check("обычный текст не распознан", not crypto.is_encrypted("public"))
    check("короткая строка не распознана", not crypto.is_encrypted("abc"))
    check("пустая строка не распознана", not crypto.is_encrypted(""))
    check("legacy-plaintext читается как есть",
          crypto.decrypt_value("legacy-password") == "legacy-password")
    check("legacy public читается как есть",
          crypto.decrypt_value("public") == "public")

    print("3. Прод-режим без ключа: явный отказ")
    reset_key(None, dev_mode=False)
    for name, call in (
        ("encrypt_value", lambda: crypto.encrypt_value("x")),
        ("decrypt_value", lambda: crypto.decrypt_value("x")),
    ):
        try:
            call()
            check(f"{name} без ключа падает", False, "исключения не было")
        except crypto.KeyConfigurationError as exc:
            text = str(exc)
            check(f"{name} без ключа падает KeyConfigurationError", True)
            check(f"{name}: в тексте есть имя переменной", TEST_KEY_ENV in text)
            check(f"{name}: в тексте нет значения ключа", "gAAAAA" not in text)
        except Exception as exc:  # noqa: BLE001 — фиксируем неверный тип
            check(f"{name} без ключа падает KeyConfigurationError", False,
                  f"получено {type(exc).__name__}")

    print("4. Dev-режим без ключа: временный ключ, round-trip работает")
    reset_key(None, dev_mode=True)
    info = crypto.get_key_info()
    # Ключ сгенерирован внутри процесса: достаём его байты, чтобы убедиться,
    # что метаданные его не раскрывают (сравниваем значение, не имя).
    generated_key = crypto.get_fernet()._signing_key  # noqa: SLF001 — тест
    check("get_key_info: ключ не задан", info["configured"] is False)
    check("get_key_info: длина 0", info["key_chars"] == 0)
    check("get_key_info: dev_fallback включён", info["dev_fallback"] is True)
    check("get_key_info: имя переменной указано", info["env_var"] == TEST_KEY_ENV)
    check("get_key_info: значение ключа не отдаётся",
          generated_key.hex() not in str(info.values()))
    check("dev-режим шифрует и расшифровывает",
          crypto.decrypt_value(crypto.encrypt_value("dev-pass")) == "dev-pass")

    print("5. Метаданные ключа при заданном ключе")
    key = Fernet.generate_key().decode("ascii")
    reset_key(key, dev_mode=False)
    info = crypto.get_key_info()
    check("get_key_info: ключ задан", info["configured"] is True)
    check("get_key_info: длина ключа совпадает", info["key_chars"] == len(key))
    check("get_key_info: dev_fallback выключен", info["dev_fallback"] is False)
    check("get_key_info: значение ключа не отдаётся", key not in str(info.values()))

    print("6. Другой ключ: шифротекст не отдаётся как plaintext")
    other_key = Fernet.generate_key().decode("ascii")
    # Токен Fernet уже url-safe base64: дополнительное кодирование не нужно.
    foreign_token = Fernet(other_key.encode("ascii")).encrypt(
        b"real-secret").decode("ascii")
    check("токен Fernet нужной длины", len(foreign_token) >= 73)
    check("чужой токен распознан как шифротекст",
          crypto.is_encrypted(foreign_token))
    try:
        result = crypto.decrypt_value(foreign_token)
        check("чужой токен не расшифровывается молча", False,
              f"вернулось значение длиной {len(result)}")
    except InvalidToken:
        check("чужой токен даёт InvalidToken", True)
    except Exception as exc:  # noqa: BLE001
        check("чужой токен даёт InvalidToken", False, f"получено {type(exc).__name__}")

    print()
    print(f"Итог: {PASSED} OK, {FAILED} FAIL")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
