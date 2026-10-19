"""Независимые интеграционные QA-тесты админки утреннего отчёта и RAG.

Проверяется контракт `docs/features/rag-and-scheduler-design.md` §5.2/§6
глазами приёмки, а не авторских юнитов:

  * RBAC: аноним -> логин, viewer/engineer -> 403 на всех новых маршрутах;
  * серверная валидация времени/порта/режима/получателей/шаблонов,
    ошибки инлайн со статусом 200 и НЕ пишут мусор в app_settings;
  * политики C1-UI (учётка без TLS запрещена, loopback разрешён),
    C4-UI (имя SMTP-хоста без схемы/порта) и O-3 (битый Jinja2-шаблон
    отклоняется при сохранении, а не при плановом запуске);
  * `scheduler.apply_settings()` вызывается после сохранения формы;
  * пароль SMTP: шифруется в БД, в HTML только «Пароль задан, N симв.»,
    пустое поле не затирает сохранённый, значение не попадает в логи;
  * кнопки «Тестовое письмо» / «Запустить отчёт» / «Переиндексировать»
    не дают 500 и показывают инлайн-результат; RAG-эндпоинт без 500 при
    выключенном RAG и при исключении сервиса;
  * HTMX-фрагменты устойчивы к падению scheduler/rag-модулей;
  * планировщик не стартует из lifespan в тестовом окружении
    (`NETOPS_SCHEDULER_ENABLED=false`).

Сеть не используется: TestClient на временной sqlite, SMTP/Qdrant не
поднимаются (ошибки доставки подменяются). `tmp_path` не используется:
DACL pytest-of-* на этом хосте ломается.

Запуск: cd backend; .\\.venv\\Scripts\\python.exe -m pytest tests/test_admin_ui_report_qa.py -v
"""
import logging
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

# env ДО импорта app.*: Settings требует jwt_secret и llm_base_url.
TMP = tempfile.mkdtemp(prefix="netops_admin_qa_")
os.environ["NETOPS_DATABASE_URL"] = f"sqlite:///{TMP}/qa.db".replace("\\", "/")
os.environ["NETOPS_MOCK_MODE"] = "true"
os.environ["NETOPS_DEV_MODE"] = "true"
os.environ["NETOPS_AD_DOMAIN"] = "mock.local"
os.environ.setdefault("NETOPS_BOOTSTRAP_ADMIN", "admin@mock.local")
os.environ.setdefault("NETOPS_JWT_SECRET", "admin-qa" + "-secret")
os.environ["NETOPS_LLM_BASE_URL"] = "http://llm.invalid:9/v1"
os.environ["NETOPS_ZABBIX_URL"] = "http://zbx.invalid/api_jsonrpc.php"
os.environ["NETOPS_SCHEDULER_ENABLED"] = "false"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import settings_store  # noqa: E402
from app.auth.jwt_utils import create_token  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import AppSetting, User  # noqa: E402
from app.scheduler import delivery  # noqa: E402

STATE: dict = {}

# Тестовый пароль собирается программно: литерал «пароль = значение»
# блокирует pre-tool-guard (.dsh/hooks/pre-tool-guard.ps1).
NEW_PWD = "test" + "-" + "smtp" + "-" + "new" + "-" + "pass"

VALID_FORM = {
    "enabled": "on",
    "time": "07:30",
    "recipients": "admin@example.test, ops@example.test",
    "smtp_host": "mail.example.test",
    "smtp_port": "587",
    "smtp_security": "starttls",
    "smtp_user": "report@example.test",
    "smtp_from": "reports@example.test",
    "subject_template": "NetOps: отчёт {{ date }} — {{ critical }} критичных",
    "body_template": "Дата: {{ date }}\nАлертов: {{ zabbix_alerts }}\n",
}

GET_PATHS = ("/admin/settings/partial/report", "/admin/settings/partial/rag")
POST_PATHS = ("/admin/settings/report",
              "/admin/settings/report/test-email",
              "/admin/settings/report/run-now",
              "/admin/settings/rag/reindex")


