"""Интеграционный тест страницы диалогов /admin/conversations (Фаза 9).

Стиль test_admin_ui_audit.py: TestClient на временной sqlite (env задаётся
ДО импорта app), dev-логин bootstrap-админа, данные — напрямую в БД.
Рабочая netops.db не затрагивается.

Отличие от аудита: страница доступна admin+engineer (не только admin).

Запуск: .venv/Scripts/python.exe tests/test_admin_ui_conversations.py  (из backend/)
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

TMP = tempfile.mkdtemp(prefix="netops_conv_")
os.environ["NETOPS_DATABASE_URL"] = f"sqlite:///{TMP}/conv_test.db".replace("\\", "/")
os.environ["NETOPS_MOCK_MODE"] = "true"
os.environ["NETOPS_DEV_MODE"] = "true"
os.environ["NETOPS_AD_DOMAIN"] = "mock.local"
os.environ.setdefault("NETOPS_BOOTSTRAP_ADMIN", "admin@mock.local")
os.environ.setdefault("NETOPS_JWT_SECRET", "conv-test-secret")
os.environ["NETOPS_LLM_BASE_URL"] = "http://localhost:1234/v1"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.auth.jwt_utils import create_token  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import Conversation, Message, User  # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, ok: bool, detail: str = ""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"PASS: {name}")
    else:
        FAIL += 1
        print(f"FAIL: {name} {detail}")


def seed_conversations():
    """Пользователи + 33 диалога (30 engineer, 3 viewer) с сообщениями.

    Спец-случаи: «Диалог 0» — длинный контент; «Диалог 1» — tool-шаг;
    «Диалог 2» — пустой. Активность монотонно растёт с номером, поэтому
    первая строка таблицы — «Диалог 29».
    """
    with SessionLocal() as db:
        eng = User(username="eng1@mock.local", display_name="Инженер",
                   role="engineer", is_active=True, granted_by="test")
        viewer = User(username="view1@mock.local", display_name="Вьюер",
                      role="viewer", is_active=True, granted_by="test")
        db.add_all([eng, viewer])
        db.flush()
        eng_id, viewer_id = eng.id, viewer.id

        base = datetime.utcnow() - timedelta(hours=2)
        for i in range(30):
            conv = Conversation(
                user_id=eng_id, title=f"Диалог {i}",
                created_at=base + timedelta(minutes=5 * i))
            db.add(conv)
            db.flush()
            if i == 0:
                db.add(Message(conversation_id=conv.id, role="user",
                               content="R" * 1000,
                               created_at=base + timedelta(seconds=40)))
                db.add(Message(conversation_id=conv.id, role="assistant",
                               content="короткий ответ",
                               created_at=base + timedelta(seconds=80)))
            elif i == 1:
                db.add(Message(conversation_id=conv.id, role="user",
                               content="вопрос по инструменту",
                               created_at=base + timedelta(minutes=5, seconds=60)))
                db.add(Message(
                    conversation_id=conv.id, role="assistant", content="",
                    tool_calls='[{"id": "c1", "name": "vmware_vms", '
                               '"arguments": "{}"}]',
                    created_at=base + timedelta(minutes=5, seconds=120)))
                db.add(Message(conversation_id=conv.id, role="tool",
                               content="результат инструмента",
                               tool_call_id="c1", name="vmware_vms",
                               created_at=base + timedelta(minutes=5, seconds=180)))
            elif i == 2:
                pass  # пустой диалог: 0 сообщений
            else:
                db.add(Message(conversation_id=conv.id, role="user",
                               content=f"вопрос {i}",
                               created_at=base + timedelta(minutes=5 * i + 1)))
                db.add(Message(conversation_id=conv.id, role="assistant",
                               content=f"ответ {i}",
                               created_at=base + timedelta(minutes=5 * i + 2)))
        for j in range(3):  # старые диалоги viewer — попадают на стр. 2
            conv = Conversation(
                user_id=viewer_id, title=f"Вьюерский {j}",
                created_at=base - timedelta(minutes=60 - 20 * j))
            db.add(conv)
            db.flush()
            db.add(Message(conversation_id=conv.id, role="user",
                           content=f"вопрос viewer {j}",
                           created_at=conv.created_at + timedelta(minutes=1)))

        db.commit()
        ids = {}
        for title in ("Диалог 0", "Диалог 1", "Диалог 2"):
            ids[title] = db.query(Conversation).filter_by(title=title).first().id
        # строка дат пустого диалога: created == updated -> в деталях дважды
        empty_created = (base + timedelta(minutes=10)).strftime("%d.%m.%Y %H:%M:%S")
        return (eng_id, viewer_id, ids["Диалог 0"], ids["Диалог 1"],
                ids["Диалог 2"], empty_created)


def main():
    with TestClient(app) as client:
        eng_id, viewer_id, long_id, tool_id, empty_id, empty_created = \
            seed_conversations()  # после lifespan (create_all)

        # --- RBAC ---
        r = client.get("/admin/conversations", follow_redirects=False)
        check("GET /admin/conversations без cookie -> редирект на логин",
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
                           create_token(viewer_id, "view1@mock.local", "viewer"))
        for path in ("/admin/conversations",
                     "/admin/conversations/partial/table",
                     f"/admin/conversations/{tool_id}/details"):
            r = client.get(path)
            check(f"viewer -> 403 на {path}",
                  r.status_code == 403
                  or (r.status_code in (302, 303)
                      and "/admin/login" in r.headers.get("location", "")),
                  f"got {r.status_code}")

        # engineer: доступен (отличие от аудита — admin-only)
        client.cookies.set("netops_token",
                           create_token(eng_id, "eng1@mock.local", "engineer"))
        r = client.get("/admin/conversations")
        check("GET /admin/conversations (engineer) -> 200, заголовок",
              r.status_code == 200 and "История диалогов" in r.text,
              f"got {r.status_code}")
        r = client.get("/admin/conversations/partial/table")
        check("partial/table (engineer) -> 200, счётчик 33",
              r.status_code == 200 and "Всего: 33" in r.text,
              f"got {r.status_code}")

        # Возврат в сессию admin: перезаписываем cookie напрямую
        client.cookies.set("netops_token", admin_token)

        r = client.get("/admin/conversations")
        check("GET /admin/conversations (admin) -> 200, заголовок",
              r.status_code == 200 and "История диалогов" in r.text,
              f"got {r.status_code}")

        # --- Таблица: фильтры ---
        r = client.get("/admin/conversations/partial/table")
        check("partial/table без фильтров -> 200, счётчик 33",
              r.status_code == 200 and "Всего: 33" in r.text,
              f"got {r.status_code}")

        r = client.get("/admin/conversations/partial/table",
                       params={"user": "view1"})
        check("фильтр по пользователю (view1): 3 диалога",
              r.status_code == 200 and "Всего: 3" in r.text
              and "view1@mock.local" in r.text, f"got {r.status_code}")

        r = client.get("/admin/conversations/partial/table",
                       params={"user": "eng1"})
        check("фильтр по пользователю (eng1): 30 диалогов",
              r.status_code == 200 and "Всего: 30" in r.text,
              f"got {r.status_code}")

        r = client.get("/admin/conversations/partial/table",
                       params={"q": "Вьюерский"})
        check("фильтр по заголовку: 3 диалога",
              r.status_code == 200 and "Всего: 3" in r.text
              and "eng1@mock.local" not in r.text, f"got {r.status_code}")

        r = client.get("/admin/conversations/partial/table",
                       params={"user": "nosuch"})
        check("фильтр по пользователю: нет совпадений",
              r.status_code == 200 and "Диалогов нет" in r.text,
              f"got {r.status_code}")

        # --- Сортировка и счётчики ---
        r = client.get("/admin/conversations/partial/table")
        check("сортировка по последней активности: «Диалог 29» выше «28»",
              r.text.find("Диалог 29") < r.text.find("Диалог 28")
              and r.text.find("Диалог 29") != -1, "")
        check("счётчик сообщений в таблице (2 у обычных)",
              'whitespace-nowrap">2</td>' in r.text, "")

        # --- Пагинация ---
        r = client.get("/admin/conversations/partial/table",
                       params={"page": 2})
        check("пагинация: page=2, «из 2», диалоги viewer здесь",
              r.status_code == 200 and "Страница 2 из 2" in r.text
              and "view1@mock.local" in r.text, f"got {r.status_code}")
        check("счётчик сообщений: 0 у пустого диалога (стр. 2)",
              'whitespace-nowrap">0</td>' in r.text, "")
        r = client.get("/admin/conversations/partial/table",
                       params={"page": 99})
        check("пагинация: page=99 клампится в диапазон",
              r.status_code == 200 and "Страница 2 из 2" in r.text,
              f"got {r.status_code}")

        # --- Фильтры по датам ---
        r = client.get("/admin/conversations/partial/table",
                       params={"date_from": "2000-01-01",
                               "date_to": "2100-01-01"})
        check("даты-фильтры: глухие границы -> все диалоги",
              r.status_code == 200 and "Всего: 33" in r.text,
              f"got {r.status_code}")
        r = client.get("/admin/conversations/partial/table",
                       params={"date_from": "2100-01-01"})
        check("даты-фильтры: будущий date_from -> пусто",
              r.status_code == 200 and "Диалогов нет" in r.text,
              f"got {r.status_code}")
        r = client.get("/admin/conversations/partial/table",
                       params={"date_from": "битая-дата"})
        check("битые значения дат игнорируются",
              r.status_code == 200, f"got {r.status_code}")

        # --- Детали: полный контент, без обрезки ---
        r = client.get(f"/admin/conversations/{long_id}/details")
        check("детали -> 200, полный контент (>800 симв., без обрезки)",
              r.status_code == 200 and "R" * 900 in r.text,
              f"got {r.status_code}, len={len(r.text)}")
        check("детали: мета (пользователь, заголовок)",
              "eng1@mock.local" in r.text and "Диалог 0" in r.text, "")

        r = client.get(f"/admin/conversations/{tool_id}/details")
        # кавычки JSON экранируются в HTML (&quot;), проверяем без них
        check("детали tool-диалога: имя инструмента и JSON вызова",
              r.status_code == 200 and "vmware_vms" in r.text
              and "c1" in r.text, f"got {r.status_code}")
        check("детали tool-диалога: порядок user -> tool",
              r.text.find("вопрос по инструменту")
              < r.text.find("результат инструмента"), "")

        r = client.get(f"/admin/conversations/{empty_id}/details")
        check("детали пустого диалога: «Сообщений нет», created == updated",
              r.status_code == 200 and "Сообщений нет" in r.text
              and r.text.count(empty_created) >= 2,
              f"got {r.status_code}")

        r = client.get("/admin/conversations/999999/details")
        check("детали: 404 для несуществующего диалога",
              r.status_code == 404, f"got {r.status_code}")

        # --- Старые REST-эндпоинты не сломаны ---
        r = client.get("/api/conversations",
                       headers={"Authorization":
                                f"Bearer {create_token(eng_id, 'eng1@mock.local', 'engineer')}"})
        check("GET /api/conversations (engineer) -> свои 30 диалогов",
              r.status_code == 200 and len(r.json()) == 30,
              f"got {r.status_code}, len={len(r.json()) if r.status_code == 200 else '?'}")
        r = client.get("/api/conversations",
                       headers={"Authorization": f"Bearer {admin_token}"})
        check("GET /api/conversations (admin) -> только свои (пусто)",
              r.status_code == 200 and r.json() == [],
              f"got {r.status_code}")
        r = client.get(f"/api/conversations/{tool_id}/messages",
                       headers={"Authorization":
                                f"Bearer {create_token(eng_id, 'eng1@mock.local', 'engineer')}"})
        check("GET /api/conversations/{id}/messages не сломан (tool-строки)",
              r.status_code == 200 and "vmware_vms" in r.text,
              f"got {r.status_code}")

    print()
    print(f"Итог: PASS={PASS} FAIL={FAIL}")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
