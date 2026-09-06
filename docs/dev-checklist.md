# Чек-лист локальной проверки (Фазы 12–13, migration.md)

Полный роут-трип dev-стека без реальных LLM/Zabbix/VMware:
мок-инструменты (`NETOPS_MOCK_MODE`), логин мимо AD (`NETOPS_DEV_MODE`,
пароль любой), sqlite вместо Postgres.

## Запуск стенда

```bash
docker compose -f docker-compose.dev.yml up --build -d
docker compose -f docker-compose.dev.yml run --rm seed   # фикстуры (идемпотентно)
```

Стек: `web` (nginx, http://localhost:8080) → `chainlit` (/chat) → `app` (FastAPI).
Сброс данных: `docker compose -f docker-compose.dev.yml down -v`.

Тестовые пользователи (создаются сидом ДО первого входа — в DEV_MODE
роль берётся из БД, автовыдача даёт только viewer):

| Логин                | Роль     | Пароль       |
|----------------------|----------|--------------|
| admin@example.com    | admin    | любой        |
| engineer@example.com  | engineer | любой        |
| viewer@example.com    | viewer   | любой        |

Эталонные SSE-сценарии — `chainlit/dev_sse/*.sse`, контракт проверяется
тестом `chainlit/dev_sse/scenarios_test.py` (venv chainlit).

## Проверка (11 пунктов migration.md)

| № | Что проверить | Как | Ожидание |
|---|---------------|-----|----------|
| 1 | Логин | http://localhost:8080 → вход `admin@example.com` + любой пароль | Редирект в интерфейс, роль admin |
| 2 | Доступ к админке | /admin/ (под admin) | Страницы админки открываются |
| 3 | Доступ к инвентарю | /admin/inventory | 6 устройств из сида (core-rtr1, sw-access-1, fw-usergate, vcenter-stand, esxi-stand-1, srv-old) |
| 4 | Доступ к аудиту | /admin/audit (только admin; engineer/viewer → 403 — см. router.py `require_roles_page(Role.admin)`) | 4 записи сида: ok / error / denied (+ маркер seed_dev) |
| 5 | Открытие чата | /chat (Chainlit) | Загружается, виден индикатор агента (FIX-04) |
| 6 | Отправка сообщения | «Пинг mock-host» в чате | Сообщение уходит, приходит delta-текст |
| 7 | Шаги инструментов | Тот же запрос | Появляются шаги инструмента `ping` (tool → tool_result) |
| 8 | Ошибка инструмента | «Пинг mock-ошибка» (триггер из mock.py) | tool_result ok=false, ошибка видна; в аудите запись со status="error" |
| 9 | Финальный ответ | Запрос из п.6 | Итоговое сообщение ассистента; стрим завершается ([DONE]) |
| 10 | Сохранение диалога | /admin/conversations (или повторное открытие чата) | Диалог в списке, сообщения сохранены |
| 11 | Сохранение аудита | /admin/audit | Появились записи реальных вызовов инструментов |

## Мок-триггеры

- Инструмент `ping` с host `mock-ошибка` → status="error" (mock.py)
- Сообщение «ошибка» в чате → сценарий ошибки LLM (llm/client.py)
- Сообщение «лимит» в чате → сценарий лимита шагов агента (llm/client.py)

## Без docker (локально, только backend)

```bash
cd backend
NETOPS_MOCK_MODE=true NETOPS_DEV_MODE=true \
  NETOPS_DATABASE_URL=sqlite:///./dev-tmp.db \
  NETOPS_BOOTSTRAP_ADMIN=admin@example.com \
  NETOPS_JWT_SECRET=dev uvicorn app.main:app --port 8000
```

## Сквозной ручной чек-лист Фазы 13

Автоматическая версия тех же 14 шагов — тест
`backend/tests/test_e2e_flow.py` (TestClient, временная sqlite, мок-режим):

```bash
cd backend && .venv/Scripts/python.exe tests/test_e2e_flow.py
```

Ручная часть — то, что локально не автоматизируется (визуальная
проверка UI, без curl/TestClient):

| № | Что проверить | Как | Ожидание |
|---|---------------|-----|----------|
| 4 | Создание устройства через модальную форму | Инвентарь → «Добавить» → заполнить форму → Сохранить (admin) | Модалка закрывается, flash «Устройство добавлено», строка устройства в таблице |
| 13 | Вход viewer | http://localhost:8080 → `viewer@example.com` + любой пароль | Dashboard открывается; пункты Инвентарь/Аудит/Настройки скрыты в меню, прямые URL дают 403 |
| 14 | Чат viewer | Открыть /chat, отправить «Пинг mock-host» | Чат открывается, шаги инструмента `ping` и ответ отображаются (инструменты доступны viewer — tools.py:87) |
| — | Индикатор агента (FIX-04) | Любой запрос в чате | «Агент думает» → «Шаг 1: вызов ping» → «Выполнено 1 шаг(ов)»; без шагов — «Ответ готов»; при сбое — «Ошибка» |
| — | nginx / WebSocket / live-сервер | — | Проверяется на сервере, вне локальной Фазы 13 |

Примечание к RBAC аудита: в старой таблице выше (п.4) аудит значился
как «admin/engineer» — по факту кода (`require_roles_page(Role.admin)`
в ui/router.py + NAV_ITEMS) раздел доступен только admin, engineer
получает 403. Строка выше исправлена.
