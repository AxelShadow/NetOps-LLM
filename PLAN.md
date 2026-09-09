# NetOps LLM — рабочее состояние проекта

> Ассистенту новой сессии: это полный рабочий журнал. Прочитай документ целиком,
> прежде чем предлагать изменения. Секреты в документе и в git не хранятся.

## 1. Суть проекта

Внутреннее клиент-серверное приложение IT-отдела: чат с локальной LLM, диагностика
инфраструктуры и автоматизация рутины. LLM-сервер — LM Studio (16 Гб VRAM),
OpenAI-совместимый API на порту 1234. Модель не имеет прямого доступа к железу —
все действия выполняются кодом приложения через инструменты (tool-calling)
с аудит-логом.

## 2. Окружение и доступы

| Что | Значение |
|---|---|
| Разработка | Windows, `E:\netops-llm`, Python 3.12, venv, uvicorn напрямую |
| Прод (целевой) | Ubuntu, Docker Compose (app + nginx + PostgreSQL) |
| LLM | LM Studio на той же машине, `http://localhost:1234/v1` |
| Модель сейчас | `openai/gpt-oss-20b` (reasoning effort = medium) |
| Альтернативная модель | Qwen3 14B Q4_K_M (лучше русский, покладистее; для неё `/no_think`) |
| AD | домен `id.samges.ru`, логины `user@id.samges.ru`, доступен |
| vCenter | 172.27.214.68, учётка `test_llm@vmwr.samges.ru` (read-only) |
| Standalone ESXi | vmh08 = 172.27.214.56, локальная учётка `test_llm` |
| Хосты под vCenter | vmh03.samges.ru, vmh05.samges.ru |
| Zabbix | **http://**zabbix-new.id.samges.ru (именно HTTP!), версия 6.2.9 |
| Zabbix API | токен отдельного пользователя `netops-llm`, передаётся параметром `auth` в теле JSON-RPC |
| Хостов в Zabbix | ~150, группы не структурированы |

Сетевое оборудование в компании: EdgeCore, Aruba, SNR, Eltex. Mikrotik нет.
UserGate — отложен на потом.

## 3. Архитектура
Браузер → APP-СЕРВЕР (FastAPI)
            ├── авторизация: AD (ldap3, UPN) + локальная таблица прав
            │   (доступ выдаёт админ; роли admin/engineer/viewer; DEV_MODE для dev)
            ├── чат: SSE-стриминг, агентский цикл tool-calling (до 20 шагов)
            ├── инструменты: Tool Registry (@register_tool) + TTL Cache + RBAC
            │   ├── VMware (pyvmomi): vCenter / standalone ESXi
            │   ├── Zabbix (JSON-RPC): один bulk-вызов для алертов
            │   ├── ping (subprocess)
            │   └── композитные: get_device_full_health, get_infrastructure_health
            ├── инвентарь: manual-устройства + синхронизация из Zabbix
            └── аудит-лог каждого вызова инструмента
        → LLM-СЕРВЕР (LM Studio; скрыт от пользователей, без доступа к железу)

Прод-сегментация: пользователи видят только app-сервер; адрес LLM известен только
конфигурации app-сервера; у LLM-сервера нет доступа к оборудованию и в интернет.
У LM Studio нет встроенной аутентификации — защита только сетевая.

## 4. Структура проекта
netops-llm/
├── PLAN.md                     # этот файл
├── docker-compose.yml          # прод: app + nginx + postgres
├── .env.example
├── nginx/nginx.conf            # proxy_buffering off — критично для SSE
├── frontend/index.html         # весь UI одним файлом (vanilla JS)
└── backend/
    ├── Dockerfile
    ├── requirements.txt        # прод (с psycopg2-binary, cachetools)
    ├── requirements-dev.txt    # dev на Windows (без psycopg2)
    ├── .env                    # НЕ в git
    ├── netops.db               # SQLite, НЕ в git
    ├── seed_devices.py         # массовый upsert устройств (скрипт)
    ├── migrate_zabbix.py       # ALTER TABLE для source/zabbix_hostid/group_name (выполнен)
    ├── show_devices.py         # дамп инвентаря
    ├── show_audit.py           # последние вызовы инструментов
    ├── check_auth.py           # диагностика настроек/входа
    ├── check_vcenter.py        # диагностика связи с VMware
    ├── check_problems.py       # диагностика хостов в проблемах Zabbix
    └── app/
        ├── main.py             # lifespan, bootstrap-админ, роутеры, статика frontend/
        ├── config.py           # Settings, env_prefix=NETOPS_, env_file=.env
        ├── db.py               # engine; SQLite: check_same_thread=False
        ├── models.py           # User, Conversation, Message, Device, AuditLog, Role, DeviceType
        ├── auth/               # ldap_auth (DEV_MODE), jwt_utils, deps, routes
        ├── llm/client.py       # AsyncOpenAI, очередь Semaphore, pick_model
        ├── agent/tools.py      # Tool Registry + все инструменты + execute_tool + аудит
        ├── devices/vmware.py   # VMwareAdapter + кэш сессий (get/drop/clear_cache)
        ├── devices/zabbix.py   # ZabbixClient (auth в теле запроса!)
        ├── api/chat.py         # диалоги + агентский цикл + build_system_prompt
        └── api/devices.py      # CRUD, /bulk, /sync-zabbix

## 5. Что сделано и проверено

### Этап 1 — каркас (готово)
- FastAPI + SQLite(dev)/PostgreSQL(prod); pydantic-settings, префикс `NETOPS_`, `.env`.
- Авторизация AD (UPN `user@домен`) + таблица `users` с ролями; доступ выдаёт админ
  (API `/api/users`, пока без UI); bootstrap-админ из `NETOPS_BOOTSTRAP_ADMIN`.
- DEV_MODE (`NETOPS_DEV_MODE=true`) — пропускает проверку AD, только для dev.
- Чат: диалоги, история в БД, стриминг SSE, очередь к LLM (Semaphore=1).
- Временный UI одним файлом: логин, чат, выбор модели.

### Этап 2 — VMware-агент (готово)
- Агентский цикл: стриминг с накоплением tool_calls из потока, MAX_AGENT_STEPS=20,
  выполнение инструментов через asyncio.to_thread; события tool/tool_result в UI (✔/✖).
- VMwareAdapter (pyvmomi, только чтение, кэш сессий, ретрай при таймауте):
  get_vms, get_hosts, get_snapshots, get_datastores, get_vm_disks,
  get_host_networks (vmnic/vmk/vSwitch), get_vm_networks, get_host_sensors,
  get_events (журнал событий, фильтр по ВМ/хосту, глубина в часах).
- Фильтры host/vm в инструментах; сравнение имён с учётом FQDN (_name_match).
- Режим device="all" — один вызов обходит все VMware-устройства инвентаря
  (модели плохо делают циклы сами — цикл зашит в инструмент).
- Автоподсказка: пустой результат с фильтром host → инструмент подсказывает
  самостоятельное устройство инвентаря.
- Инвентарь подставляется в системный промпт (build_system_prompt) + текущее время.

### Этап 3 — инвентарь: CRUD + Zabbix (готово)
- API устройств: список (без паролей), создание, изменение, удаление;
  роли: просмотр всем, изменение — admin.
- UI-вкладка «Инвентарь»: форма, таблица, поиск, фильтр по группе.
- Device: поля source (manual|zabbix), zabbix_hostid, group (колонка group_name).
- Синхронизация из Zabbix (POST /api/devices/sync-zabbix, admin):
  ~150 хостов импортируются ВЫКЛЮЧЕННЫМИ; обновление по zabbix_hostid;
  исчезнувшие из Zabbix — выключаются, не удаляются; имя нормализуется,
  при коллизии добавляется hostid.
- Zabbix-устройства в UI серые с бейджем; редактирование/удаление запрещено
  (API тоже), доступен только тумблер вкл/выкл — по одному и по группе
  (PATCH /api/devices/bulk; маршрут объявлен ДО /{device_id}!).