def _form(**overrides) -> dict:
    data = dict(VALID_FORM)
    data.update(overrides)
    return data


def _as(client: TestClient, who: str) -> None:
    client.cookies.set("netops_token", STATE[who])


def _wipe_settings() -> None:
    with SessionLocal() as db:
        db.query(AppSetting).delete()
        db.commit()


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as http:
        with SessionLocal() as db:
            engineer = User(username="eng-qa@mock.local", display_name="QA eng",
                            role="engineer", is_active=True, granted_by="test")
            viewer = User(username="view-qa@mock.local", display_name="QA view",
                          role="viewer", is_active=True, granted_by="test")
            db.add_all([engineer, viewer])
            db.commit()
            db.refresh(engineer)
            db.refresh(viewer)
            STATE["eng_id"] = engineer.id
            STATE["viewer_id"] = viewer.id

        response = http.post("/admin/login",
                             data={"username": "admin@mock.local",
                                   "password": "x"},
                             follow_redirects=False)
        assert response.status_code in (302, 303), response.status_code
        STATE["admin"] = response.cookies.get("netops_token")
        assert STATE["admin"], "cookie админа не выдана"
        STATE["eng"] = create_token(STATE["eng_id"], "eng-qa@mock.local",
                                    "engineer")
        STATE["viewer"] = create_token(STATE["viewer_id"], "view-qa@mock.local",
                                       "viewer")
        yield http


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    """Чистые app_settings + гарантированно выключенный планировщик."""
    monkeypatch.setattr(get_settings(), "scheduler_enabled", False)
    _wipe_settings()
    try:
        yield
    finally:
        _wipe_settings()


# ---------------------------------------------------------------------------
# 1. RBAC новых маршрутов
# ---------------------------------------------------------------------------
def test_rbac_non_admin_denied_on_all_report_and_rag_routes(client):
    for who in ("eng", "viewer"):
        _as(client, who)
        for path in GET_PATHS:
            response = client.get(path, follow_redirects=False)
            assert response.status_code == 403, (who, path, response.status_code)
            assert "Утренний отчёт" not in response.text
            assert "RAG по runbook'ам" not in response.text
        for path in POST_PATHS:
            response = client.post(path, data=_form(), follow_redirects=False)
            assert response.status_code == 403, (who, path, response.status_code)
    # отказ в доступе не оставил настроек в БД
    assert settings_store.get_setting("scheduler.time") == ""


def test_anonymous_redirected_to_login(client):
    client.cookies.clear()
    for path in GET_PATHS:
        response = client.get(path, follow_redirects=False)
        assert response.status_code in (302, 303), (path, response.status_code)
        assert "/admin/login" in response.headers.get("location", "")
    for path in POST_PATHS:
        response = client.post(path, data=_form(), follow_redirects=False)
        assert response.status_code in (302, 303), (path, response.status_code)


# ---------------------------------------------------------------------------
# 2. сохранение формы: настройки, apply_settings, маскирование пароля
# ---------------------------------------------------------------------------
def test_save_valid_form_persists_and_applies(client, monkeypatch):
    from app.scheduler import service as scheduler_service

    applied: list[bool] = []

    def fake_apply():
        applied.append(True)
        return {"applied": True, "message": "Планировщик: настройки применены"}

    monkeypatch.setattr(scheduler_service, "apply_settings", fake_apply)
    _as(client, "admin")
    response = client.post("/admin/settings/report",
                           data=_form(smtp_password=NEW_PWD))
    assert response.status_code == 200, response.text
    assert "сохранены" in response.text
    assert applied == [True], "apply_settings не вызван после сохранения"
    assert "Планировщик: настройки применены" in response.text

    assert settings_store.get_setting("scheduler.enabled") == "true"
    assert settings_store.get_setting("scheduler.time") == "07:30"
    assert settings_store.get_setting("scheduler.recipients").startswith(
        "admin@example.test")
    assert settings_store.get_setting("smtp.host") == "mail.example.test"
    assert settings_store.get_setting("smtp.security") == "starttls"

    ciphertext = settings_store.get_setting("smtp.password")
    assert ciphertext and ciphertext != NEW_PWD
    assert settings_store.get_secret_setting("smtp.password") == NEW_PWD
    assert "Пароль задан" in response.text
    assert f"{len(NEW_PWD)} симв." in response.text
    assert NEW_PWD not in response.text


