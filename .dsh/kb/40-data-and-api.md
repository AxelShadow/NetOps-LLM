> Статус: каркас · Обновлено: 2026-10-16 · Индексированный коммит: 5d169ba

# 40 — Данные и API

## Назначение

Раздел описывает схему данных (таблицы, связи, авто-миграции) и публичные контракты
приложения: группы HTTP-эндпоинтов `/api/*`, `/internal/*`, `/admin/*` и схему
аутентификации запросов к ним.

## Ключевые файлы

| Путь | Роль |
|---|---|
| `backend/app/models.py` | все ORM-модели и перечисления (`Role`, `DeviceType`) |
| `backend/app/db.py` | `Base`, `engine`, `SessionLocal`, `get_db` |
| `backend/app/main.py` | `lifespan`: `create_all` + авто-миграции колонок и ENUM + bootstrap-админ |
| `backend/app/crypto.py` | шифрование/дешифрование чувствительных полей (Fernet) |
| `backend/app/api/chat.py` | контракт диалогов и SSE-событий агентского цикла |
| `backend/app/api/devices.py` | контракт инвентаря, синхронизация Zabbix, discovery |
| `backend/app/api/internal.py` | внутренний сервисный API и `auth-check` для nginx |
| `backend/app/ui/router.py` | контракт HTML-страниц и HTMX-фрагментов `/admin/*` |
| `backend/seed_dev.py`, `backend/seed_devices.py` | фикстуры и массовый upsert устройств |
| `backend/migrate_zabbix.py` | разовый скрипт ALTER TABLE для полей Zabbix |
| `backend/tests/test_snmp_migration.py` | проверка авто-миграции колонок `devices` на «старой» БД |

## Структура и потоки

**Таблицы (SQLAlchemy, `backend/app/models.py`):**

| Таблица | Поля | Связи / особенности |
|---|---|---|
| `users` | `id`, `username` (UPN, unique), `display_name`, `role`, `is_active`, `granted_by`, `granted_at` | `role` — ENUM `admin/engineer/viewer` (дефолт `viewer`) |
| `conversations` | `id`, `user_id`, `title`, `created_at` | FK на `users`, каскадное удаление сообщений |
| `messages` | `id`, `conversation_id`, `role`, `content`, `tool_calls`, `tool_call_id`, `name`, `created_at` | `role` — `user`/`assistant`/`tool`; `tool_calls` — JSON-строка вызовов |
| `devices` | `id`, `name` (unique), `type`, `host`, `port`, `username`, `password`, `enabled`, `description`, `source`, `zabbix_hostid`, `group_name`, `snmp_version`, `snmp_community`, `protocol`, `mac`, `dns_name` | `type` — ENUM `DeviceType`; `source` — `manual`/`zabbix`/`snmp`; пароль и community шифруются |
| `audit_log` | `id`, `created_at`, `user_id`, `conversation_id`, `tool`, `arguments`, `result`, `status`, `duration_ms` | `status` — `ok`/`error`/`denied`; результат обрезан до 4000 символов |
| `app_settings` | `key` (PK), `value`, `updated_at` | runtime-настройки (бюджет контекста, лимит шагов агента) |

- `DeviceType` (по коду): `eltex`, `mikrotik`, `usergate`, `printer`, `aruba`,
  `edgecore`, `vcenter`, `esxi`, `other`.
- Чувствительные поля хранятся зашифрованными: свойства `Device.password` и
  `Device.snmp_community` вызывают `crypto.encrypt_value`/`decrypt_value`
  (Fernet, ключ `NETOPS_ENCRYPTION_KEY`); в API-ответах (`_public`) они не отдаются.
  `encrypt_value` возвращает сам токен Fernet (url-safe base64, без повторного
  кодирования). `decrypt_value` различает три случая: пустая строка → пустая;
  legacy-plaintext (не токен Fernet) → возвращается как есть для совместимости
  со старыми строками, при следующей записи значение шифруется; токен Fernet,
  который не расшифровывается текущим ключом → `InvalidToken` (шифротекст
  никогда не уходит на устройство вместо пароля).
- Ключ обязателен вне dev: `crypto._get_or_generate_key` бросает
  `KeyConfigurationError` при пустом `NETOPS_ENCRYPTION_KEY` и выключенном
  `NETOPS_DEV_MODE`; в dev-режиме генерируется временный ключ — **один раз на
  процесс** (иначе значения не читались бы даже внутри одного запуска) с
  предупреждением в логе без значения ключа. `get_key_info()` отдаёт метаданные
  ключа (наличие, длину, признак dev-fallback) без самого ключа.

**Миграции:** alembic в проекте **нет**. Схема догоняется при старте приложения
(`lifespan` в `main.py`) идемпотентными DDL-функциями:

| Функция | Что делает |
|---|---|
| `Base.metadata.create_all(engine)` | создаёт отсутствующие таблицы |
| `_ensure_message_columns()` | `ALTER TABLE messages ADD COLUMN` для `tool_calls`, `tool_call_id`, `name` |
| `_ensure_audit_columns()` | добавляет `audit_log.duration_ms` |
| `_ensure_device_columns()` | добавляет `devices.snmp_version`, `snmp_community`, `protocol`, `mac`, `dns_name` |
| `_ensure_printer_enum_value()` | на Postgres `ALTER TYPE devicetype ADD VALUE IF NOT EXISTS` для `printer`, `aruba`, `edgecore` (на sqlite тихо пропускается) |
| `bootstrap_admin()` | создаёт первого администратора из `NETOPS_BOOTSTRAP_ADMIN` |

`backend/seed_dev.py` повторяет тот же набор миграций перед засевом фикстур.
Ограничение: новый тип устройства или колонка требуют правки этих списков — иначе
на существующей БД INSERT/UPDATE упадёт.