- Устройства из Zabbix видны модели только включёнными (как и все).

### Этап 4 — данные Zabbix в агенте (готово)
- zabbix_problems — активные проблемы; хосты достаются через trigger.get
  (problem.objectid = triggerid), т.к. в 6.2 selectHosts у problem.get не работает;
  сортировка по eventid (по clock нельзя).
- zabbix_items — последние значения метрик устройства, поиск по имени,
  возвращает itemid для истории.
- zabbix_history — история метрики (часы, лимит 7 дней), хронологический порядок.
- Правило промпта: устройства из Zabbix опрашиваются только через zabbix_*;
  «что болит» → zabbix_problems (без device — по всей инфраструктуре).

### Этап 5 — диалоги, история tool-calls, параллельность, аудит (готово, 2026-08-12)
- UI: сайдбар диалогов (список, открытие, создание, переименование, удаление);
  при загрузке страницы открывается последний диалог, не создаётся новый.
  API: PATCH/DELETE /api/conversations/{cid}.
- История хранит tool-вызовы: колонки Message.tool_calls (JSON)/tool_call_id/name,
  роль "tool". Шаг агента пишется атомарно (assistant с tool_calls + все
  tool-результаты). При переоткрытии диалога история отдаётся модели
  в OpenAI-формате; обрезанная голова срезается до первого user.
- Инструменты в агентском цикле выполняются параллельно (asyncio.gather,
  события стримятся по мере готовности).
- Аудит: GET /api/audit (только админы, require_admin) + вкладка «Аудит» в UI.
- Миграция колонок messages — в lifespan (_ensure_message_columns, inspect +
  ALTER TABLE; alembic не используется). Проверки: backend/smoke_test.py.

### Этап 6 — Tool Registry, TTL Cache, композитные инструменты (готово, 2026-08-13)
- **Tool Registry**: все инструменты обёрнуты в декоратор `@register_tool(name, description,
  parameters, cache_ttl, roles, is_composite)`. Схема для LLM генерируется автоматически
  из реестра (get_tools_schema). Убран if/elif из execute_tool.
- **In-memory TTL Cache** (cachetools.TTLCache): read-only инструменты кэшируются.
  Ключ = JSON-сериализация аргументов. Кэш не сохраняется при ошибках/denied.
  TTL: ping=60s, list_devices=300s, vmware=60-300s, zabbix=60-120s, композитные=120s.
- **RBAC на уровне инструментов**: каждый инструмент имеет список допустимых ролей.
  execute_tool проверяет user_role до вызова. user.role.value передаётся из chat.py.
- **Композитные инструменты**:
  - `get_current_time` — текущее время сервера (для расчёта длительности инцидентов).
  - `get_device_full_health(device)` — ping + Zabbix-алерты + датасторы (<15%) +
    хосты (CPU/RAM >85%) + снапшоты + события за 24ч. Один вызов = полная диагностика.
  - `get_infrastructure_health` — ОДИН bulk-запрос к Zabbix + опрос управляющих
    VMware-устройств. НЕ опрашивает хосты под vCenter отдельно.
- **Вспомогательные парсеры**: _extract_vmware_list, _get_free_percent, _safe_float —
  безопасное извлечение данных из ответов VMware-адаптера.
- **Принципы опроса**:
  - Zabbix: ОДИН вызов problem.get без device (все алерты сразу).
  - VMware: опрашиваем только vCenter + standalone ESXi. Хосты под vCenter НЕ
    опрашиваются отдельно (данные приходят через vCenter).
  - Инвентарь: используется для определения какие устройства опрашивать,
    но НЕ для поштучного пинга/опроса.
- **Системный промпт обновлён**: правило «что болит» → get_infrastructure_health;
  анализ порогов (датасторы <15%, CPU/RAM >85%, снапшоты).

### Этап 7 — миграция UI, Фазы 0–2 (готово, 2026-09-02)
Выполняется план из migration.md (16 фаз): чат → Chainlit, админка → FastAPI
+ Jinja2 + HTMX + Tailwind, backend остаётся источником логики/RBAC/аудита.
Снимок состояния и инварианты: docs/migration/current-state.md.
- **Фаза 0a** (a01254a): current-state.md переписан как точный снимок кода
  (роли admin/engineer/viewer, 18 инструментов, SSE-контракт delta/tool/
  tool_result/error + [DONE], JWT 12 ч).
- **Фаза 0b — мок-режим NETOPS_MOCK_MODE** (c9a9c11): при true сетевые
  инструменты отдают фейковые данные (перехват в register_tool, agent/mock.py),
  LLM подменяется MockLLMClient (сценарии: обычный/ошибка/лимит 20 шагов).
  Без сетевых вызовов. mock_smoke.py — 15/15 PASS.
- **Фаза 1 — внутренний API для Chainlit** (8331de7): агентский цикл выделен
  в run_agent_cycle (SSE-контракт старого SPA не изменён); POST
  /internal/chat/stream: сервисный токен NETOPS_INTERNAL_SERVICE_TOKEN
  (constant-time, пустой = выключено), пользователь по X-User-Id, создание
  диалога, аудит через execute_tool. internal_api_test.py — 16/16 PASS.