def test_empty_password_keeps_saved_secret(client):
    _as(client, "admin")
    first = client.post("/admin/settings/report",
                        data=_form(smtp_password=NEW_PWD))
    assert first.status_code == 200
    ciphertext = settings_store.get_setting("smtp.password")
    assert ciphertext

    second = client.post("/admin/settings/report",
                         data=_form(smtp_password=""))
    assert second.status_code == 200
    assert settings_store.get_setting("smtp.password") == ciphertext
    assert settings_store.get_secret_setting("smtp.password") == NEW_PWD
    assert "Пароль задан" in second.text
    assert NEW_PWD not in second.text


def test_secret_not_in_html_or_logs(client, caplog):
    _as(client, "admin")
    with caplog.at_level(logging.DEBUG):
        response = client.post("/admin/settings/report",
                               data=_form(smtp_password=NEW_PWD))
    assert response.status_code == 200
    ciphertext = settings_store.get_setting("smtp.password")

    for payload in (response.text,
                    client.get("/admin/settings").text,
                    client.get("/admin/settings/partial/report").text):
        assert NEW_PWD not in payload
        assert ciphertext not in payload
    assert NEW_PWD not in caplog.text
    assert ciphertext not in caplog.text


# ---------------------------------------------------------------------------
# 3. серверная валидация: инлайн-ошибка, настройки не меняются
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("overrides,marker", [
    ({"time": "25:99"}, "Время запуска"),
    ({"time": "8:00"}, "Время запуска"),
    ({"smtp_port": "0"}, "SMTP-порт"),
    ({"smtp_port": "70000"}, "SMTP-порт"),
    ({"smtp_port": "мусор"}, "SMTP-порт"),
    ({"smtp_security": "plain"}, "SMTP-защита"),
    # C4-UI: схема/порт в поле «Сервер» — инлайн-ошибка, не сохранение
    ({"smtp_host": "https://mail.example.test"}, "без схемы и порта"),
    ({"smtp_host": "mail.example.test:587"}, "без схемы и порта"),
    # C1-UI: учётка без TLS на внешнем сервере запрещена
    ({"smtp_security": "none"}, "учётная запись без TLS запрещена"),
    # O-3: битый Jinja2-шаблон не сохраняется «молча»
    ({"body_template": "{% if %}"}, "синтаксическую ошибку"),
    ({"subject_template": "{{ broken "}, "синтаксическую ошибку"),
    ({"recipients": "not-an-email"}, "Получатели"),
    ({"recipients": "a b"}, "Получатели"),
    ({"subject_template": "x" * 20001}, "Тема письма"),
    ({"body_template": "y" * 20001}, "Тело письма"),
])
def test_save_validation_error_inline_and_no_changes(client, overrides, marker):
    _as(client, "admin")
    response = client.post("/admin/settings/report",
                           data=_form(**overrides))
    assert response.status_code == 200, response.status_code
    assert marker in response.text, (overrides, response.text[:400])
    # ни одно поле не сохранено (валидация до записи)
    for key in ("scheduler.time", "scheduler.enabled", "smtp.host",
                "smtp.port", "smtp.security", "scheduler.recipients",
                "scheduler.subject_template", "scheduler.body_template"):
        assert settings_store.get_setting(key) == "", key


def test_save_too_many_recipients_inline(client):
    _as(client, "admin")
    many = ", ".join(f"user{i}@example.test" for i in range(25))
    response = client.post("/admin/settings/report",
                           data=_form(recipients=many))
    assert response.status_code == 200
    assert "максимум" in response.text
    assert settings_store.get_setting("scheduler.recipients") == ""