**Группы HTTP-эндпоинтов:**

| Группа | Файл | Эндпоинты (кратко) | Доступ |
|---|---|---|---|
| Auth | `auth/routes.py` | `POST /api/auth/login`, `GET /api/auth/me` | публично / любой токен |
| Users | `auth/routes.py` | `GET|POST /api/users`, `PATCH|DELETE /api/users/{uid}` | admin |
| Chat | `api/chat.py` | `GET /api/models`, `POST|GET /api/conversations`, `GET /api/conversations/{cid}/messages`, `PATCH|DELETE /api/conversations/{cid}` | любой токен, диалоги — владелец |
| Agent | `api/chat.py` | `POST /api/conversations/{cid}/messages` — SSE-стрим агентского цикла | владелец диалога |
| Audit | `api/chat.py` | `GET /api/audit?limit=N` (1..500) | admin |
| Inventory | `api/devices.py` | `GET|POST /api/devices`, `PATCH /api/devices/bulk`, `PATCH|DELETE /api/devices/{id}`, `POST /api/devices/sync-zabbix` | чтение — любой токен, мутации — admin |
| Internal | `api/internal.py` | `GET /internal/auth-check`, `POST /internal/chat/stream` | сервисный токен |
| Admin UI | `ui/router.py` | страницы и HTMX-партиалы `/admin/*` (перечень — в `20-frontend.md`) | cookie-сессия + роли |
| Landing | `ui/home.py` | `GET /` | публично |

**SSE-контракт** (`POST /api/conversations/{cid}/messages` и
`/internal/chat/stream`): кадры `data: {json}\n\n` с ключами `delta`, `tool`,
`tool_result`, `error`; терминатор `data: [DONE]`. Дополнительно внутренний
эндпоинт возвращает заголовок `X-Conversation-Id` (id созданного/продолжаемого
диалога) и требует заголовок `X-User-Id` (Chainlit аутентифицирует пользователя сам).

**Схема аутентификации запросов:**

| Потребитель | Способ передачи | Проверка |
|---|---|---|
| Старый SPA, API-клиенты | `Authorization: Bearer <JWT>` | `get_current_user` (`auth/deps.py`), при необходимости `require_admin` |
| Админка `/admin/*` | HttpOnly-cookie `netops_token` (SameSite=Lax, TTL `NETOPS_JWT_HOURS`) | `get_current_user_page` + `require_roles_page(...)`; 401 → редирект на `/admin/login` |
| nginx → `/internal/auth-check` | сервисный токен в `X-Internal-Service-Token` + cookie/Bearer | `_check_service_token` (constant-time), ответ 204 + заголовки `X-User-Id/Email/Role/Display-Name` (email и display_name — percent-encoded) |
| Chainlit → `/internal/chat/stream` | `X-Internal-Service-Token` + `X-User-Id` | `_check_service_token` + `_load_user_by_id` (пользователь должен существовать и быть активным) |
| Chainlit login-форма | `POST /api/auth/login` → Bearer JWT → `GET /api/auth/me` | Chainlit не хранит JWT: из колбэка выходят только `id` и метаданные |

- JWT: HS256, payload `sub` (строковый id), `username`, `role`, `exp`; `sub`
  валидируется как числовой с ограничением длины (защита от 500 на huge-int).
- Пустой `NETOPS_INTERNAL_SERVICE_TOKEN` полностью выключает `/internal/*`.
- Логаут у API отсутствует — SPA просто удаляет токен; у админки есть
  `POST /admin/logout` (удаление cookie).
- CSRF-токенов нет: формы админки полагаются на `SameSite=Lax` cookie (комментарий
  в `ui/router.py`).

## Команды и проверки

```bash
# контракты API и SSE без LLM (TestClient, временная sqlite)
cd backend && .venv/Scripts/python.exe smoke_test.py
cd backend && .venv/Scripts/python.exe internal_api_test.py
cd backend && .venv/Scripts/python.exe internal_ext_test.py
cd backend && .venv/Scripts/python.exe tests/test_e2e_flow.py

# авто-миграция колонок devices на «старой» БД
cd backend && .venv/Scripts/python.exe tests/test_snmp_migration.py

# фикстуры (dev-стек или локальная sqlite)
docker compose -f docker-compose.dev.yml run --rm seed
cd backend && python seed_devices.py        # массовый upsert устройств
cd backend && python show_devices.py        # дамп инвентаря (диагностика)
```

## Открытые вопросы

- **не проверено:** фактическое содержимое рабочей `backend/netops.db` и прод-Postgres —
  БД не читались; состав колонок взят из кода моделей и авто-миграций.
- **требует уточнения:** `docs/migration/current-state.md` описывает
  `DeviceType.snr` и пароль устройства открытым текстом — по текущему коду такого
  типа нет, а пароль/community шифруются (`crypto.py`).
- **требует уточнения:** нет единого реестра контрактов — перечень эндпоинтов
  собирается из декораторов роутеров; авто-документация FastAPI (`/docs`,
  `/openapi.json`) в коде не отключается и, судя по `location /` в
  `nginx/default.conf.template`, может быть доступна снаружи — фактическая
  доступность не проверялась (риск для security-guard).
- **не проверено:** поведение `ALTER TYPE ... ADD VALUE` на прод-Postgres (версия PG
  и права пользователя) — покрыто только кодом и тестом на sqlite-ветке.
- Заполнить по ходу работы: политика хранения/очистки `messages` и `audit_log`
  (ротации и лимитов в коде не найдено).
- Заполнить по ходу работы: нужен ли отдельный публичный контракт для внешних
  потребителей, кроме `/internal/*` для Chainlit.
