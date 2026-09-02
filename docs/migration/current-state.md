# NetOps LLM — снимок текущего состояния (Фаза 0)

> Снимок актуален на 2026-09-02. Все факты сверены с кодом (file:line).
> Это опорный документ для фаз миграции UI (migration.md): Chainlit-чат + Jinja2/HTMX-админка.

## 1. Карта маршрутов

### Авторизация (backend/app/auth/routes.py, префикс `/api`)
| Метод и путь | Доступ | Что делает | Где |
|---|---|---|---|
| POST `/api/auth/login` | публичный | AD/LDAP-вход (ldap3), авто-регистрация viewer, bootstrap-повышение до admin; возвращает `{token}` | routes.py:48-93 |
| GET `/api/auth/me` | любой токен | профиль текущего пользователя | routes.py:96-99 |
| GET `/api/users` | admin | список пользователей | routes.py:105-113 |
| POST `/api/users` | admin | добавить пользователя | routes.py:115-126 |
| PATCH `/api/users/{uid}` | admin | роль/is_active/display_name; нельзя лишить прав себя | routes.py:128-144 |
| DELETE `/api/users/{uid}` | admin | удалить пользователя | routes.py:147+ |

**Logout-эндпоинта НЕТ** — SPA просто удаляет токен из localStorage.

### Чат и аудит (backend/app/api/chat.py)
| Метод и путь | Доступ | Что делает | Где |
|---|---|---|---|
| GET `/api/models` | любой токен | список моделей LLM (LM Studio) | chat.py:156-163 |
| POST `/api/conversations` | любой токен | создать диалог | chat.py:165-173 |
| GET `/api/conversations` | любой токен | свои диалоги | chat.py:175-182 |
| GET `/api/conversations/{cid}/messages` | владелец | история сообщений | chat.py:184-190 |
| PATCH `/api/conversations/{cid}` | владелец | переименовать | chat.py:192-204 |
| DELETE `/api/conversations/{cid}` | владелец | удалить (каскад сообщений) | chat.py:206-213 |
| GET `/api/audit?limit=100` | admin | журнал вызовов инструментов | chat.py:215-230 |
| POST `/api/conversations/{cid}/messages` | владелец | **SSE-стрим агентского цикла** | chat.py:232+ |

### Инвентарь (backend/app/api/devices.py, префикс `/api/devices`)
| Метод и путь | Доступ | Что делает | Где |
|---|---|---|---|
| GET `/api/devices` | любой токен | список устройств (сериализация `_public` БЕЗ пароля) | devices.py:54-58 |
| POST `/api/devices` | admin | создать устройство (manual) | devices.py:61-77 |
| PATCH `/api/devices/bulk` | admin | вкл/выкл группой (ids или group) | devices.py:79-93 |
| POST `/api/devices/sync-zabbix` | admin | синхронизация из Zabbix; отключение пропавших | devices.py:110-161 |
| PATCH `/api/devices/{device_id}` | admin | изменить устройство | devices.py:164+ |
| DELETE `/api/devices/{device_id}` | admin | удалить устройство | devices.py:196+ |

### Статика и точки входа
- `frontend/index.html` (~752 строки, монолит) — единственный UI, отдаётся nginx (`/` → try_files). Вкладки: Чат / Инвентарь / Пользователи / Аудит. JWT хранится в **localStorage** (`api()`-обёртка, logout по 401).
- `backend/app/main.py:73-76` — mount `StaticFiles(frontend/)` для локальной разработки (в Docker-контейнере frontend нет, статику отдаёт nginx).
- Роутеры подключаются в `main.py:68-70` (auth, chat, devices).

## 2. Карта SSE-событий (POST /api/conversations/{cid}/messages)

Формат кадра: `data: {json}\n\n` (функция `sse()`, chat.py:102-103). Завершающий маркер: `data: [DONE]\n\n` (chat.py:355).

| Событие | Поля | Где генерируется |
|---|---|---|
| `{"delta": str}` | кусок текста ассистента | chat.py:141 (очередь из `_stream_turn`) |
| `{"tool": name, "args": {...}}` | вызов инструмента | chat.py:301 |
| `{"tool_result": {name, ok, preview}}` | результат: ok=status=="ok", preview=result[:200] | chat.py:304-306 |
| `{"error": "Сервер LLM недоступен, попробуйте позже"}` | при исключении в цикле | chat.py:348 |

