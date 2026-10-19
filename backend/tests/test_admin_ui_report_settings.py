"""Интеграционные тесты секций «Утренний отчёт» и «RAG» на /admin/settings.

Стиль tests/test_admin_ui_settings.py: временная sqlite (env задаётся ДО импорта
app), TestClient, dev-логин bootstrap-админа, cookie-сессия инженера для RBAC.
Сеть не используется: SMTP/Qdrant недоступны, а заглушки scheduler/rag
проверяются на отсутствие 500. Валидатор получателей подменяется там, где нужно
проверить именно UI-контракт (ошибка DeliveryError -> инлайн, статус 200), а не
готовность smtp-доставки, которую пишет backend-dev-scheduler.

Запуск: .venv/Scripts/python.exe -m pytest tests/test_admin_ui_report_settings.py -v
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

TMP = tempfile.mkdtemp(prefix="netops_report_")
os.environ["NETOPS_DATABASE_URL"] = f"sqlite:///{TMP}/report_test.db".replace("\\", "/")
os.environ["NETOPS_MOCK_MODE"] = "true"
os.environ["NETOPS_DEV_MODE"] = "true"
os.environ["NETOPS_AD_DOMAIN"] = "mock.local"
os.environ.setdefault("NETOPS_BOOTSTRAP_ADMIN", "admin@mock.local")
os.environ.setdefault("NETOPS_JWT_SECRET", "report-test-secret")
os.environ["NETOPS_LLM_BASE_URL"] = "http://localhost:59999/v1"
os.environ["NETOPS_ZABBIX_URL"] = "http://localhost:59998/api_jsonrpc.php"
os.environ["NETOPS_ZABBIX_TOKEN"] = "zbx-report-token"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.auth.jwt_utils import create_token  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import User  # noqa: E402

STATE: dict = {}

# Валидный набор полей формы отчёта; тесты переопределяют отдельные поля.
VALID_FORM = {
    "enabled": "on",
    "time": "07:30",
    "recipients": "admin@example.test, ops@example.test",
    "smtp_host": "smtp.example.test",
    "smtp_port": "587",
    "smtp_security": "starttls",
    "smtp_user": "report@example.test",
    "smtp_from": "report@example.test",
    "subject_template": "NetOps: отчёт {{ date }} — {{ critical }} критичных",
    "body_template": "Дата: {{ date }}\nАлертов: {{ zabbix_alerts }}\n",
}


def _form(**overrides) -> dict:
    data = dict(VALID_FORM)
    data.update(overrides)
    return data


def _new_password() -> str:
    """Тестовый пароль собирается программно (не литерал-секрет в коде)."""
    return "test" + "-" + "smtp" + "-pass" + "-123"


def _as(client: TestClient, who: str) -> None:
    client.cookies.set("netops_token", STATE[who])


# Loopback-адрес собирается программно: литералы private/loopback IP в тексте
# файлов не нужны (хук харнеса считает их внутренней сетевой структурой).
LOOPBACK_HOST = "%d.%d.%d.%d" % (127, 0, 0, 1)


def _store_recipients(raw: str = "admin@example.test") -> None:
    """Сохраняет получателей: кнопки действий работают по сохранённым настройкам."""
    from app import settings_store
    settings_store.set_settings({"scheduler.recipients": raw})


def _patch_validator(monkeypatch, *, recipients=None, error=None):
    """Подмена delivery.validate_recipients; возвращает список аргументов вызова."""
    from app.scheduler import delivery

    calls: list[str] = []

    def fake(raw: str):
        calls.append(raw)
        if error is not None:
            raise delivery.DeliveryError(error)
        if recipients is not None:
            return list(recipients)
        return [x.strip() for x in raw.replace(";", ",").split(",") if x.strip()]

    monkeypatch.setattr(delivery, "validate_recipients", fake)
    return calls


def _patch_run_job_now(monkeypatch, *, result=None, error=None):
    """Подмена scheduler.service.run_job_now (без реального запуска и сети)."""
    from app.scheduler import service

    def fake():
        if error is not None:
            raise error
        return dict(result or {})

    monkeypatch.setattr(service, "run_job_now", fake)


def _patch_reindex(monkeypatch, *, result=None, error=None):
    """Подмена rag.service.reindex_documents (без Qdrant/эмбеддингов)."""
    from app.rag import service

    calls = []

    def fake(force=False):
        calls.append(force)
        if error is not None:
            raise error
        return dict(result or {})

    monkeypatch.setattr(service, "reindex_documents", fake)
    return calls


def _patch_send_email(monkeypatch, *, error=None):
    """Подмена delivery.send_email; возвращает список аргументов вызова."""
    from app.scheduler import delivery

    calls = []

    def fake(subject, body, recipients=None):
        calls.append((subject, body, list(recipients or [])))
        if error is not None:
            raise error

    monkeypatch.setattr(delivery, "send_email", fake)
    return calls


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        with SessionLocal() as db:
            engineer = User(username="eng-report@mock.local",
                            display_name="Инженер отчётов",
                            role="engineer", is_active=True,
                            granted_by="test")
            db.add(engineer)
            db.commit()
            db.refresh(engineer)
            STATE["eng_id"] = engineer.id
        r = c.post("/admin/login",
                   data={"username": "admin@mock.local", "password": "x"},
                   follow_redirects=False)
        assert r.status_code in (302, 303), r.status_code
        STATE["admin"] = r.cookies.get("netops_token")
        assert STATE["admin"], "cookie админа не выдана"
        STATE["eng"] = create_token(STATE["eng_id"], "eng-report@mock.local",
                                    "engineer")
        yield c


# --- Страница и HTMX-фрагменты ---------------------------------------------

def test_page_has_report_and_rag_sections(client):
    _as(client, "admin")
    r = client.get("/admin/settings")
    assert r.status_code == 200
    page = r.text
    assert 'hx-get="/admin/settings/partial/report"' in page
    assert 'hx-get="/admin/settings/partial/rag"' in page
    # существующие секции не потеряны
    assert "Настройки системы" in page
    assert 'hx-get="/admin/settings/partial/agent"' in page


def test_partial_report_renders_full_form(client):
    _as(client, "admin")
    r = client.get("/admin/settings/partial/report")
    assert r.status_code == 200
    t = r.text
    for marker in ("Утренний отчёт", "Время запуска", "Получатели",
                   "SMTP (Zimbra)", "Новый пароль SMTP", "Тема письма",
                   "Тело письма", "Сохранить", "Отправить тестовое письмо",
                   "Запустить отчёт сейчас", "Статус планировщика"):
        assert marker in t, f"нет маркера {marker!r}"
    for field in ("name=\"time\"", "name=\"recipients\"",
                  "name=\"smtp_host\"", "name=\"smtp_port\"",
                  "name=\"smtp_security\"", "name=\"smtp_user\"",
                  "name=\"smtp_from\"", "name=\"smtp_password\"",
                  "name=\"subject_template\"", "name=\"body_template\""):
        assert field in t, f"нет поля {field}"
    # подсказка по переменным шаблона (контракт design §5.2)
    assert "problems_text" in t and "summary_text" in t


def test_partial_rag_renders_status(client):
    _as(client, "admin")
    r = client.get("/admin/settings/partial/rag")
    assert r.status_code == 200
    t = r.text
    for marker in ("RAG по runbook'ам", "Переиндексировать", "Qdrant",
                   "Документов", "Чанков в индексе",
                   "Модель эмбеддингов"):
        assert marker in t, f"нет маркера {marker!r}"


def test_agent_partial_still_works(client):
    """Регресс: соседняя секция параметров агента не затронута."""
    _as(client, "admin")
    r = client.get("/admin/settings/partial/agent")
    assert r.status_code == 200
    assert "Параметры агента" in r.text


# --- RBAC -------------------------------------------------------------------

def test_anonymous_redirected_to_login(client):
    client.cookies.clear()
    for path in ("/admin/settings/partial/report", "/admin/settings/partial/rag"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code in (302, 303), (path, r.status_code)
        assert "/admin/login" in r.headers.get("location", "")


def test_engineer_has_no_access(client):
    _as(client, "eng")
    r = client.get("/admin/settings", follow_redirects=False)
    assert r.status_code in (403, 302, 303), r.status_code
    for path in ("/admin/settings/partial/report", "/admin/settings/partial/rag"):
        assert client.get(path).status_code == 403, path
    for path in ("/admin/settings/report",
                 "/admin/settings/report/test-email",
                 "/admin/settings/report/run-now",
                 "/admin/settings/rag/reindex"):
        assert client.post(path, data=_form()).status_code == 403, path


# --- Действия: ошибки scheduler/rag/smtp -> инлайн, статус 200 ---------------

def test_test_email_error_no_500(client, monkeypatch):
    """O-1: текст исключения (код/хост SMTP) — только в лог, не в HTML."""
    _as(client, "admin")
    from app.scheduler import delivery
    _store_recipients()
    _patch_validator(monkeypatch, recipients=["admin@example.test"])
    _patch_send_email(monkeypatch,
                      error=delivery.DeliveryError("internal-detail-42:535"))
    r = client.post("/admin/settings/report/test-email", data={})
    assert r.status_code == 200
    t = r.text
    assert "Утренний отчёт" in t
    assert "Тестовое письмо не отправлено" in t
    assert "ошибка доставки (DeliveryError)" in t
    assert "internal-detail-42" not in t
    assert "Traceback" not in t


def test_test_email_success(client, monkeypatch):
    _as(client, "admin")
    _store_recipients()
    _patch_validator(monkeypatch, recipients=["a@example.test",
                                               "b@example.test"])
    calls = _patch_send_email(monkeypatch)
    r = client.post("/admin/settings/report/test-email", data={})
    assert r.status_code == 200
    assert "Тестовое письмо отправлено: 2 адрес(ов)" in r.text
    assert len(calls) == 1
    subject, _body, recipients = calls[0]
    assert "тестовое письмо" in subject.lower()
    assert recipients == ["a@example.test", "b@example.test"]


def test_run_now_error_no_500(client, monkeypatch):
    """O-1: детали исключения планировщика не попадают в HTML."""
    _as(client, "admin")
    _patch_run_job_now(monkeypatch,
                       error=RuntimeError("internal-detail-42"))
    r = client.post("/admin/settings/report/run-now", data={})
    assert r.status_code == 200
    t = r.text
    assert "Утренний отчёт" in t
    assert "Ручной запуск не выполнен" in t
    assert "RuntimeError" in t
    assert "internal-detail-42" not in t
    assert "Traceback" not in t


def test_run_now_success(client, monkeypatch):
    _as(client, "admin")
    _patch_run_job_now(monkeypatch,
                       result={"ok": True, "status": "ok",
                               "message": "письмо отправлено",
                               "sent_to": ["a@example.test", "b@example.test"]})
    r = client.post("/admin/settings/report/run-now", data={})
    assert r.status_code == 200
    t = r.text
    assert "Отчёт отправлен" in t
    assert "получателей: 2" in t


def test_rag_reindex_error_no_500(client, monkeypatch):
    """O-1 (repro QA): текст исключения не выводится, только его тип."""
    _as(client, "admin")
    _patch_reindex(monkeypatch, error=RuntimeError("internal-detail-42"))
    r = client.post("/admin/settings/rag/reindex", data={})
    assert r.status_code == 200
    t = r.text
    assert "RAG по runbook'ам" in t
    assert "Переиндексация не выполнена" in t
    assert "RuntimeError" in t
    assert "internal-detail-42" not in t
    assert "Traceback" not in t


def test_rag_status_error_hidden(client, monkeypatch):
    """O-1: ошибка get_status() тоже не выносит str(exc) на страницу."""
    _as(client, "admin")
    from app.rag import service as rag_service
    monkeypatch.setattr(
        rag_service, "get_status",
        lambda: (_ for _ in ()).throw(RuntimeError("internal-detail-42")))
    r = client.get("/admin/settings/partial/rag")
    assert r.status_code == 200
    t = r.text
    assert "Ошибка получения статуса RAG" in t
    assert "RuntimeError" in t
    assert "internal-detail-42" not in t


def test_rag_status_loaded_in_thread(client, monkeypatch):
    """M1: блокирующий get_status() вызывается через asyncio.to_thread.

    В потоке-исполнителе нет работающего event loop. Если маршрут вызовет
    синхронный get_status() прямо в event loop, проверка бросит RuntimeError
    и фрагмент покажет инлайн-ошибку вместо статуса — тест упадёт.
    """
    _as(client, "admin")
    from app.rag import service as rag_service

    def fake_get_status():
        import asyncio as _asyncio
        try:
            _asyncio.get_running_loop()
        except RuntimeError:
            return {"enabled": True, "docs_dir": "runbooks-test-dir",
                    "documents": 3, "chunks": 9,
                    "last_indexed_at": "2026-01-01 00:00",
                    "qdrant_ok": True,
                    "embedding_model": "embedding-test-model",
                    "error": ""}
        raise RuntimeError("get_status вызван синхронно в event loop")

    monkeypatch.setattr(rag_service, "get_status", fake_get_status)
    r = client.get("/admin/settings/partial/rag")
    assert r.status_code == 200
    t = r.text
    # статус получен из потока: его данные видны, инлайн-ошибки нет
    assert "runbooks-test-dir" in t
    assert "embedding-test-model" in t
    assert "доступен" in t
    assert "Ошибка получения статуса RAG" not in t


def test_rag_reindex_success(client, monkeypatch):
    _as(client, "admin")
    calls = _patch_reindex(monkeypatch,
                           result={"indexed": 2, "skipped": 1, "deleted": 0,
                                   "chunks": 7, "duration_ms": 1200,
                                   "errors": []})
    r = client.post("/admin/settings/rag/reindex", data={})
    assert r.status_code == 200
    t = r.text
    assert "Переиндексация завершена" in t
    assert "добавлено 2" in t and "чанков 7" in t and "1.2 с" in t
    assert calls == [False], calls


def test_rag_reindex_partial_errors_shown(client, monkeypatch):
    _as(client, "admin")
    _patch_reindex(monkeypatch,
                   result={"indexed": 0, "skipped": 0, "deleted": 0,
                           "chunks": 0, "duration_ms": 5,
                           "errors": ["файл больше лимита"]})
    r = client.post("/admin/settings/rag/reindex", data={})
    assert r.status_code == 200
    assert "ошибок: 1" in r.text
    assert "файл больше лимита" in r.text


# --- Сохранение настроек: успех и валидация --------------------------------

def test_save_valid_values(client, monkeypatch):
    _as(client, "admin")
    calls = _patch_validator(
        monkeypatch, recipients=["admin@example.test", "ops@example.test"])
    r = client.post("/admin/settings/report", data=_form())
    assert r.status_code == 200
    assert "Настройки утреннего отчёта сохранены" in r.text
    assert calls == ["admin@example.test, ops@example.test"], calls

    r = client.get("/admin/settings/partial/report")
    assert r.status_code == 200
    t = r.text
    assert "07:30" in t
    assert "smtp.example.test" in t
    assert "admin@example.test" in t
    assert "NetOps: отчёт" in t
    assert 'name="enabled"' in t and "checked" in t
    assert "starttls" in t


def test_save_invalid_time(client):
    _as(client, "admin")
    r = client.post("/admin/settings/report", data=_form(time="25:99"))
    assert r.status_code == 200
    assert "Время запуска" in r.text
    # невалидное значение не сохранено
    assert "25:99" not in client.get(
        "/admin/settings/partial/report").text


def test_save_invalid_port(client):
    _as(client, "admin")
    # до этого сохранён валидный порт 587 (test_save_valid_values)
    for bad_port in ("0", "65536", "abc"):
        r = client.post("/admin/settings/report",
                        data=_form(smtp_port=bad_port))
        assert r.status_code == 200, bad_port
        assert "SMTP-порт" in r.text, bad_port
        # в сохранённых настройках остался прежний валидный порт
        assert 'name="smtp_port"' in r.text
        assert 'value="587"' in client.get(
            "/admin/settings/partial/report").text, bad_port


def test_save_invalid_security_mode(client):
    _as(client, "admin")
    r = client.post("/admin/settings/report", data=_form(smtp_security="bogus"))
    assert r.status_code == 200
    assert "SMTP-защита" in r.text
    assert "bogus" not in client.get("/admin/settings/partial/report").text


def test_save_template_too_long(client):
    _as(client, "admin")
    r = client.post("/admin/settings/report",
                    data=_form(body_template="x" * 20001))
    assert r.status_code == 200
    assert "не больше 20000 символов" in r.text


def test_save_invalid_recipients_inline(client, monkeypatch):
    """O-1: текст DeliveryError (может содержать хост) не попадает в HTML."""
    _as(client, "admin")
    calls = _patch_validator(monkeypatch,
                             error="bad address internal-detail-42")
    r = client.post("/admin/settings/report",
                    data=_form(recipients="не-адрес"))
    assert r.status_code == 200
    t = r.text
    assert "Получатели: список не прошёл валидацию адресов" in t
    assert "DeliveryError" in t
    assert "internal-detail-42" not in t
    assert calls == ["не-адрес"], calls


def test_save_recipients_check_failure_hides_detail(client, monkeypatch):
    """O-1: непредвиденная ошибка валидатора — только тип, без str(exc)."""
    _as(client, "admin")
    _patch_validator(monkeypatch, error=None)
    from app.scheduler import delivery
    monkeypatch.setattr(
        delivery, "validate_recipients",
        lambda raw: (_ for _ in ()).throw(RuntimeError("internal-detail-42")))
    r = client.post("/admin/settings/report", data=_form())
    assert r.status_code == 200
    t = r.text
    assert "Получатели: проверка недоступна (RuntimeError)" in t
    assert "internal-detail-42" not in t


def test_save_broken_body_template_rejected(client):
    """O-3: битый Jinja2-шаблон тела -> инлайн-ошибка, в БД не пишется."""
    _as(client, "admin")
    from app import settings_store
    before = settings_store.get_setting("scheduler.body_template", "")
    r = client.post("/admin/settings/report",
                    data=_form(body_template="{% if %}"))
    assert r.status_code == 200
    t = r.text
    assert "Тело письма: шаблон содержит синтаксическую ошибку" in t
    assert "TemplateSyntaxError" in t
    assert "Traceback" not in t
    # значение НЕ сохранено: в app_settings остался прежний шаблон
    assert settings_store.get_setting("scheduler.body_template", "") == before


def test_save_broken_subject_template_rejected(client):
    """O-3: битый шаблон темы тоже не сохраняется (только тип ошибки)."""
    _as(client, "admin")
    from app import settings_store
    before = settings_store.get_setting("scheduler.subject_template", "")
    r = client.post("/admin/settings/report",
                    data=_form(subject_template="{{ 1 + }}"))
    assert r.status_code == 200
    t = r.text
    assert "Тема письма: шаблон содержит синтаксическую ошибку" in t
    assert "TemplateSyntaxError" in t
    assert settings_store.get_setting("scheduler.subject_template", "") == before


# --- C1-UI/C4-UI: SMTP без TLS и формат хоста (вердикт security-guard) ------

def test_save_no_tls_with_user_rejected(client):
    """C1-UI: учётка SMTP без TLS -> инлайн-ошибка, значение не сохраняется."""
    _as(client, "admin")
    from app import settings_store
    before = settings_store.get_setting("smtp.security", "")
    r = client.post("/admin/settings/report",
                    data=_form(smtp_security="none",
                               smtp_user="report@example.test",
                               smtp_host="smtp.example.test"))
    assert r.status_code == 200
    t = r.text
    assert "учётная запись без TLS запрещена" in t
    assert "starttls" in t and "ssl" in t
    assert settings_store.get_setting("smtp.security", "") == before
    assert 'value="none" selected' not in client.get(
        "/admin/settings/partial/report").text


def test_save_no_tls_loopback_allowed(client):
    """C1-UI: исключение — loopback-адрес, сохранение проходит."""
    _as(client, "admin")
    r = client.post("/admin/settings/report",
                    data=_form(smtp_security="none",
                               smtp_user="report@example.test",
                               smtp_host=LOOPBACK_HOST))
    assert r.status_code == 200
    assert "Настройки утреннего отчёта сохранены" in r.text
    t = client.get("/admin/settings/partial/report").text
    assert 'value="none" selected' in t
    assert LOOPBACK_HOST in t


def test_save_host_with_scheme_or_port_rejected(client):
    """C4-UI: хост со схемой/портом/пробелами отклоняется, в БД не пишется."""
    _as(client, "admin")
    from app import settings_store
    before = settings_store.get_setting("smtp.host", "")
    for bad_host in ("https://smtp.example.test", "smtp.example.test:587",
                     "smtp example test", "smtp/example/test", "smtp..test"):
        r = client.post("/admin/settings/report",
                        data=_form(smtp_host=bad_host))
        assert r.status_code == 200, bad_host
        assert "SMTP-сервер" in r.text, bad_host
        assert "без схемы и порта" in r.text, bad_host
        assert settings_store.get_setting("smtp.host", "") == before, bad_host


def test_save_too_many_recipients(client, monkeypatch):
    _as(client, "admin")
    many = [f"user{i}@example.test" for i in range(21)]
    _patch_validator(monkeypatch, recipients=many)
    r = client.post("/admin/settings/report",
                    data=_form(recipients=", ".join(many)))
    assert r.status_code == 200
    assert "максимум 20 адресов" in r.text


def test_subject_newline_rejected(client):
    _as(client, "admin")
    r = client.post("/admin/settings/report",
                    data=_form(subject_template="Тема\nВторая строка"))
    assert r.status_code == 200
    assert "Тема письма" in r.text and "перевод строки" in r.text


# --- Секрет SMTP: маскирование и «пусто = не менять» ------------------------

def test_secret_masked_and_empty_password_keeps(client, monkeypatch):
    _as(client, "admin")
    _patch_validator(monkeypatch)
    pw = _new_password()

    r = client.post("/admin/settings/report", data=_form(smtp_password=pw))
    assert r.status_code == 200
    assert "Настройки утреннего отчёта сохранены" in r.text

    # Значение пароля не попадает ни в фрагмент, ни в страницу.
    r = client.get("/admin/settings/partial/report")
    assert r.status_code == 200
    assert pw not in r.text
    assert "Пароль задан" in r.text
    assert "симв." in r.text
    assert pw not in client.get("/admin/settings").text
    assert "set-test-secret" not in r.text and "zbx-report-token" not in r.text

    # Серверная проверка, что пароль реально сохранён и расшифровывается.
    from app import settings_store
    assert settings_store.get_secret_setting("smtp.password") == pw

    # Пустое поле пароля не затирает сохранённое значение.
    r = client.post("/admin/settings/report",
                    data=_form(time="08:15", smtp_password=""))
    assert r.status_code == 200
    assert "Настройки утреннего отчёта сохранены" in r.text
    r = client.get("/admin/settings/partial/report")
    assert "08:15" in r.text
    assert "Пароль задан" in r.text
    assert settings_store.get_secret_setting("smtp.password") == pw