- **Фаза 2 — каркас админки /admin/*** (ee64829): Jinja2 + HTMX (локально,
  static/js/htmx.min.js) + Tailwind (CDN, прод-сборка — Фаза 13); логин/
  логаут с JWT в HttpOnly cookie netops_token (вход через тот же
  ad_authenticate); dashboard + заглушки инвентарь/диалоги/аудит/настройки;
  серверный RBAC: аудит+настройки — admin, инвентарь+диалоги — admin+engineer,
  viewer — только чат; exception-handler: /admin/* 401 → редирект на логин,
  403 → HTML (API-контракты не затронуты). admin_ui_test.py — 16/16 PASS.
- Регресс: smoke_test.py зелёный; все тесты изолированы (временная sqlite
  через NETOPS_DATABASE_URL до импорта app, netops.db не трогается).

### Этап 8 — миграция UI, Фазы 3–5: контент разделов админки (готово, 2026-09-03)
Наполнение каркаса /admin/* реальным контентом по образцу Фазы 2
(серверный рендеринг Jinja2 + HTMX-фрагменты, RBAC как в каркасе).
- **Фаза 3 — инвентарь /admin/inventory** (50cddec): таблица устройств
  с фильтрами (тип/источник/поиск/включённость) и пагинацией; HTMX-CRUD
  (модальные формы create/edit/delete через partial-ответы); кнопка
  sync-zabbix (ручная синхронизация с Zabbix). test_admin_ui_inventory.py —
  31/31 PASS.
- **Фаза 4 — аудит /admin/audit** (2e97dc1): таблица AuditLog с фильтрами
  (пользователь/действие/диалог/даты) и пагинацией; HTMX-детали записи
  (partial). test_admin_ui_audit.py — 23/23 PASS.
- **Фаза 5 — настройки /admin/settings** (714af19): конфигурация read-only
  без секретов (DEV_MODE, мок, авто-регистрация, история, лимит шагов
  агента, LLM URL/модель/таймаут, Zabbix URL, AD сервер/домен/Base DN,
  версия приложения = git-хеш с кэшем и фолбэком "dev"); HTMX-проверки
  подключений LLM/Zabbix/VMware с таймаутом 5 с (VMware — wait_for поверх
  to_thread, отдельная ветка TimeoutError: pyvmomi блокирует поток, но
  запрос не виснет на «чёрной дыре»); spinner.svg (htmx-indicator).
  Управление пользователями не делали — UI для него не существовало
  (только /api/users), справочник типов — статический enum.
  test_admin_ui_settings.py — 25/25 PASS (RBAC 403 engineer, отсутствие
  утечек секретов, тайм-аут «чёрной дыры» 10.255.255.1 <8 c).
- Верификация: субагент подтвердил статику (таймауты, секреты, RBAC,
  имена полей конфига); исполнение тестов — прогонами в основной сессии
  (в сессии верификатора Bash был заблокирован правами).
- Регресс после Фазы 5: инвентарь 31/31, аудит 23/23 — зелёные.

### Этап 9 — миграция UI, Фазы 6–7: Chainlit-чат + авторизация (готово, 2026-09-04)
Отдельное приложение chainlit/ — тонкий клиент внутреннего API; FastAPI
остаётся единственным источником бизнес-логики/RBAC/аудита.
- **Фаза 6 — скелет чата (714ad49)**: app.py (conversation_id в
  cl.user_session, id диалога из response-заголовка), client.py (SSE-парсер
  без импорта chainlit + stream_chat: 401/403/413/обрыв -> дружественные
  BackendError, ровно один автоповтор на 404), adapters.py (delta ->
  Message, tool/tool_result -> Step ✔/✖), config.py fail-fast; тесты:
  sse_parser_test.py 17/17 (парсер + MockTransport), internal_ext_test.py
  7/7 (заголовок X-Conversation-Id, продолжение диалога, 404 чужого).
  Backend: X-Conversation-Id в StreamingResponse /internal/chat/stream.
  Гэп «id нового диалога не возвращается» закрыт аддитивным заголовком.
- **Фаза 7 — авторизация (402be6c)**: GET /internal/auth-check для nginx
  auth_request (Фаза 10): сервисный токен + JWT (cookie netops_token или
  Bearer) -> 204 + X-User-Id/Email/Role/Display-Name, 401 нет/невалиден,
  403 отключён. load_user_from_token не различает невалидный JWT и
  деактивированного (везде None) — поэтому decode_token + явные проверки.
  /api/auth/me отдаёт id (логин-форме Chainlit нужен X-User-Id).
  chainlit/auth.py: header_auth по X-Proxy-Auth-Secret
  (secrets.compare_digest; пустой секрет/заголовок -> header-режим
  выключен) + password_auth (login -> JWT -> /me; пароль не логируется,
  JWT не сохраняется, умирает в колбэке); app.py берёт user_id из
  metadata сессии; dev-заглушка CHAINLIT_DEV_USER_ID удалена.
  internal_ext_test.py расширен до 16 чеков.
- Живой смоук (uvicorn + chainlit, dev/mock): логин-форма -> cookie
  access_token -> User(id=1, role=admin); auth-check: Bearer и cookie
  -> 204 + 4 заголовка, мусорный токен -> 401, деактивированный -> 403;
  header_auth 4/4 сценария; viewer проходит; в логах нет паролей/токенов.
- Верификация: два субагента (Фаза 6, Фаза 7) — PASS: все тесты, adversarial
  (подделанные JWT: sub=999/exp-в-прошлом/неверная-подпись -> 401),
  git-гигиена (.venv/.files/translations покрыты .gitignore).
- Известные мелочи (не блокеры): JWT с нечисловым sub даёт 500 (унаследованный
  паттерн из auth/deps.py, недостижим без секретного ключа); dev-режим
  авто-регистрирует *@ad_domain (дизайн NETOPS_DEV_MODE); X-User-Email
  несёт username из БД (bootstrap-админ хранится как "admin" без домена).

### Этап 10 — миграция UI, Фаза 9: история диалогов (готово, 2026-09-04)
/admin/conversations — список диалогов + просмотр сообщений; источник
истины — БД FastAPI (Chainlit свою БД не заводит). Клон паттерна аудита
(Этап 8), read-only.
- **Маршруты (be614e4)**: GET /admin/conversations/partial/table —
  агрегат _conv_query: join User (username), count(Message.id),
  coalesce(max(messages.created_at), created_at) как «дата обновления» —
  столбца в схеме нет, миграция не нужна; group_by Conversation.id +
  User.username (иначе Postgres only_full_group_by; в sqlite молча бы
  прошло), сортировка по последней активности DESC; фильтры user/q
  (ilike) + date_from/date_to (inclusive, битые даты игнорируются);
  пагинация 25 с клампом. GET /admin/conversations/{id}/details —
  сообщения по Message.id ASC включая role="tool" и tool_calls JSON,
  контент полностью (без обрезки, как в аудите), 404 «Диалог не найден».
- **RBAC**: admin+engineer (как в заглушке; отличие от аудита — admin-only).
- **Шаблоны**: pages/conversations.html (4 фильтра), components/
  conversations/table.html (колонки: #/пользователь/заголовок/создан/
  обновлён/сообщений; пагинация сохраняет фильтры), details.html
  (модалка: мета + лента сообщений, бейджи user/assistant/tool,
  имя инструмента, pre whitespace-pre-wrap, tool_calls отдельным блоком).
- **Осознанно исключено** (решение 2026-09-04): кнопка «Открыть в чате»
  и resume диалога в Chainlit (связка thread_id ↔ conversation_id) —
  отложено до появления маршрута /chat (Фаза 10) и resume-механизма.
- Тесты: test_admin_ui_conversations.py 30/30 (RBAC-матрица: engineer
  200 / viewer 403 / без cookie — редирект; фильтры, сортировка,
  счётчики, пагинация, детали без обрезки, регресс /api/conversations).
  Регресс: audit 23/23, inventory 31/31, settings 25/25, smoke ok.
- Верификатор PASS (adversarial: 422 не-числовой id, orphan-диалог →
  «—», конкурентные GET, XSS-экранирование, idempotent GET).
- Унаследованное наблюдение (не блокер, из Фазы 4): значения фильтров
  в пагинации не URL-кодируются (|urlencode) — одновременно в audit и
  conversations table.html; ломается только на &/=/#/+ в значении.

### Этап 11 — миграция UI, Фазы 10–11: nginx reverse-proxy + docker-compose (готово, 2026-09-05)
Сквозная маршрутизация: nginx как единая точка входа, Chainlit за
auth_request, всё в docker-compose. Backend — единственный источник
логики/RBAC/аудита; chainlit ходит только в /internal/chat/stream.
- **Фаза 10, nginx (a0d4dce)**: nginx/nginx.conf удалён, вместо него
  nginx/default.conf.template — envsubst-шаблон официального образа
  (/etc/nginx/templates → conf.d). Маршруты: точный `location = /admin`
  → 302 /admin/ (иначе /admin без слэша матчится SPA try_files); /admin/
  и /api — SPA-проксирование на app:8000; /internal/ — `internal;`
  (прямой внешний доступ закрыт); /chat и /chat/ — на chainlit:8001
  (WebSocket Upgrade/Connection через map, proxy_buffering off для SSE).
  auth_request → /_auth → app /internal/auth-check: подзапрос отдаёт
  только 2xx/401/403 (иначе nginx вернёт 500); 401 → error_page =302
  /admin/login; 403 (пользователь отключён) остаётся честным 403.
  auth_request_set X-User-Id/Email/Role/Display-Name → proxy_set_header.
  `${...}` — только NETOPS_*; nginx-переменные ($host и т.п.) envsubst
  не трогает.
- **Percent-encoding заголовков (a0d4dce)**: кириллица в X-User-* роняет
  Starlette 500 → backend _user_headers quote()'ит display_name и email
  (email safe="@._-~"), chainlit/auth.py парный unquote(); пустой email
  после unquote → None. Менять только парой.
- **Sub-guard (a0d4dce)**: JWT-sub без валидного числового id → 401
  (backend/app/auth/deps.py + internal.py _load_user_by_id); до Фазы 10
  нечисловой sub давал 500.
- **Фаза 11, compose (4ec5e55)**: сервис chainlit (Dockerfile,
  FASTAPI_INTERNAL_URL=http://app:8000, CHAINLIT_ROOT_PATH=/chat,
  NETOPS_INTERNAL_SERVICE_TOKEN с :? — fail-fast, config.py падает на
  пустом токене, иначе crash-loop; healthcheck /chat/health через
  urllib — alpine без curl) + web (официальный nginx, template через
  envsubst, NETOPS_* как ${VAR:-} — пустой секрет = заголовок не
  передаётся = header-auth выключен, работает логин-форма).
  .env.example / chainlit/.env.example обновлены.
- Тесты: internal_ext_test.py 22/22 (кириллица 204 + encoded-заголовки,
  sub=abc → 401, sub отсутствует → 401, X-Conversation-Id, auth-check,
  /api/auth/me); регресс 16/16, conversations 30/30, audit 23/23,
  settings 25/25. compose config валиден; fail-fast на пустом токене —
  exit 15 с понятным текстом.
- Верификатор PASS (116/116; adversarial: roundtrip quote/unquote
  включая кириллицу, симуляция envsubst-рендера, /chat/health подтверждён
  на исходниках установленного chainlit, grep: прямых вызовов
  LM Studio/Zabbix/VMware в chainlit/ нет).
- Наблюдение (не блокер): huge-int sub (например 20-значный) проходит
  isdigit() и роняет db.get 500 вместо 401 — во всех трёх точках
  (deps.py, internal.py×2); лечится length-cap в guard. Эксплойт требует
  валидного JWT-секрета.
- Live-проверка compose up / nginx / WebSocket — отложена на сервер
  (Docker Desktop не поднимали).

### Этап 12 — техдолг: FIX-01 + FIX-02 (готово, 2026-09-05)
- **FIX-01, мок-режим (1fd555b)**: перехват мок-режима уже существовал
  в wrapper реестра (RBAC до диспетчеризации, аудит после) — дублирующий
  перехват в execute_tool не потребовался. Единственный дефект исправлен:
  статический `from .mock import MOCK_TOOLS` заменён ленивым внутри
  ветки `if settings.mock_mode` — mock.py не загружается в проде.
  tests/test_mock_mode.py (pytest, временная sqlite): 15 моков = реестру,
  ping → фейковый 192.0.2.10 + аудит ok, маркер 'mock-ошибка' →
  status=error, vmware_vms → 4 ВМ (srv-app-01). requirements-dev.txt:
  +httpx, python-multipart, pytest.
- **FIX-02, retry/таймауты chainlit-клиента (коммит ниже)**:
  tenacity>=8.2.0,<9 в requirements (venv: 8.5.0; downgrade с
  транзитивной 9.1.4 безопасен — 9.x нужен chainlit только в tests-extra).
  stream_chat: retry ТОЛЬКО фазы подключения (обычная функция _open(),
  не генератор — tenacity не видит исключений итерации): 3 попытки,
  backoff 2→10с, ретраятся Connect/Read/Write/Pool/Network + 5xx;
  401/413/404/4xx не ретраятся; 404-автоповтор сохранён. Таймауты:
  connect 10с, read 60с между SSE-кадрами (было read=None — вечное
  ожидание), общий 300с на стрим (asyncio.timeout → AGENT_TIMEOUT).
  Обрыв ПОСЛЕ первого события не ретраится (дубли сообщений в БД) →
  STREAM_INTERRUPTED. Сетевой отказ после retry → NETWORK_ERROR
  «Не удалось соединиться с сервером». Тесты sse_parser_test.py
  12+9: m6 ошибка×2→успех на 3-й, m7 все retry пали → NETWORK_ERROR,
  m8 вечный стрим → AGENT_TIMEOUT, m9 503→retry→успех. Верификатор
  PASS: остановленный backend → сообщение за ~11с (не вечный лоадер);
  adversarial: mid-stream ReadTimeout не ретраится (0 дублей), 401 —
  1 вызов (не жжёт попытки), конкурентные вызовы не делят retry-состояние.

### Этап 13 — техдолг: FIX-03 + FIX-04 (готово, 2026-09-05)
- **FIX-03, инвалидация кэша VMware (15c4132)**: helper
  `_invalidate_vmware_cache()` в ui/router.py — безопасный (ImportError →
  debug-лог, прочие Exception → warning, CRUD не ломается), вызывает
  `clear_cache()` из devices/vmware.py. Вызывается во ВСЕХ трёх точках:
  inventory_create, inventory_update, inventory_delete (было только
  удаление — create/update оставляли stale-кэш). Тесты
  test_admin_ui_inventory.py: 35/35 на временной sqlite (проверки:
  _adapters пуст после DELETE, кэш сброшен после create/update,
  сбой upsert не инвалидирует). Верификатор PASS, netops.db не тронута.
- **FIX-04, индикатор агента в Chainlit (15c4132)**: cl.Step
  «Агент думает» в on_message отправляется до stream_chat; первый
  delta-токен → «Ответ формируется...»; каждый tool-вызов → «Шаг N:
  вызов X» (счётчик); финал → «Выполнено N шаг(ов)» / «Ответ готов»;
  BackendError → «Ошибка». tool_result рендерится как раньше
  (adapters.render_event), счётчик не трогает. API проверен по
  установленному chainlit (Step.output — свойство, send/update async);
  py_compile + import-smoke чисты, регрессия backend 21/21.

### Этап 14 — миграция UI, Фаза 12: локальная разработка и мок-тестирование (готово, 2026-09-06)
- **docker-compose.dev.yml**: полный dev-стек без реальных
  LLM/Zabbix/VMware — sqlite (том devdata), NETOPS_MOCK_MODE=true
  (15 мок-инструментов), NETOPS_DEV_MODE=true (логин мимо AD, пароль
  любой), dev-секреты по умолчанию. Сервисы app / seed / chainlit /
  web (nginx :8080) — роут-трип / → nginx → /chat → chainlit →
  backend. `docker compose config --quiet` — валиден. Сброс данных:
  `down -v`.
- **backend/seed_dev.py**: идемпотентные дев-фикстуры. Три
  пользователя ДО первого входа (admin@/engineer@/viewer@example.com;
  в DEV_MODE автовыдача даёт только viewer, роли admin/engineer —
  только сидом), 6 устройств разных типов, 2 диалога (plain и с
  tool_calls JSON в сообщении ассистента), 4 записи аудита
  (ok/error/denied + невидимый маркер seed_dev как guard
  идемпотентности). Повторный прогон ничего не создаёт (проверено).
- **chainlit/dev_sse/*.sse** (4 сценария из migration.md):
  success_tool_call / error_tool_call / timeout / multi_step —
  эталонные SSE-последовательности по фактическому контракту
  chat.py: имена инструментов ping / vmware_vms (реестр tools.py),
  preview = первые 200 симв. результата, timeout.sse — обрыв без
  [DONE] (последний delta оборван посередине).
- **chainlit/dev_sse/scenarios_test.py**: каждый .sse-файл
  прогоняется через реальный parse_sse_line client.py (9/9 PASS).
  Грабля: парсер ожидает строки БЕЗ \n (как aiter_lines httpx) —
  тест срезает перевод строки перед передачей.
- **docs/dev-checklist.md**: чек-лист локальной проверки — 11
  пунктов из migration.md (логин → сохранение аудита) с командами
  запуска, таблицей пользователей и мок-триггерами: host
  `mock-ошибка` → status=error (mock.py), сообщения «ошибка» /
  «лимит» → сценарии LLM (llm/client.py).
- Прогоны: backend-тесты 118/118 (audit 23, conversations 30,
  inventory 35, settings 25, mock_mode 5 через pytest), chainlit
  sse_parser 21/21 + scenarios 9/9, seed идемпотентен; всё на
  временных sqlite, netops.db не тронута.

### Этап 15 — миграция UI, Фаза 13: тестирование (готово, 2026-09-06)
- **backend/tests/test_agent_tools_rbac.py** (13): RBAC-ветки
  execute_tool напрямую — unknown-tool («не найден в системе»,
  статус error + аудит), denied viewer против roles=["admin"]
  (тестовый инструмент через register_tool, удаление из _registry в
  finally; реальные роли denied не достигают — у всех инструментов
  дефолт ["viewer","engineer","admin"], tools.py:87), «Запрещено»
  в исключении → denied, обрезка аудита 4000 / возврата 20000,
  RBAC-проверка до мок-диспетчеризации, аудит-поля (duration_ms
  у denied/unknown = None).
- **backend/tests/test_agent_limit.py** (8): сценарий «лимит»
  через /internal/chat/stream — ровно 20 кадров tool (все
  get_current_time) + 20 tool_result ok, delta ровно одна
  «(Остановлено: лимит шагов агента)», [DONE] последний; БД:
  20 assistant с tool_calls + 20 role=tool, финального
  assistant-сообщения с «Остановлено» НЕТ (текст стримится, но
  final_text="" → не сохраняется — фиксация поведения, не баг);
  аудит: 20 записей ok.
- **chainlit/adapters_test.py** (11): render_event с моками
  cl.Step/user_session (без живой сессии chainlit): _fmt_args
  (пустые → «—», первые 5 пар), _fmt_result ✔/✖, усечение
  MAX_PREVIEW=400 + суффикс « …», delta/error/done → None без
  Step, tool → Step «Инструмент: …» + send, tool_result →
  update + сброс current_step, orphan-tool_result → свой Step.
- **backend/tests/test_e2e_flow.py** (29): сквозной тест всех 14
  шагов migration.md на TestClient (временная sqlite, мок-режим):
  логин admin → dashboard → инвентарь + partial → создание
  устройства (flash «Устройство добавлено» + Device в БД) → аудит
  → чат «пинг» через /internal/chat/stream (кадры tool ping →
  tool_result «Обмен пакетами» → финальный delta → [DONE]) →
  диалог в истории (/admin/conversations + details) → вызов ping
  в аудите (фильтр dialog={conv_id}) → viewer: 403-матрица
  (аудит/инвентарь GET+POST/диалоги/настройки), чат viewer
  работает (все инструменты допускают viewer — факт tools.py:87).
- **run_tests.py** (корень): консолидированный runner 16 наборов
  (backend-венв: 10 существующих + 3 новых + pytest mock_mode;
  chainlit-венв: sse_parser + scenarios + adapters) — subprocess
  с PYTHONIOENCODING=utf-8, MISSING не провал, отсутствие венва →
  exit 2 с инструкцией. Полный прогон 16/16 OK (~1 мин).
- **docs/dev-checklist.md**: ручной чек-лист Фазы 13 (создание
  устройства через модалку, вход viewer, индикатор FIX-04 «Агент
  думает» → «Шаг N» → «Выполнено N шаг(ов)», nginx/WebSocket — на
  сервере); исправлена строка аудита: только admin (engineer →
  403, require_roles_page(Role.admin) в ui/router.py).
- Прогоны: 16/16 наборов, 61 новая проверка (13+8+29+11), двойные
  прогоны стабильны, регрессий нет (118 прежних зелёные), netops.db
  не тронута (байт-в-байт). Грабля: консоль Windows cp1251 — новым
  тестам нужен sys.stdout.reconfigure(encoding="utf-8").

### Этап 17 — паритет новой админки со старым SPA: пользователи + bulk-инвентарь (готово, 2026-09-07)
- **Контекст**: после Фазы 14 (live-проверка на сервере) пользователь
  обнаружил, что в новой админке нет двух возможностей старого SPA:
  управление ролями/активностью пользователей и массовое вкл/выкл
  устройств. Backend API уже всё умел (PATCH /api/users/{uid},
  PATCH /api/devices/bulk) — недостало только UI. Живые фиксы
  прод-окружения по ходу: WEB_PORT (6b47e35), nginx Host
  $http_host (6b447e5), absolute_redirect off (9b2d8ed — редирект
  /chat→/chat/ терял нестандартный порт), проверка Zabbix на
  /api_jsonrpc.php вместо корня (91ac1e0 — был JSONDecodeError
  на HTML-ответе).
- **Страница «Пользователи» /admin/users** (admin-only, в navbar
  из NAV_ITEMS): таблица (username+display_name, роль-select
  hx-trigger=change, toggle блокировки, granted_by/at);
  POST /admin/users/{uid}/role и /{uid}/active — та же логика,
  что API: само-смена роли и само-деактивация запрещены (400),
  своя строка в UI disabled; POST /users/add (нормализация
  _normalize, дубль 409, PRG-303, inline-форма с повторным
  рендером при ошибках). Верификатор: 34 проверки — PASS.
- **Чекбоксы инвентаря + bulk**: в таблице (партиал) колонка
  чекбоксов admin-only + «выбрать все»; панель «Выбрано: N» +
  кнопки Вкл/Выкл выбранные (confirm, невидимая form, JS
  InvBulk — делегирование change на document, пересчёт по
  htmx:afterSwap); POST /admin/inventory/bulk (ids строкой
  «1,3» или многократным полем, enabled=on|off, пусто → 400,
  update + clear_cache/FIX-03, PRG-303 c flash «Обновлено N
  устройств» cо склонением). Верификатор: 20/20 тест +
  adversarial (инъекции, orphan-ids, idempotency) — PASS.
- **test_admin_ui_inventory_bulk.py** (20) в runner — 18/18
  (~1 мин 13с). Tailwind.css пересобран (классы новых
  шаблонов подтверждены grep'ом в сборке).
- Верификатор нашёл предсуществующий (не регресс этой работы)
  дефект: int64-overflow uid/ids («999…9» × 20) → 500 на
  /admin/users/*, /admin/inventory/bulk и старых API-роутах —
  кандидат в отдельный FIX-NN (len-cap, как в jwt sub-guard).

### Этап 16 — миграция UI, Фаза 14: замена старого интерфейса (готово, 2026-09-07)
- **config.py**: поле `use_new_ui: bool = False` (env
  NETOPS_USE_NEW_UI) — флаг Фазы 14; false — старый SPA основной,
  true — новый UI (лендинг) на `/`.
- **frontend/index.html → frontend/legacy/index.html** (git mv,
  история сохранена): старый SPA теперь живёт под `/legacy`
  (mount StaticFiles в main.py; nginx: try_files с fallback
  /legacy/index.html — SPA-роутинг сохранён). Удаление — только
  после live-подтверждения на сервере (Фаза 16), по правилам
  migration.md.
- **ui/home.py** (новый): `GET /` — use_new_ui=false → 303 на
  /legacy/; true → лендинг templates/pages/landing.html (3
  карточки: Чат /chat, Админка /admin/, Старый интерфейс /legacy/;
  публичен, standalone как login.html; is_dev → CDN-иначе
  локальная сборка tailwind.css).
- **nginx/default.conf.template**: `location = /` → proxy app:8000
  (главную решает backend по флагу); `location /legacy/` —
  статика старого SPA; прочий `location /` → proxy без try_files
  (статики на / больше нет, неизвестные пути — честный 404 от
  FastAPI).
- **login.html + router.py**: is_dev-паттерн подключении Tailwind
  (ранее login.html — всегда CDN, артефакт Фазы 13) — во всех
  4 рендерах (GET + POST 401/403×2).
- **docker-compose.dev.yml**: NETOPS_USE_NEW_UI=true (dev-стек
  проверяет новый путь); прод-compose берёт флаг из .env.
- **tests/test_ui_switch.py** (8): false → GET / = 303 /legacy/ +
  GET /legacy/ = 200 + 404 StaticFiles на несуществующий файл;
  true → лендинг со ссылками /chat, /admin/, /legacy/; cache_clear
  переключает флаг без рестарта (home.py читает get_settings()
  в рантайме); GET /admin/login = 200. В runner: 17/17 OK (~1 мин).
- **.env.example**: NETOPS_USE_NEW_UI=false задокументирован.
- Прогоны: верификация PASS (состав 13 файлов, порядок
  роутер-до-mount, raw-ASGI traversal, envsubst-рендер nginx,
  compose-валидация, изоляция БД — netops.db не тронута).

### Этап 18 — SNMP: адаптер v2c + скилл принтеров (готово, 2026-09-08)
- **devices/snmp.py** (новый): pysnmp 7.1.x — полностью asyncio-API
  (`hlapi.v1arch.asyncio`), синхронных getCmd/walkCmd больше нет.
  Обёртки `snmp_get`/`snmp_walk` через `asyncio.run()` на вызов
  (в backend нет гарантии event-loop в потоке вызывающего); SnmpDispatcher
  на каждый вызов (создание ~0.02 мс, потокобезопасно). SNMP-типы 7.x —
  не int-подклассы: явная конвертация `int(...)`. Только GET/GETBULK —
  read-only. Таймаут обязателен (UdpTransportTarget.create, retries=1).
  Грабли: prettyPrint бинарного OctetString = hex ('0x1800'), пустого =
  '' — критично для битмаски ошибок принтера; WALK с
  lexicographicMode=False — авто-остановка на выходе из поддерева.
- **models.py / main.py**: DeviceType.printer; Device.snmp_version
  (default "2c") / snmp_community (default "public"). Авто-миграция без
  alembic при старте: `_ensure_device_columns` (inspect → ALTER TABLE
  ADD COLUMN, sqlite и postgres, идемпотентно) + `_ensure_printer_enum_value`
  (postgres ENUM: ALTER TYPE ADD VALUE IF NOT EXISTS в AUTOCOMMIT-
  соединении — в транзакции нельзя). Прод-БД без ручных действий.
- **Админка**: SNMP-секция в форме инвентаря (select snmp_version +
  input snmp_community, дефолты при пустых); тип printer в select
  (автоматически из _INV_DEVICE_TYPES). seed_dev.py: hq-printer-1.
- **Инструменты агента** (tools.py, после zabbix-блока): snmp_info
  (sysDescr/sysName/sysContact/sysLocation + uptime «X д Y ч Z мин»,
  TTL 120), snmp_interfaces (ifTable по ifIndex: имя/MTU/скорость/
  admin+oper status, TTL 120), snmp_walk (произвольный OID, TTL 0 —
  скилл для отладки/вендорских OID), printer_info (TTL 120 — скилл
  принтеров: serial, счётчик распечатанных страниц prtMarkerLifeCount,
  тонер % (уровень/maxCapacity, -3=неизвестно -> null), статус
  hrPrinterStatus, ошибки hrPrinterDetectedErrorState; device='all' —
  все принтеры, недоступный не валит агрегат: его item = {error}).
  Community/port из Device; тип-гвард _snmp_target — только
  printer/eltex/mikrotik/usergate.
- **Битмаска ошибок (RFC 2790, грабли верификатора)**: BITS
  нумеруются MSB-first — «бит 0 = старший бит первого байта»;
  декод по байтам `byte & (0x80 >> i)`, полная 15-битная карта
  (lowPaper..overduePreventMaint). Первая версия декодила LSB-first
  по урезанному списку — каждый бит интерпретировался неверно
  (замятие показывалось бы «нет бумаги»); поймал верификатор,
  фикс + независимые прогоны декодера.
- **Честность по сканам**: счётчик отсканированных страниц вендорозависим
  (стандартный Printer-MIB его не даёт; нужен enterprise-MIB HP/Kyocera
  и т.п.) — printer_info отдаёт pages_scanned=null с пояснением, пункт 14
  SYSTEM_PROMPT прямо запрещает выдумывать значение.
- **SYSTEM_PROMPT**: пункт 14 — SNMP-инструменты, скилл принтеров,
  honest-note про сканы.
- **Тесты**: test_snmp_migration.py (16: свежая БД/дефолты raw-INSERT,
  идемпотентность, «старая» БД с ALTER, CRUD printer, printer в select),
  test_snmp_tools.py (22: реестр+TTL, моки single/all, реальная логика
  printer_info на подменённом walk — тонер 25%, битмаска 0x1800 ->
  noToner+doorOpen, 5 прямых проверок декодера BITS, RBAC viewer,
  маркер mock-ошибка, тип-гвард vcenter, инвентарь). test_mock_mode.py:
  EXPECTED_MOCK_TOOLS 15 -> 19. Runner: 20 наборов.
- **Моки**: 4 (snmp_info/snmp_interfaces/snmp_walk/printer_info) в
  MOCK_TOOLS — формы JSON идентичны реальным инструментам.
- **Прогоны**: верификация — первый прогон FAIL (битмаска, см. выше),
  после фикса — повторная верификация PASS; runner 20/20, SNMP-наборы
  22/22 и 16/16, netops.db не тронута (md5 до/после).
- **requirements.txt**: pysnmp>=7.1. **docs/dev-checklist.md**: секция
  «Этап SNMP» (авто-прогон + ручная таблица живого SNMP-агента).

### Этап 19 — принтеры: композитный отчёт + SNMP-Discovery (готово, 2026-09-09)
- **Повод (живое тестирование 4 принтеров)**: модель на «дай информацию
  по принтерам» опросила только 2 (лимит 20 шагов) и ВЫДУМАЛА данные по
  остальным. Плюс 100+ принтеров нельзя добавить руками.
- **get_printers_report** (tools.py, is_composite, TTL 120): все
  включённые принтеры за ОДИН вызов агента. Параллельный опрос
  ThreadPoolExecutor(16) — безопасно: инструмент живёт в asyncio.to_thread
  (chat.py), в потоке нет event-loop, каждый воркер свой asyncio.run +
  dispatcher (snmp.py потокобезопасен по построению); 100 принтеров ≈
  12–15 с вместо 200. Компакт: одна строка на принтер (статус/страницы/
  тонер %/ошибки/serial), сводка total/healthy/with_problems/unreachable,
  недоступные отдельным списком (SnmpError на первом walk — остальные
  не делаем). Бюджет _COMPACT_BUDGET=15000 < MAX_RESULT считается по
  ИТОГОВОМУ JSON (unreachable включён): problem-строки деградируют
  полная → без serial → имя в remaining_problems, здоровые — до
  бюджета, остаток списком имён в remaining_healthy → JSON валиден при
  100+ (в т.ч. 100 одновременно проблемных — находка верификатора,
  76+ полных problem-строк сами по себе превышали бы MAX_RESULT).
  Тонер < 15% или errors непусты = «проблемный» (сортировка в начало).
- **SYSTEM_PROMPT**: правило 2 усилено («нет в результате/ошибка —
  "нет данных", не додумывать»), правило 14 переписано (отчёт по всем =
  один get_printers_report, НЕ printer_info по очереди), build_system_prompt
  упоминает в инвентаре.
- **Discovery** (devices/printer_discovery.py + api/devices.py
  sync_printer_discovery + ui/router.py POST /inventory/discover-printers):
  форма в инвентаре (ОДНА подсеть, маска до /22 — ≤1024 адресов,
  community default public, порт, таймаут; клампы). Критерий принтера:
  непустой
  walk серийной таблицы 43.5.1.1.17 (авторитетно — свитчи её не
  реализуют), фолбэк — keywords printer-моделей в sysDescr. Найденные
  добавляются ВЫКЛЮЧЕННЫМИ: type=printer, группа «Принтеры»,
  source="snmp" (amber-бейдж в таблице, опция фильтра), community из
  формы, имя _norm_name(sysName) или printer-{ip}, коллизия → -{ip}.
  Повторный запуск: тот же host → обновить community/description
  (имя НЕ трогаем — мог переименовать пользователь), чужой host → skip,
  пропавших НЕ отключаем (могли просто выключиться) — отличие от
  Zabbix-синка, у которого gone → enabled=False.
- **Тесты**: test_printers_report.py (14: реестр/схема/TTL/композит/роли,
  мок, реальная логика на подменённом walk — поля/тонер/битмаска/
  недоступный/сводка/сортировка, 130 принтеров — бюджет+remaining,
  пустой инвентарь, eltex не опрашивается; грабля: wrapper кэширует
  результат — между сценариями чистить _caches), test_printer_discovery.py
  (26: parse_subnets все ветки, probe_ip критерий, discover счётчики,
  sync БД-поведение+re-run+skipped, UI-эндпоинт flash/бейдж (в
  partial, не в странице — таблица HTMX-load)/RBAC). test_mock_mode:
  19→20. Runner: 22 набора.
- **Грабли этапа**: (1) _registry хранит wrapper — вызов в тестах
  минует mock только при settings.mock_mode=False вручную; (2) кэш TTL
  120 живёт между тест-сценариями → _caches[name].clear() (НЕ pop:
  KeyError в wrapper); (3) таблица инвентаря в HTML страницы — только
  «Загрузка…», реальные строки — GET /admin/inventory/partial/table.

1. **Zabbix 6.2**: токен работает только параметром `auth` в теле JSON-RPC
   (заголовок Authorization: Bearer — не сработал); URL — http, не https;
   sortfield "clock" у problem.get запрещён; selectHosts у problem.get молча
   игнорируется → хосты через trigger.get.
2. **gpt-oss-20b**: склонна к отказам «по безопасности» — снято формулировкой
   промпта про авторизованных сотрудников; не делает обход устройств по инструкции —
   поэтому device="all"; правило «вызывай параллельно» помогает частично;
   русский понимает хуже Qwen3.
3. Модель не знает текущего времени — подставляем в промпт + инструмент get_current_time.
4. SSE ломается при буферизации: nginx `proxy_buffering off` + заголовок
   X-Accel-Buffering: no.
5. Из контейнера Docker `localhost` хоста не виден — нужен
   host.docker.internal:host-gateway (extra_hosts).
6. Данные внутри ВМ (IP, занятость дисков) требуют VMware Tools — без них
   адаптер отдаёт fallback с пометкой. Это нормальное поведение.
7. PowerShell: `curl` — псевдоним Invoke-WebRequest, использовать `curl.exe`;
   JSON в кавычках часто ломается — для диагностики лучше Python-скрипты.
8. Если модель стабильно не выполняет какое-то поведение по промпту — зашивать
   его в инструмент, а не в инструкцию (пример: device="all").
9. **Все инструменты должны возвращать JSON-строку**: если инструмент возвращает
   сырой текст (например, вывод ping из консоли), json.loads() падает с ошибкой
   "Expecting value: line 1 column 1". Решение: оборачивать в json.dumps().
10. **vCenter ≠ ESXi**: vCenter — сервер управления, у него нет аппаратных сенсоров.
    Для vCenter проверяем: статус управляемых хостов, датасторы, события.
    Сенсоры (температура, диски) актуальны только для standalone ESXi.
11. **Не опрашивать хосты под vCenter отдельно**: если vCenter в инвентаре,
    данные по vmh03/vmh05 приходят через него. Standalone (vmh08) — отдельно.
12. **Zabbix — только bulk**: НЕ опрашивать каждое Zabbix-устройство отдельно.
    Один вызов problem.get без hostids возвращает все алерты инфраструктуры.
13. **Большие JSON-массивы ломают LLM**: если инструмент возвращает тысячи строк,
    вывод упирается в MAX_RESULT, JSON обрывается, модель галлюцинирует.
    Решение: агрегация на бэкенде, фильтрация по порогам, MAX_RESULT=20000.

## 7. Текущий набор инструментов агента

### Базовые
| Инструмент | TTL | Описание |
|---|---|---|
| `get_current_time` | 0 | Текущее время сервера |
| `ping` | 60s | ICMP-проверка доступности (4 пакета) |
| `list_devices` | 300s | Список активных устройств инвентаря |
| `list_groups` | 300s | Группы устройств |

### VMware (все поддерживают device="all" и фильтры host/vm)
| Инструмент | TTL | Описание |
|---|---|---|
| `vmware_vms` | 120s | ВМ: питание, IP, CPU, память |
| `vmware_hosts` | 120s | ESXi-хосты: состояние, CPU, память |
| `vmware_snapshots` | 120s | ВМ со снапшотами |
| `vmware_datastores` | 300s | Датасторы: ёмкость, занято, свободно |
| `vmware_vm_disks` | 300s | Диски ВМ: VMDK + гостевая ОС |
| `vmware_host_networks` | 300s | vmnic/vmk/vSwitch |
| `vmware_vm_networks` | 300s | Сетевые адаптеры ВМ |
| `vmware_events` | 60s | Журнал событий (фильтр по ВМ/хосту, часы) |
| `vmware_host_sensors` | 120s | Аппаратные сенсоры ESXi |

### Zabbix
| Инструмент | TTL | Описание |
|---|---|---|
| `zabbix_problems` | 60s | Активные проблемы. Без device = вся инфраструктура |
| `zabbix_items` | 120s | Последние значения метрик устройства |
| `zabbix_history` | 60s | История метрики (до 7 дней) |

### SNMP (v2c, read-only; принтеры + сетевые eltex/mikrotik/usergate)
| Инструмент | TTL | Описание |
|---|---|---|
| `snmp_info` | 120s | sysDescr/sysName/sysContact/sysLocation/uptime |
| `snmp_interfaces` | 120s | ifTable: имя/MTU/скорость/статусы |
| `snmp_walk` | 0 | Произвольный OID (GETBULK-обход) |
| `printer_info` | 120s | Скилл принтеров: serial, распечатанные страницы, тонер %, статус, ошибки; device="all" |

### Композитные (is_composite=True)
| Инструмент | TTL | Описание |
|---|---|---|
| `get_device_full_health` | 120s | Полная диагностика одного устройства |
| `get_infrastructure_health` | 120s | Обзор проблем всей инфраструктуры |
| `get_printers_report` | 120s | Все принтеры за ОДИН вызов: 1 строка/принтер + недоступные; бюджет 15К против обрезки MAX_RESULT |

**MAX_RESULT** = 20000 символов (обрезка вывода инструмента).

### Правила SYSTEM_PROMPT (кратко):
- Легитимный внутренний ассистент; сразу вызывать инструменты; не выдумывать.
  Нет устройства в результате/ошибка — «нет данных», не додумывать (пр. 2).
- device — точное имя или "all"; не говорить «нет устройства» без list_devices.
- Ошибки дословно + повтор по списку имён; не раскрывать внутренние сервисы.
- Параллельные вызовы; standalone vs vcenter-хосты.
- «всё со всех» → device="all"; Zabbix-устройства → zabbix_*.
- **«что болит», «есть ли проблемы» → get_infrastructure_health** (НЕ zabbix_problems отдельно).
- **«проблемы по [устройство]» → get_device_full_health**.
- Анализ порогов: датасторы <15% = критично; CPU/RAM >85% = риск; снапшоты = долг.
- Zabbix: ОДИН bulk-вызов, не опрашивать устройства поштучно.
- vCenter: проверять хосты/датасторы/события; сенсоры — только для standalone ESXi.
- **Принтеры и сетевые → SNMP-инструменты** (snmp_info/snmp_interfaces/
  snmp_walk/printer_info). Отчёт по ВСЕМ принтерам — ОДИН вызов
  get_printers_report (не printer_info по очереди); printer_info — только
  подробности по одному. Счётчик сканов вендорозависим — честно сообщать,
  не выдумывать; распечатанные страницы = pages_printed.

build_system_prompt добавляет: текущее время + список включённых устройств.

## 8. Известные недочёты и несоответствия

- Пароли устройств в БД открытым текстом — перед продом шифровать (Fernet + ключ из env).
- В UI-форме есть типы edgecore/snr/aruba, но в DeviceType их пока нет
  (шаг с netmiko не применялся) — добавляются одним изменением enum вместе с SSH-этапом.
- Нет refresh-токенов (JWT на 12 часов).
- netmiko/net_show/ssh_cli.py — НЕ вносились в код (шаг пропущен осознанно).
- Ключи адаптера VMware (cpu_usage_percent, free_percent) зависят от реализации
  vmware.py — при изменении адаптера нужно обновить _get_free_percent и пороги.
- get_printers_report: бюджет _COMPACT_BUDGET не деградирует список
  unreachable и не учитывает имена в remaining_* — при 200 недоступных
  или ~200 проблемных с длинными (64-симв.) именами итоговый JSON может
  превысить MAX_RESULT (обрезка -> битый JSON). За пределами текущего
  парка (~130 принтеров) не срабатывает; при росте — деградация
  unreachable + учёт rem-имён в бюджете.

## 9. Дорожная карта — что дальше

### Текущий фокус: миграция UI (migration.md, 16 фаз)
- Готово: Фазы 0–14 (Этапы 7–16 в §5) — снимок состояния, мок-режим,
  /internal/chat/stream, каркас админки /admin/*, контент: инвентарь,
  аудит, настройки, история диалогов; Chainlit-чат через
  /internal/chat/stream + авторизация (auth-check для nginx,
  header-auth, логин-форма); nginx reverse-proxy (auth_request,
  X-User-* заголовки, WebSocket/SSE, envsubst-шаблон); docker-compose
  с сервисами chainlit + web (nginx), fail-fast на пустом токене;
  Фаза 12 — dev-стек docker-compose.dev.yml + seed_dev.py +
  SSE-сценарии + чек-лист (docs/dev-checklist.md); Фаза 13 —
  тестирование (test_agent_tools_rbac 13, test_agent_limit 8,
  adapters_test 11, test_e2e_flow 29, runner run_tests.py, ручной
  чек-лист dev-checklist.md); Фаза 14 — замена старого интерфейса:
  старый SPA на /legacy, лендинг / с выбором (чат/админка/legacy),
  флаг NETOPS_USE_NEW_UI (false по умолчанию), nginx location = /
  -> backend, test_ui_switch 8, runner 17/17.
- Следующие: Фазы 15–16 (документация, готовность). Старый SPA
  (frontend/legacy/index.html) удаляется только после
  live-подтверждения на сервере. Live-проверка nginx/WebSocket и
  docker-стека отложена на сервер.
- Этап 18 (SNMP + скилл принтеров) — готово, см. §5 и §7; runner 20/20.
- Этап 19 (композитный отчёт принтеров + SNMP-Discovery) — готово,
  см. §5 и §7; runner 22/22. Живое подтверждение: отчёт по 4 реальным
  принтерам одним вызовом, discovery по принтерным подсетям.

### Ближайший шаг (сделано — Этап 18, SNMP): краткий итог
- Прямой SNMP для ручных устройств реализован (v2c, pysnmp): snmp_info /
  snmp_interfaces / snmp_walk / printer_info (скилл принтеров, счётчик
  распечатанных страниц, тонер, статус/ошибки; device="all").
- Community хранится в инвентаре (snmp_version/snmp_community, авто-миграция
  при старте). Счётчик сканов вендорозависим — отдаётся null с пояснением.
- Возможное развитие: SNMPv3 (authPriv), enterprise-MIB моделей принтеров
  для счётчика сканов (HP/Kyocera), edgecore/snr/aruba в DeviceType при
  SSH-этапе (ENUM уже расширяется по образцу printer).

### Затем
- SSH CLI (netmiko) для Eltex/EdgeCore/SNR/Aruba с белым списком read-only команд
  (show/display/ping/traceroute). Для Aruba сначала выяснить тип: контроллер
  (aruba_aos_8) или коммутаторы (AOS-CX/ProCurve) — от этого зависит профиль.
- UserGate — REST API, отдельный адаптер.
- RAG по документации/runbook'ам: ChromaDB/Qdrant + эмбеддинги (bge-m3 /
  Qwen3-Embedding-0.6B). Инструмент: search_internal_docs(query).
- APScheduler: утренний health-check, сводные отчёты, разбор алертов.
  Утренний отчёт = вызов get_infrastructure_health + доставка в Telegram.
- Прод на Ubuntu: docker compose, реальный AD (убрать DEV_MODE), TLS,
  шифрование паролей, alembic-миграции, сетевая сегментация (раздел 3).

### Предложения по улучшению UI и бэкенда

Быстрые (по 1–2 часа, эффект сразу виден):
- Кнопка «Стоп» при генерации: AbortController на фронте (frontend/index.html,
  send()) — сейчас длинный поток нельзя прервать. finally в generate() уже
  сохраняет финальный ответ, бэкенд переживёт disconnect.
- Markdown в ответах: ассистент пишет markdown, а addMsg() рендерит через
  textContent — списки и код отображаются сырыми. Подключить marked (один
  скрипт) только для role=assistant.
- Очередь к LLM видна пользователю: LLMService.slot() (llm/client.py:20,
  Semaphore=1 + счётчик ожидающих) написан, но chat.py его НЕ вызывает —
  второй запрос идёт в LM Studio в обход очереди и пользователь ждёт вслепую.
  Обернуть вызов create() в slot() и слать SSE-событие «ожидание очереди».
- Экспорт диалога в markdown (кнопка в шапке чата) — для отчётов по инцидентам.
- CSV-импорт инвентаря кнопкой в UI (сейчас только скрипт seed_devices.py).
- Token/Step Counter в UI: сколько шагов (tool calls) и токенов съел ответ.
  Критически важно для отладки промптов и оценки нагрузки.
- Сохранение reasoning/thought в AuditLog: если модель отдаёт chain-of-thought,
  писать его в аудит. Бесценно при разборе инцидентов.

Средние:
- Новые zabbix-инструменты: zabbix_alerts (история уведомлений за период),
  zabbix_hosts/макросы. problem.acknowledge — уже запись: только после
  RBAC-гейта по ролям.
- Счётчик шагов/токенов в UI (сколько шагов сделал агент, длительность) —
  полезно для отладки промптов и оценки нагрузки.
- Soft Truncation: вместо жёсткого MAX_RESULT возвращать
  {"status": "partial", "returned": N, "total": M, "hint": "уточните фильтр"}.
  Модель сама сузит запрос (self-correction).

Крупные:
- Доставка отчётов в Telegram/почту (развитие APScheduler-пункта): утренний
  обход уходит в канал дежурных, а не только лежит в чате.
- Шифрование паролей устройств (Fernet, ключ из env) — дублирую из §8,
  сделать ДО прода: в БД сейчас открытый текст (models.py, поле password).
- Тёмная тема в UI (цвета захардкожены, css-переменных нет) — косметика,
  но дешёвая: вынести палитру в переменные при случае других правок CSS.

### Эволюция инструментов (Концепция развития)

#### 1. Композитные инструменты (реализовано в Этапе 6)
- get_device_full_health: ping + Zabbix + датасторы + CPU/RAM + снапшоты + события.
- get_infrastructure_health: bulk Zabbix + VMware (только управляющие устройства).
- Принцип: модель делает ОДИН вызов, инструмент сам агрегирует и фильтрует.

#### 2. Аналитика временных рядов (Time-Series Analytics)
- analyze_metric_trend(itemid, hours): бэкенд считает min, max, avg, p95, std_dev,
  аномалии. Модель получает готовые факты, а не сырые тысячи точек.
- predict_capacity_exhaustion(datastore): линейная регрессия → «место закончится
  через N дней».

#### 3. Сетевая диагностика и L2/L3
- traceroute / mtr: поиск обрывов на магистрали.
- dns_lookup / reverse_dns: разрешение коллизий имён (Zabbix vs VMware vs AD).
- mac_to_port(mac) (после SNMP/CLI): поиск физического порта.

#### 4. Безопасность CLI (подготовка к Netmiko/SSH)
- Dry-Run режим: модель генерирует команду, инженер жмёт "Approve" в UI.
- Regex White-List: execute_show_command(device, cmd) валидирует cmd на бэкенде.
  Разрешено только show/display. Попытка conf t отклоняется до отправки.

#### 5. RAG и базы знаний
- search_internal_docs(query): поиск по runbook'ам, Wiki, базам знаний.
  Модель читает внутренние инструкции при специфичных ошибках.

#### 6. Безопасность SSH/CLI
- Dry-Run: глобальный флаг для мутирующих/SSH команд. Модель генерирует команду,
  кидает в чат как "Предложение", инженер жмёт "Approve" в UI.
- Regex White-List: инструмент execute_show_command(device, cmd) на бэкенде
  жёстко валидирует cmd. Разрешено только show *, display *. Попытка conf t
  отклоняется до отправки на коммутатор с падением в AuditLog.

## 10. Локальный запуск (dev, Windows)

```powershell
cd E:\netops-llm\backend
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload --port 8000
# UI: http://127.0.0.1:8000, вход admin@id.samges.ru, пароль любой (DEV_MODE)