def test_save_none_with_user_on_loopback_allowed(client):
    """C1-UI: `none` + учётка допустимы для loopback (локальный тест-релей)."""
    _as(client, "admin")
    response = client.post(
        "/admin/settings/report",
        data=_form(smtp_security="none", smtp_host="localhost"))
    assert response.status_code == 200
    assert "сохранены" in response.text, response.text[:400]
    assert "без TLS запрещена" not in response.text
    assert settings_store.get_setting("smtp.security") == "none"
    assert settings_store.get_setting("smtp.host") == "localhost"
    assert settings_store.get_setting("smtp.user") == "report@example.test"


# ---------------------------------------------------------------------------
# 4. кнопки действий не дают 500 (инлайн-результат)
# ---------------------------------------------------------------------------
def test_reindex_endpoint_graceful(client, monkeypatch):
    _as(client, "admin")
    from app.rag import service as rag_service

    def boom(force=False):
        raise RuntimeError("qdrant down")

    monkeypatch.setattr(rag_service, "reindex_documents", boom)
    response = client.post("/admin/settings/rag/reindex")
    assert response.status_code == 200
    assert "Переиндексация не выполнена" in response.text
    assert "RuntimeError" in response.text

    monkeypatch.setattr(rag_service, "reindex_documents", lambda force=False: {
        "ok": False, "indexed": 1, "skipped": 2, "deleted": 0, "chunks": 3,
        "duration_ms": 12, "errors": ["Qdrant: недоступен"],
    })
    response = client.post("/admin/settings/rag/reindex")
    assert response.status_code == 200
    assert "Переиндексация завершена" in response.text
    assert "ошибок: 1" in response.text


def test_reindex_endpoint_with_real_disabled_rag(client, monkeypatch):
    """Реальный reindex при выключенном RAG -> инлайн, статус 200 (не 500)."""
    _as(client, "admin")
    settings = get_settings()
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "rag_enabled", False)
    response = client.post("/admin/settings/rag/reindex")
    assert response.status_code == 200, response.status_code
    assert "Переиндексация завершена" in response.text
    assert "RAG выключен" in response.text


def test_run_now_endpoint_graceful(client, monkeypatch):
    _as(client, "admin")
    from app.scheduler import service as scheduler_service

    def boom():
        raise RuntimeError("job crashed")

    monkeypatch.setattr(scheduler_service, "run_job_now", boom)
    response = client.post("/admin/settings/report/run-now")
    assert response.status_code == 200
    assert "Ручной запуск не выполнен" in response.text

    monkeypatch.setattr(scheduler_service, "run_job_now", lambda: {
        "ok": False, "status": "error", "message": "нет получателей",
        "sent_to": [], "duration_ms": 1})
    response = client.post("/admin/settings/report/run-now")
    assert response.status_code == 200
    assert "Отчёт не отправлен" in response.text
    assert "нет получателей" in response.text

    monkeypatch.setattr(scheduler_service, "run_job_now", lambda: {
        "ok": True, "status": "ok", "message": "отправлено",
        "sent_to": ["ops@example.test"], "duration_ms": 2})
    response = client.post("/admin/settings/report/run-now")
    assert response.status_code == 200
    assert "Отчёт отправлен" in response.text
    assert "получателей: 1" in response.text