Агентский цикл (chat.py:232-357):
- **MAX_AGENT_STEPS = 20** (chat.py:20); при исчерпании — `{"delta": "\n\n(Остановлено: лимит шагов агента)"}` (chat.py:345).
- История: последние `NETOPS_HISTORY_MESSAGES` (по умолчанию 20, config.py:22) сообщений; голова срезается до первого `user` (chat.py:252-256).
- `_stream_turn` (chat.py:129+) — один вызов модели через `llm.client` (AsyncOpenAI, LM Studio): текст стримится по токенам, tool_calls аккумулируются.
- Tool-вызовы одной волны выполняются **параллельно** (`asyncio.create_task`/`gather`, chat.py:295-325); tool-сообщения сохраняются в БД (chat.py:330-341).
- Системный промпт: `build_system_prompt(db)` (chat.py:59) — инструкции + инвентарь устройств.

## 3. Роли и RBAC

- Роли: **admin / engineer / viewer** (models.py:8-11, enum Role).
- Проверки: `get_current_user` (auth/deps.py:15-29, Bearer JWT → пользователь из БД), `require_admin` (auth/deps.py:32-37, 403 для не-admin).
- Разделы API по ролям: чат/диалоги — все роли (владелец); инвентарь GET — все, мутации — admin; пользователи — admin; аудит — admin (`require_admin`, chat.py:216).
- Инструменты агента: `@register_tool(..., roles=...)` — **по умолчанию разрешены все роли** (tools.py:74). Точечных ограничений нет.
- `execute_tool(name, args, user_id, conversation_id, role)` (tools.py:775+) — проверяет роль по реестру, выполняет через TTL-кэш (cachetools, TTL=300с), пишет AuditLog (user_id, conversation_id, tool, arguments, result[:4000], status ok/error) и режет результат до MAX_RESULT=20000 символов.
- Старый SPA скрывает вкладки «Пользователи»/«Аудит» для не-админа (frontend/index.html ~:405-409) — это только UI, серверная проверка в роутерах.

## 4. Инструменты агента (18 шт., backend/app/agent/tools.py)

`get_current_time` (:329), `ping` (:344), `list_devices` (:367), `list_groups` (:383), `vmware_vms` (:410), `vmware_hosts` (:416), `vmware_snapshots` (:422), `vmware_datastores` (:428), `vmware_vm_disks` (:434), `vmware_host_networks` (:440), `vmware_vm_networks` (:446), `vmware_events` (:452), `vmware_host_sensors` (:463), `zabbix_problems` (:470), `zabbix_items` (:483), `zabbix_history` (:496), `get_device_full_health` (:513, композитный), `get_infrastructure_health` (:616, композитный).

Композитные инструменты (`get_device_full_health`, `get_infrastructure_health`) вызывают подинструменты через registry-обёртки. ssh_execute / snmp_* / search_* **удалены ранее** и не существуют.

## 5. Модели БД (backend/app/models.py)

- `User`: username (UPN), display_name, role, is_active, granted_by, granted_at (:14-25).
- `Conversation`: user_id, title, created_at (:28-33).
- `Message`: conversation_id, role, content, name, tool_call_id, tool_calls (JSON), created_at (:36-50).
- `Device`: name, type (enum DeviceType: vcenter/esxi/eltex/edgecore/snr/aruba/other), host, port, username, **password (открытым текстом)**, enabled, description, source, zabbix_hostid, group (:53-70).
- `AuditLog`: user_id, conversation_id, tool, arguments, result, status, created_at (:73-81).

## 6. Шаблоны / статика / инфраструктура

