# Чек-лист локальной проверки (Фаза 12, migration.md)

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
| 4 | Доступ к аудиту | /admin/audit (только admin/engineer) | 4 записи сида: ok / error / denied (+ маркер seed_dev) |
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