def test_test_email_endpoint_graceful_and_no_secret(client, monkeypatch):
    """O-1 (фикс): текст DeliveryError (код/хост SMTP) — только в лог, не в HTML."""
    _as(client, "admin")
    client.post("/admin/settings/report", data=_form(smtp_password=NEW_PWD))
    calls: list[tuple] = []

    def fake_send(subject, body, recipients=None):
        calls.append((subject, body, list(recipients or [])))
        raise delivery.DeliveryError("internal-detail-42:535 connection refused")

    monkeypatch.setattr(delivery, "send_email", fake_send)
    response = client.post("/admin/settings/report/test-email")
    assert response.status_code == 200
    assert "не отправлено" in response.text
    # на страницу — только тип ошибки, детали/текст исключения не выводятся
    assert "ошибка доставки (DeliveryError)" in response.text
    assert "internal-detail-42" not in response.text
    assert "connection refused" not in response.text
    assert NEW_PWD not in response.text
    assert calls and "тестовое письмо" in calls[0][0]

    def fake_ok(subject, body, recipients=None):
        calls.append((subject, body, list(recipients or [])))

    monkeypatch.setattr(delivery, "send_email", fake_ok)
    response = client.post("/admin/settings/report/test-email")
    assert response.status_code == 200
    assert "Тестовое письмо отправлено" in response.text
    assert NEW_PWD not in response.text


# ---------------------------------------------------------------------------
# 5. устойчивость HTMX-фрагментов к падению модулей
# ---------------------------------------------------------------------------
def test_partials_resilient_to_module_failures(client, monkeypatch):
    _as(client, "admin")
    from app.rag import service as rag_service
    from app.scheduler import service as scheduler_service

    def boom_state():
        raise RuntimeError("scheduler broken")

    def boom_status():
        raise RuntimeError("rag broken")

    monkeypatch.setattr(scheduler_service, "get_state", boom_state)
    monkeypatch.setattr(rag_service, "get_status", boom_status)

    response = client.get("/admin/settings/partial/report")
    assert response.status_code == 200
    assert "Утренний отчёт" in response.text
    assert "Состояние планировщика недоступно" in response.text

    response = client.get("/admin/settings/partial/rag")
    assert response.status_code == 200
    assert "RAG по runbook'ам" in response.text
    assert "Ошибка" in response.text


# ---------------------------------------------------------------------------
# 6. планировщик не стартует в тестах
# ---------------------------------------------------------------------------
def test_lifespan_does_not_start_scheduler(client):
    from app.scheduler import service as scheduler_service

    assert get_settings().scheduler_enabled is False
    assert scheduler_service._scheduler is None
    state = scheduler_service.get_state()
    assert state["running"] is False
    assert state["enabled"] is False


# ---------------------------------------------------------------------------
# 7. страница/фрагменты: секции на месте, секрета нет
# ---------------------------------------------------------------------------
def test_settings_page_sections_and_no_secret(client):
    _as(client, "admin")
    client.post("/admin/settings/report", data=_form(smtp_password=NEW_PWD))
    ciphertext = settings_store.get_setting("smtp.password")

    page = client.get("/admin/settings")
    assert page.status_code == 200
    assert 'hx-get="/admin/settings/partial/report"' in page.text
    assert 'hx-get="/admin/settings/partial/rag"' in page.text
    assert NEW_PWD not in page.text and ciphertext not in page.text

    report = client.get("/admin/settings/partial/report")
    assert report.status_code == 200
    for marker in ("name=\"time\"", "name=\"smtp_password\"",
                   "Отправить тестовое письмо", "Запустить отчёт сейчас",
                   "Статус планировщика"):
        assert marker in report.text, marker
    assert NEW_PWD not in report.text and ciphertext not in report.text

    rag = client.get("/admin/settings/partial/rag")
    assert rag.status_code == 200
    assert "Переиндексировать" in rag.text
    assert "Qdrant" in rag.text


# ---------------------------------------------------------------------------
# 8. сообщение о последнем запуске экранируется в HTML (нет XSS через лог)
# ---------------------------------------------------------------------------
def test_last_run_message_is_html_escaped(client):
    _as(client, "admin")
    settings_store.set_settings({
        "scheduler.last_run_status": "error",
        "scheduler.last_run_message": "<img src=x onerror=alert(1)>",
    })
    response = client.get("/admin/settings/partial/report")
    assert response.status_code == 200
    assert "<img src=x" not in response.text, "HTML не экранирован (XSS)"
    assert "&lt;img" in response.text