- `frontend/index.html` — SPA-монолит: логин-экран (:255-261), чат с сайдбаром диалогов (:263-279), пользователи (:281-296), аудит (:298-315), инвентарь (:317-365); JWT в localStorage; SSE читает `r.body.getReader()` + парсинг `data:`-строк (`[DONE]` пропускается).
- `nginx/nginx.conf` — `location /api/` → `proxy_pass http://app:8000`, `proxy_buffering off` (SSE), `proxy_read_timeout 600s` (:9-16); **WS-заготовок нет** (нет Upgrade-заголовков — понадобится для Chainlit).
- `docker-compose.yml` — 3 сервиса: `db` (postgres:16-alpine, volume pgdata), `app` (build ./backend, env_file .env), `web` (nginx:alpine, 8080:80, монтирует nginx.conf и frontend/). **Healthcheck'и отсутствуют**, extra_hosts нет. Порты app/db наружу не проброшены.
- `backend/Dockerfile` — python:3.12-slim, копирует только `app/`; CMD uvicorn :8000.
- Авто-миграция колонок `messages` (tool_calls, tool_call_id, name) при старте — `_ensure_message_columns()` (main.py:42-56); bootstrap-админ в `lifespan` (main.py:59-64): `bootstrap_admin()` из `NETOPS_BOOTSTRAP_ADMIN` (main.py импортирует из auth/bootstrap.py — повышение при логине, routes.py:78-88).

## 7. Секретные переменные (только имена — БЕЗ значений)

- `NETOPS_JWT_SECRET` (подпись JWT HS256)
- `NETOPS_ZABBIX_TOKEN` (API-токен Zabbix)
- `DB_PASSWORD` (postgres)
- Поля `username`/`password` таблицы Device (доступ к vCenter/ESXi — открытым текстом в БД)
- `NETOPS_BOOTSTRAP_ADMIN` (UPN, автоматически становится admin)
- .env.example в корне — образец заполнения (без реальных секретов)

## 8. Функции, которые нельзя сломать (инварианты миграции)

1. **Агентский цикл** (chat.py:232-357): стриминг токенов, параллельные tool-вызовы, сохранение в БД, лимит 20 шагов.
2. **execute_tool + аудит** (tools.py:775+): RBAC, TTL-кэш, AuditLog, обрезка результата.
3. **SSE-контракт**: имена событий `delta`/`tool`/`tool_result`/`error` + `[DONE]` — старый SPA парсит именно их (frontend/index.html:534-540). Переименование сломает SPA.
4. **RBAC-модель** (auth/deps.py): get_current_user / require_admin на роутах.
5. **Login flow** (auth/routes.py:48-93 + ldap_auth.py:27-30): AD-аутентификация через ldap3; `NETOPS_DEV_MODE=true` пропускает AD (ldap_auth.py:28-30) — так работает smoke_test.
6. **Bootstrap-админ** (main.py lifespan + routes.py:78-88).
7. **JWT** (auth/jwt_utils.py:6-14): HS256, **12 часов** (config.py:11 `jwt_hours: int = 12`), поля sub/username/role.
8. **GET /api/models** (chat.py:156-163) — селектор модели в SPA.

## 9. Мок-режим

В коде на момент снимка **отсутствует** (нет NETOPS_MOCK_MODE в config.py, httpx объявлен в requirements.txt, но нигде не используется). `NETOPS_MOCK_MODE` добавляется в рамках Фазы 0 миграции (agent/mock.py + перехват в llm/client.py и register_tool-обёртке).

## 10. Команды локального запуска (Windows, из backend/)

```bash
# env для разработки (без AD и внешних сервисов):
NETOPS_DEV_MODE=true
NETOPS_DATABASE_URL=sqlite:///./dev.db     # изолировать от рабочей netops.db
NETOPS_JWT_SECRET=<любой для dev>
NETOPS_BOOTSTRAP_ADMIN=admin@corp.local
NETOPS_LLM_BASE_URL=http://localhost:1234/v1

uvicorn app.main:app --reload              # http://127.0.0.1:8000
python smoke_test.py                       # интеграционные проверки без LLM (temp sqlite)
```

Образец env-файла: `.env.example` (корень). Тестов на pytest нет — только `backend/smoke_test.py` (TestClient, создаёт temp sqlite, проверяет dev-логин, CRUD диалогов, tool-сообщения, аудит, 403 viewer, каскадное удаление).

## 11. Что будет меняться (кратко, из migration.md)

SPA-монолит → **Chainlit-чат** (сервис `chat-app`, внутренний API `/internal/*` с сервис-токеном) + **админка FastAPI/Jinja2/HTMX/Tailwind** (`/admin/*`, сессии на cookie, роли). Старый SPA не удаляется до конца миграции (Фаза 16).
