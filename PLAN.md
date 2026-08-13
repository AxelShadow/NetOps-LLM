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

## 6. Выученные грабли (важно!)

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

### Композитные (is_composite=True)
| Инструмент | TTL | Описание |
|---|---|---|
| `get_device_full_health` | 120s | Полная диагностика одного устройства |
| `get_infrastructure_health` | 120s | Обзор проблем всей инфраструктуры |

**MAX_RESULT** = 20000 символов (обрезка вывода инструмента).

### Правила SYSTEM_PROMPT (кратко):
- Легитимный внутренний ассистент; сразу вызывать инструменты; не выдумывать.
- device — точное имя или "all"; не говорить «нет устройства» без list_devices.
- Ошибки дословно + повтор по списку имён; не раскрывать внутренние сервисы.
- Параллельные вызовы; standalone vs vcenter-хосты.
- «всё со всех» → device="all"; Zabbix-устройства → zabbix_*.
- **«что болит», «есть ли проблемы» → get_infrastructure_health** (НЕ zabbix_problems отдельно).
- **«проблемы по [устройство]» → get_device_full_health**.
- Анализ порогов: датасторы <15% = критично; CPU/RAM >85% = риск; снапшоты = долг.
- Zabbix: ОДИН bulk-вызов, не опрашивать устройства поштучно.
- vCenter: проверять хосты/датасторы/события; сенсоры — только для standalone ESXi.

build_system_prompt добавляет: текущее время + список включённых устройств.

## 8. Известные недочёты и несоответствия

- Пароли устройств в БД открытым текстом — перед продом шифровать (Fernet + ключ из env).
- В UI-форме есть типы edgecore/snr/aruba, но в DeviceType их пока нет
  (шаг с netmiko не применялся) — добавляются одним изменением enum вместе с SSH-этапом.
- Управление пользователями: вкладка в UI + API /api/users (доступ выдаёт админ).
- Нет refresh-токенов (JWT на 12 часов).
- netmiko/net_show/ssh_cli.py — НЕ вносились в код (шаг пропущен осознанно).
- Ключи адаптера VMware (cpu_usage_percent, free_percent) зависят от реализации
  vmware.py — при изменении адаптера нужно обновить _get_free_percent и пороги.

## 9. Дорожная карта — что дальше

### Ближайший шаг: прямой SNMP для ручных устройств
- Форма: версия SNMP (v2c community / v3), поля в Device (snmp_version и т.п.).
- Библиотека pysnmp (чистый Python, работает на Windows).
- Инструменты: snmp_info (sysDescr/uptime), snmp_interfaces (разбор ifTable),
  snmp_walk (сырой обход OID). Только GET/WALK — read-only по природе.
- В компании используется SNMPv2c.
- Регистрация через @register_tool: декоратор + cache_ttl=120.

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