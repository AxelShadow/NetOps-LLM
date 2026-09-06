"""Тест Фазы 14 «Замена старого интерфейса» миграции UI (migration.md).

Переключение NETOPS_USE_NEW_UI и сохранение старого SPA на /legacy:

- Сценарий 1 (use_new_ui=false, дефолт): GET / -> 303 /legacy/
  (старый SPA остаётся основным интерфейсом), GET /legacy/ -> 200
  со «NetOps LLM» (StaticFiles из frontend/legacy), несуществующий
  файл в /legacy/ -> 404 от StaticFiles (не SPA-fallback);
- Сценарий 2 (use_new_ui=true): GET / -> 200 лендинг со ссылками
  «Чат», /legacy/, /admin/ (templates/pages/landing.html);
- Сценарий 3: перенос git mv frontend/index.html ->
  frontend/legacy/index.html не сломал mount: GET /admin/login -> 200.

Факты прод-кода, на которые опираются проверки:
- home.py: роут GET / вызывает get_settings() ВНУТРИ обработчика,
  поэтому lru_cache перезаполняется get_settings.cache_clear() и
  новый запрос сразу видит изменённый NETOPS_USE_NEW_UI — без
  пересоздания TestClient (config.py: @lru_cache get_settings);
- main.py: frontend/legacy монтируется на /legacy (StaticFiles
  html=True -> /legacy/ отдаёт index.html);
- ui/router.py: роутер админки с prefix="/admin", логин-страница
  pages/login.html — 200 без авторизации.

Рабочая netops.db не затрагивается: NETOPS_DATABASE_URL задаётся на
временную sqlite ДО импорта app.* (как в tests/test_e2e_flow.py).

Запуск: .venv/Scripts/python.exe tests/test_ui_switch.py  (из backend/)
"""
import os
import sys
import tempfile
from pathlib import Path

# Windows-консоль по умолчанию cp1251: кириллица лендинга не кодируется
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

# --- Изоляция: env ДО импорта app.* (как test_e2e_flow.py) ---
TMP = tempfile.mkdtemp(prefix="netops_ui_switch_")
os.environ["NETOPS_DATABASE_URL"] = f"sqlite:///{TMP}/ui_switch_test.db".replace("\\", "/")
os.environ["NETOPS_MOCK_MODE"] = "true"
os.environ["NETOPS_DEV_MODE"] = "true"
os.environ["NETOPS_INTERNAL_SERVICE_TOKEN"] = "test-token"
os.environ["NETOPS_AD_DOMAIN"] = "mock.local"
os.environ.setdefault("NETOPS_BOOTSTRAP_ADMIN", "admin@mock.local")
os.environ.setdefault("NETOPS_JWT_SECRET", "ui-switch-test-secret")
os.environ["NETOPS_LLM_BASE_URL"] = "http://localhost:1234/v1"
os.environ.pop("NETOPS_ZABBIX_URL", None)
os.environ.pop("NETOPS_ZABBIX_TOKEN", None)
os.environ["NETOPS_ZABBIX_URL"] = ""    # перекрывает backend/.env (если есть)
os.environ["NETOPS_ZABBIX_TOKEN"] = ""
# Сценарий 1 начинается со старым UI основным (дефолт config.py)
os.environ["NETOPS_USE_NEW_UI"] = "false"

from fastapi.testclient import TestClient  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.main import app  # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, ok: bool, detail: str = ""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"PASS: {name}")
    else:
        FAIL += 1
        print(f"FAIL: {name} {detail}")


def main():
    # --- Сценарий 1: use_new_ui=false — старый SPA основной ---
    get_settings.cache_clear()  # Settings читается при первом запросе
    with TestClient(app) as client:
        r = client.get("/", follow_redirects=False)
        check("1: GET / -> 303 на /legacy/ (use_new_ui=false)",
              r.status_code == 303 and r.headers.get("location") == "/legacy/",
              f"got {r.status_code}, location={r.headers.get('location')!r}")

        r = client.get("/legacy/", follow_redirects=False)
        check("2: GET /legacy/ -> 200 старый SPA («NetOps LLM»)",
              r.status_code == 200 and "NetOps LLM" in r.text,
              f"got {r.status_code}")

        # git mv не сломал пути: SPA-файл ищется в /legacy/ как есть;
        # несуществующий файл — честный 404 StaticFiles (mount без
        # SPA-fallback), не index.html и не редирект.
        r = client.get("/legacy/favicon.ico")
        check("3: GET /legacy/favicon.ico (нет файла) -> 404 StaticFiles",
              r.status_code == 404,
              f"got {r.status_code}")

    # --- Сценарий 2: use_new_ui=true — / отдаёт лендинг ---
    # home.py читает get_settings() в рантайме: достаточно cache_clear
    # с новым env, TestClient пересоздавать не нужно.
    os.environ["NETOPS_USE_NEW_UI"] = "true"
    get_settings.cache_clear()
    with TestClient(app) as client:
        r = client.get("/", follow_redirects=False)
        check("4: GET / -> 200 лендинг (use_new_ui=true)",
              r.status_code == 200, f"got {r.status_code}")
        check("5: лендинг содержит «Чат»",
              "Чат" in r.text, "нет слова «Чат» в html")
        check("6: лендинг: ссылки /chat, /admin/, /legacy/",
              'href="/chat"' in r.text and 'href="/admin/"' in r.text
              and 'href="/legacy/"' in r.text,
              "нет ссылок на /chat, /admin/ или /legacy/")

        # Старый интерфейс остаётся доступен на /legacy при новом UI
        r = client.get("/legacy/", follow_redirects=False)
        check("7: GET /legacy/ -> 200 и при use_new_ui=true",
              r.status_code == 200 and "NetOps LLM" in r.text,
              f"got {r.status_code}")

    # --- Сценарий 3: git mv frontend/ не сломал админку ---
    # (сбрасываем флаг обратно, хотя логин не зависит от него)
    os.environ["NETOPS_USE_NEW_UI"] = "false"
    get_settings.cache_clear()
    with TestClient(app) as client:
        r = client.get("/admin/login")
        check("8: GET /admin/login -> 200 (mount /legacy не сломал админку)",
              r.status_code == 200, f"got {r.status_code}")

    print(f"\nИтог: PASS={PASS} FAIL={FAIL}")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
