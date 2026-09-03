"""Интеграционный тест настроек /admin/settings (Фаза 5 миграции).

Стиль test_admin_ui_inventory.py: TestClient на временной sqlite (env задаётся
ДО импорта app), dev-логин bootstrap-админа. Сетевые проверки (LLM/Zabbix/
VMware) выполняются против недоступных адресов — проверяем коды ответов и
разметку результата, а не реальную связность. Рабочая netops.db не затрагивается.

Запуск: .venv/Scripts/python.exe tests/test_admin_ui_settings.py  (из backend/)
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

TMP = tempfile.mkdtemp(prefix="netops_set_")
os.environ["NETOPS_DATABASE_URL"] = f"sqlite:///{TMP}/set_test.db".replace("\\", "/")
os.environ["NETOPS_MOCK_MODE"] = "true"
os.environ["NETOPS_DEV_MODE"] = "true"
os.environ["NETOPS_AD_DOMAIN"] = "mock.local"
os.environ.setdefault("NETOPS_BOOTSTRAP_ADMIN", "admin@mock.local")
os.environ.setdefault("NETOPS_JWT_SECRET", "set-test-secret")
os.environ["NETOPS_LLM_BASE_URL"] = "http://localhost:59999/v1"  # ничего не слушает
os.environ["NETOPS_ZABBIX_URL"] = "http://localhost:59998/api_jsonrpc.php"
os.environ["NETOPS_ZABBIX_TOKEN"] = "zbx-token"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.auth.jwt_utils import create_token  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import Device, User  # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, ok: bool, detail: str = ""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"PASS: {name}")
    else:
        FAIL += 1
        print(f"FAIL: {name} {detail}")


def seed() -> tuple[int, int]:
    """Инженер + отключённое VMware-устройство; возвращает id."""
    with SessionLocal() as db:
        eng = User(username="eng1@mock.local", display_name="Инженер",
                   role="engineer", is_active=True, granted_by="test")
        db.add(eng)
        db.flush()
        eng_id = eng.id
        db.commit()
    return eng_id


def main():
    with TestClient(app) as client:
        eng_id = seed()  # после lifespan (create_all)

        # --- RBAC ---
        r = client.get("/admin/settings", follow_redirects=False)
        check("GET /admin/settings без cookie -> редирект на логин",
              r.status_code in (302, 303)
              and "/admin/login" in r.headers.get("location", ""),
              f"got {r.status_code}")

        r = client.post("/admin/login",
                        data={"username": "admin@mock.local", "password": "x"},
                        follow_redirects=False)
        check("POST /admin/login admin -> 303",
              r.status_code in (302, 303), f"got {r.status_code}")
        admin_token = r.cookies.get("netops_token")

        client.cookies.set("netops_token",
                           create_token(eng_id, "eng1@mock.local", "engineer"))
        r = client.get("/admin/settings", follow_redirects=False)
        check("GET /admin/settings (engineer) -> 403 или редирект",
              r.status_code == 403
              or (r.status_code in (302, 303)
                  and "/admin/login" in r.headers.get("location", "")),
              f"got {r.status_code}")
        r = client.get("/admin/settings/partial/check/llm")
        check("GET /admin/settings/partial/check/llm (engineer) -> 403",
              r.status_code == 403, f"got {r.status_code}")

        # Возврат в сессию admin
        client.cookies.set("netops_token", admin_token)

        # --- Страница настроек ---
        r = client.get("/admin/settings")
        ok = r.status_code == 200
        check("GET /admin/settings (admin) -> 200", ok, f"got {r.status_code}")
        if ok:
            t = r.text
            check("Заголовок «Настройки системы»",
                  "Настройки системы" in t)
            check("Секция подключений с кнопкой проверки LLM",
                  "partial/check/llm" in t and "Проверить" in t)
            check("Секция Zabbix с кнопкой проверки",
                  "partial/check/zabbix" in t)
            check("Секция VMware с кнопкой проверки",
                  "partial/check/vmware" in t)
            check("Показан URL LLM",
                  "http://localhost:59999/v1" in t)
            check("Показан «Лимит шагов агента»",
                  "Лимит шагов агента" in t)
            check("Показан DEV_MODE",
                  "Режим разработки (DEV_MODE)" in t)
            check("Показана версия приложения (git-хеш или dev)",
                  "Версия приложения" in t)
            check("Показан «Мок-режим»",
                  "Мок-режим" in t)
            check("Секрет JWT не утекает на страницу",
                  "set-test-secret" not in t)
            check("Токен Zabbix не утекает на страницу",
                  "zbx-token" not in t)

        # --- HTMX: проверка LLM ---
        r = client.get("/admin/settings/partial/check/llm")
        ok = r.status_code == 200
        check("GET partial/check/llm (admin) -> 200", ok,
              f"got {r.status_code}")
        if ok:
            # mock_mode=true -> должен сообщить «Мок-режим» без сети
            check("LLM в мок-режиме -> «Мок-режим»",
                  "Мок-режим" in r.text and "Подключено" not in r.text,
                  f"body: {r.text[:200]}")

        # --- HTMX: проверка Zabbix против недоступного адреса ---
        r = client.get("/admin/settings/partial/check/zabbix")
        ok = r.status_code == 200
        check("GET partial/check/zabbix (admin) -> 200", ok,
              f"got {r.status_code}")
        if ok:
            check("Zabbix недоступен -> «Недоступно»",
                  "Недоступно" in r.text,
                  f"body: {r.text[:200]}")

        # --- HTMX: проверка VMware без устройств в инвентаре ---
        r = client.get("/admin/settings/partial/check/vmware")
        ok = r.status_code == 200
        check("GET partial/check/vmware (admin) -> 200", ok,
              f"got {r.status_code}")
        if ok:
            check("VMware без устройств -> «Нет устройств»",
                  "Нет устройств" in r.text,
                  f"body: {r.text[:200]}")

        # --- VMware: с устройством в инвентаре (адрес недоступен) ---
        with SessionLocal() as db:
            vm = Device(name="vc-test", type="vcenter", host="10.255.255.1",
                        port=443, username="u", password="p",
                        enabled=True, source="manual")
            db.add(vm)
            db.commit()
        r = client.get("/admin/settings/partial/check/vmware")
        ok = r.status_code == 200
        check("GET partial/check/vmware с устройством -> 200", ok,
              f"got {r.status_code}")
        if ok:
            check("VMware устройство в ответе, ошибка недоступности",
                  "vc-test" in r.text and "Недоступно" in r.text,
                  f"body: {r.text[:300]}")

        # --- VMware: таймаут проверки (не дольше ~5 c на «чёрной дыре») ---
        import time
        t0 = time.monotonic()
        r = client.get("/admin/settings/partial/check/vmware")
        elapsed = time.monotonic() - t0
        check("VMware-проверка ограничена таймаутом (<8 c)",
              r.status_code == 200 and elapsed < 8.0,
              f"elapsed {elapsed:.1f}s, status {r.status_code}")

        print(f"\nИтого: PASS={PASS} FAIL={FAIL}")
        sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
