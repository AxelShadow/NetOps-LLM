# NetOps-LLM — чат (Chainlit)

Chat-UI инфраструктурного ассистента. Только чат: админка — в FastAPI/Jinja2 (`/admin`).
Вся бизнес-логика, RBAC и аудит — в FastAPI-бэкенде; это приложение — тонкий клиент
внутреннего API `POST /internal/chat/stream` и **не обращается** к LM Studio,
Zabbix или VMware напрямую (запрещено migration.md).

**Статус: Фаза 6 (скелет, dev).** До Фазы 7 (авторизация) в прод не пускать:
user_id приходит из `CHAINLIT_DEV_USER_ID` и работает только при
`ENVIRONMENT=development`.

## Файлы

| Файл | Назначение |
|---|---|
| `app.py` | Точка входа: `on_chat_start` / `on_message`, conversation_id в `cl.user_session` |
| `client.py` | HTTP-клиент + SSE-парсер (`stream_chat`, `parse_sse_line`); НЕ импортирует chainlit |
| `adapters.py` | SSE-события → UI: `delta`→Message, `tool`/`tool_result`→Step (✔/✖) |
| `config.py` | env-конфиг (fail-fast), без зависимостей |
| `auth.py` | Фаза 7: header-auth (nginx) + логин-форма |
| `sse_parser_test.py` | Тест парсера и `stream_chat` (без chainlit, MockTransport) |

## Переменные окружения

| Переменная | Обяз. | Смысл |
|---|---|---|
| `FASTAPI_INTERNAL_URL` | да | База FastAPI, напр. `http://localhost:8000` |
| `NETOPS_INTERNAL_SERVICE_TOKEN` | да | Сервисный токен `/internal/*` (= бэкенд). Server-to-server, в браузер не попадает |
| `NETOPS_PROXY_AUTH_SECRET` | нет | Секрет proxy-авторизации (Фаза 7). Пусто = header-режим выключен, работает логин-форма |
| `ENVIRONMENT` | нет | `development` (дефолт) / `production` |
| `CHAINLIT_DEV_USER_ID` | нет | Только Фаза 6 + development: заглушка user_id (id из БД, обычно 1) |
| `CHAINLIT_HOST` / `CHAINLIT_PORT` | нет | Читает chainlit-cli (дефолт 127.0.0.1:8000); в Docker — 0.0.0.0:8001 |
| `CHAINLIT_AUTH_SECRET` | да | Подпись cookies Chainlit: `chainlit create-secret` |

## Запуск (Windows dev, мок-режим)

```bash
# 1. venv (однократно)
cd E:/netops-llm/chainlit
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt

# 2. backend (другой терминал) — мок-режим, dev-режим
cd E:/netops-llm/backend
NETOPS_MOCK_MODE=true NETOPS_DEV_MODE=true NETOPS_DATABASE_URL=sqlite:///./dev.db \
NETOPS_JWT_SECRET=dev-secret NETOPS_BOOTSTRAP_ADMIN=admin \
NETOPS_INTERNAL_SERVICE_TOKEN=dev-token \
.venv/Scripts/python -m uvicorn app.main:app --port 8000

# 3. chainlit
cd E:/netops-llm/chainlit
FASTAPI_INTERNAL_URL=http://localhost:8000 NETOPS_INTERNAL_SERVICE_TOKEN=dev-token \
ENVIRONMENT=development CHAINLIT_DEV_USER_ID=1 \
.venv/Scripts/chainlit run app.py
```

## Мок-сценарии для проверки (`NETOPS_MOCK_MODE=true` у бэкенда)

| Сообщение | Поведение |
|---|---|
| «пинг» / «ping» | Шаг `ping` (✔), затем финальный текст |
| «ошибка» | Шаг `ping` с ✖, финал «Инструмент завершился с ошибкой…» |
| «лимит» | 20 шагов `get_current_time`, затем «(Остановлено: лимит шагов агента)» |
| прочее | Шаг `get_current_time` (✔) + детерминированный финал |

## SSE-контракт `/internal/chat/stream`

Кадры `data: {json}\n\n` без имён событий: `{"delta": str}`,
`{"tool": name, "args": {...}}`, `{"tool_result": {name, ok, preview}}`,
`{"error": str}`; терминатор `data: [DONE]` (не JSON). Id диалога —
response-заголовок `X-Conversation-Id`. Источник истины диалогов — БД
FastAPI; здесь храним только id в сессии.

## Контракт `/internal/auth-check` (для nginx, Фаза 10)

`GET /internal/auth-check` с заголовком `X-Internal-Service-Token`:
проверяет сессию (cookie `netops_token` или `Authorization: Bearer`);
`204` + заголовки `X-User-Id`, `X-User-Email` (UPN), `X-User-Role`,
`X-User-Display-Name`, либо `401`/`403`. nginx будет использовать его в
`auth_request` и пробрасывать заголовки пользователя (через
`auth_request_set $… $upstream_http_x_…`) в Chainlit вместе с
`X-Proxy-Auth-Secret`; Chainlit принимает заголовки только при
совпадении секрета (`secrets.compare_digest`).

## Инварианты

- Прямой доступ к LM Studio / Zabbix / VMware из этого приложения — запрещён.
- Пользователю — только дружественные тексты ошибок (`BackendError.user_message`),
  без стеков/URL/внутренних деталей.
- Сервисный токен и proxy-секрет — только на сервере Chainlit (server-to-server).
- Пароли из логин-формы нигде не логируются и не сохраняются; JWT не кладётся
  в `cl.user_session` (используются только user_id и метаданные).
- Диалоги не дублируются: единственное хранилище — БД FastAPI.
