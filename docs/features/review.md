# Ревью диффа: RAG по runbook'ам + утренние отчёты (SMTP/планировщик)

> Роль: `reviewer` (`.dsh/roles/reviewer.md`) · Ветка: `main` · HEAD: `4c7214f`
> Объект ревью: рабочее дерево поверх `4c7214f` (незакоммиченные изменения:
> 19 изменённых + 21 новый файл, `git status --porcelain` = 40 записей).
> Контракт: [`rag-and-scheduler-design.md`](rag-and-scheduler-design.md) §4–§5.
> Входы: отчёты `qa-report.md` (принять с оговорками) и `security-verdict.md`
> (APPROVE WITH CONDITIONS, C1–C8).
> Ревьюер не автор изменения; код, тесты и конфигурацию не правил, коммитов не делал.
> Значения секретов в отчёте не приводятся (только имена/пути/`<redacted, N chars>`).

## Вердикт

**APPROVE WITH COMMENTS (принять с замечаниями). Блокеров нет.**

Основание: контракт §4–§5 выполнен, все 38 наборов `run_tests.py` зелёные,
`docker compose config` для обоих файлов — код 0, дифф не добавляет находок
secret-scan и не выходит за согласованные зоны, чужие тесты не ослаблены.
Замечания ниже — не блокирующие; M1 желательно закрыть до включения RAG в проде
(правка на 2 строки), M2 — открытое условие `security-guard` C3 (до
`NETOPS_SCHEDULER_ENABLED=true` в проде).

### Замечания

| # | Severity | Файл:строка | Что не так | Кому / когда |
|---|---|---|---|---|
| M1 | major | `backend/app/ui/router.py:860-864`, `:824-828`; `backend/app/rag/indexer.py:200,202` | GET `/admin/settings/partial/rag` — `async`-обработчик, но `_rag_status_safe()` вызывает синхронный `rag_service.get_status()` → `indexer.index_status()`, а там **сетевые** вызовы `store.health()` и `store.collection_info()` (httpx, таймаут 10 с в проде / 2 с в mock). При включённом RAG и недоступном Qdrant открытие `/admin/settings` админом блокирует event loop до ~20 с — встаёт весь чат для всех пользователей. Блокирующие операции остальных кнопок уже корректно вынесены в `asyncio.to_thread` (`:773`, `:799`, `:880`), статус — нет. | `frontend-dev-admin`; исправить до включения `NETOPS_RAG_ENABLED=true` в проде (лучше — до коммита: `return await asyncio.to_thread(_rag_response_safe, ...)` или обернуть только `_rag_status_safe`) |
| M2 | major (перенесённое условие security C3) | `backend/app/scheduler/jobs.py:62,175-187,238`; `backend/app/scheduler/reports.py:328-352` | C3 закрыт частично: `_truncate_body` ограничивает **итоговое тело** 100 000 символов, но вызывается **после** `render_template` (`jobs.py:238`: `_truncate_body(_render(...))`). Стоимость самого рендера (циклы/`range`/`*` в шаблоне из админки) по-прежнему не ограничена временем и памятью — DoS-вектор из вердикта security остаётся. Ограничение размера результата — полезно, но условие «ограничить рендер» не выполнено. | `backend-dev-scheduler`; до `NETOPS_SCHEDULER_ENABLED=true` в проде (рендер в отдельном потоке с таймаутом либо явный запрет циклов/больших операций в шаблоне) |
| m1 | minor | `backend/app/templates/components/settings/report_status.html:15` | При `scheduler.enabled=false` (а env=true) статус показывает «выключен (NETOPS_SCHEDULER_ENABLED=false)» — причина неверна (выключено в админке, а не на сервере). Ниже выводится корректный `status.message`, поэтому вводит в заблуждение только первая строка. | `frontend-dev-admin` |
| m2 | minor | `backend/app/ui/router.py:451-476` vs `backend/app/scheduler/delivery.py:76-97` | Два разных валидатора SMTP-хоста: UI принимает «сырой» IPv6-литерал (`host.count(":") > 1` → `ipaddress.IPv6Address` → True, `router.py:~470`), а `delivery._validate_smtp_host` его отвергает (regex без двоеточий, `delivery.py:93`). Значение сохраняется из формы, но отправка всегда падает с «некорректное имя хоста». | `frontend-dev-admin` + `backend-dev-scheduler`; использовать один валидатор из `delivery` в UI |
| m3 | minor (условие security C5) | `backend/app/ui/router.py:489-505` | После ротации `NETOPS_ENCRYPTION_KEY` форма показывает «Пароль задан» без предупреждения, что секрет не расшифровывается (в лог пишется warning, в UI — нет). Условие C5 в силе. | `frontend-dev-admin`; до включения отчётов в проде |
| m4 | minor | `backend/app/rag/indexer.py:327,375-376` | Порядок «сначала `delete_by_source`, затем `upsert`» + условие пропуска `sha256` совпал и `chunks>0`: если при `force=True` (или изменённом файле с тем же хэшем) upsert упал после успешного delete, запись `rag_documents` остаётся с прежним sha256/chunks, и следующий **инкрементальный** прогон пропустит документ (`indexer.py:327`) — точки в Qdrant потеряны до `force`/изменения файла. Ошибка видна в `errors`, восстановимо `force`. | `backend-dev-rag`; при сбое upsert сбрасывать `row.sha256`/`chunks` (или индексировать во временную коллекцию) |
| m5 | minor | `docs/features/qa-report.md:47,72,181,330,364` | Отчёт QA устарел относительно `run_tests.py`: документ утверждает «всего 35 наборов» и что 3 QA-набора прогоняются отдельно, тогда как в реестре сейчас **38** наборов (3 QA-набора зарегистрированы). Факт регистрации — плюс; документ нужно привести в соответствие. | `qa` (или Lead при коммите) |
| m6 | minor | `docs/dev-checklist.md:196` | Строка «`python run_tests.py   # 24 набора`» устарела (сейчас 38); файл в этом диффе уже правился devops — дёшево поправить цифру. | `devops` |
| m7 | minor (док/аудит) | `docs/features/security-verdict.md` (разделы C1, C2, C4, «Что осталось») | Вердикт security не обновлён после внесения фиксов C1/C2/C4: документ по-прежнему описывает их как открытые дефекты «до мержа», хотя в дереве фиксы есть (проверено кодом и тестами, см. §4). Для следа ревью/аудита стоит добавить блок «статус условий на момент ревью». C3/C5 остаются открытыми — это корректно отражено. | `security-guard` / Lead |
| m8 | minor (покрытие) | `backend/tests/test_report_parsing.py` | Нет автоматического регресса на sandbox Jinja2 (SSTI): `render_template("{{ ''.__class__.__mro__ }}")`, `__globals__`, `{% include %}` проверялись security-guard вручную, но в наборе не зафиксированы. Свойство безопасности стоит закрепить тестом. | `backend-dev-scheduler` / `qa` |
| m9 | info | `backend/app/rag/indexer.py:172-180` vs `:249-252` | `index_status()` отдаёт `error="В каталоге документов нет файлов…"`, а `reindex()` на том же каталоге — `ok=true, indexed=0` (наблюдение QA O-4). Не дефект, но расхождение сообщений. | `backend-dev-rag` |
| m10 | info | `backend/app/rag/indexer.py:311-319` | Бинарный файл с расширением `.md/.txt` индексируется как текст с `errors="replace"` (рекомендация security в C2 не реализована) — мусор в индексе, не уязвимость. | `backend-dev-rag`, backlog |
| m11 | info | `backend/app/scheduler/settings.py:59-61` | Правка файла, который контракт §3 относит к «фундаменту Lead»: добавлен ключ `scheduler.last_problem_keys`. Изменение аддитивное и нужно по §5, зона задачи scheduler-dev (`backend/app/scheduler/**`) его покрывает — фиксирую как границу, а не нарушение. | Lead (принять к сведению) |
| m12 | info | `docker-compose.yml:34` | `NETOPS_RAG_QDRANT_URL` задан литералом в `environment` и перекрывает значение из `.env`; при необходимости другого адреса Qdrant придётся править compose. Для штатной схемы (qdrant в compose) — приемлемо. | `devops`, backlog |
| m13 | info | `backend/app/rag/indexer.py:409-413` | Широкий `except Exception` вокруг транзакции БД откатывает `rag_documents`, но уже выполненные записи в Qdrant не откатываются — до следующей переиндексации индекс может быть неконсистентен. Восстановимо повторным прогоном; отмечено для эксплуатации. | `backend-dev-rag`, backlog |

Блокеров (severity blocker) нет; замечаний, требующих правки **до коммита**, нет.
M1/M2 — условия корректной эксплуатации при включении фич (по умолчанию обе
выключены: `NETOPS_RAG_ENABLED=false`, `NETOPS_SCHEDULER_ENABLED=false`).

---

## 1. Дифф и границы

- `git diff 4c7214f --stat`: 19 файлов, +2648/−81; untracked — 21 файл
  (13 тестов, 3 шаблона, `backend/reindex_rag.py`, `runbooks/README.md`,
  `runbooks/example-runbook.md`, 2 отчёта).
- Все изменения — в зонах контракта §3 / задач task-1…task-6:
  `app/rag/**`, `app/scheduler/**`, `app/agent/{tools,mock}.py`,
  `app/api/chat.py` (промпт), `app/ui/router.py` (+495 строк, только новые
  маршруты), `app/templates/**`, `docker-compose*.yml`, `runbooks/**`,
  `docs/**`, `run_tests.py`, `backend/tests/**`, `backend/reindex_rag.py`.
- Вне зон изменений нет: `.dsh/**`, `chainlit/**`, `nginx/**`, `frontend/**`,
  `requirements*.txt`, `.env.example`, `.gitignore` не тронуты
  (`git status --porcelain`).
- Мусора/временных файлов, отладочного вывода, `TODO/FIXME`/`breakpoint` в
  изменённом production-коде нет (единственные `print` — CLI `reindex_rag.py`,
  это его вывод по назначению). Новых зависимостей нет: `apscheduler==3.11.3`,
  `httpx`, `jinja2`, `cryptography` уже в `backend/requirements.txt`.
- Изменения в «чужих» тестах — только разрешённые Lead:
  `test_mock_mode.py` (+1 имя мока и комментарий 21→22; точная проверка
  `set(MOCK_TOOLS.keys()) == EXPECTED_MOCK_TOOLS` сохранена, ослабления нет),
  `test_crypto.py` (замена `b64decode`→`urlsafe_b64decode` — корректный фикс
  флейка ~20 %, проверки токена не ослаблены).

## 2. Соответствие контракту §4–§5 (проверено чтением + тестами)

| Требование контракта | Факт |
|---|---|
| §4.1 `QdrantStore.health/collection_info/ensure_collection/upsert/delete_by_source/search`, `QdrantError` без адреса/ключа, таймаут | выполнено (`rag/store.py`); тесты `test_rag_store.py` 26 шт., включая «в тексте ошибки нет URL/ключа», 404→`exists:false`, mismatch размерности, сетевую ошибку |
| §4.1 `docs_dir_path/index_status/reindex(force)`; `.md/.markdown/.txt`; лимит `NETOPS_RAG_MAX_FILE_BYTES`; `RagDocument`; удаление исчезнувших; `..`/симлинки | выполнено (`rag/indexer.py`); payload `{source, heading, chunk, text, sha256}` (поле `file_mtime` — опционально в контракте, отсутствует) |
| §4.1 `search_documents(query, top_k=None) -> (json, status)`, `get_status`, `reindex_documents`; ошибки — `status="error"`, не исключение | выполнено (`rag/service.py`); при выключенном RAG/Qdrant/эмбеддингах — JSON с `status:"error"` и понятным `message` |
| §4.2 инструмент `search_internal_docs`: схема `query` (required) + `top_k` 1..10, описание, RBAC все роли, мок, аудит | выполнено (`agent/tools.py:1354-1368`, `agent/mock.py:443-527`); `TOOLS_SCHEMA` = **25**, инструмент виден; `cache_ttl=60` с кэшированием только успешных ответов |
| §4.2 `MAX_RESULT=20000` — инструмент сам обрезает | `service.MAX_RESULT=20000` (сокращает тексты, затем хвост), `execute_tool` дополнительно режет |
| §5 `start/stop_scheduler` идемпотентны, `apply_settings`, `get_state` (enabled/running/next_run/jobs/last_run), `run_job_now`; `AsyncIOScheduler(timezone=...)`, `CronTrigger`; старт только при env=true; ошибка старта не роняет приложение; нет второго инстанса | выполнено (`scheduler/service.py`); второй инстанс исключён `threading.RLock`+`_scheduler`; после `shutdown` создаётся новый объект (перезапуск «мёртвого» планировщика не выполняется) |
| §5 `parse_health_report`, `diff_with_previous`, `render_template` (Jinja2, неизвестные переменные → пусто) | выполнено (`reports.py`); `SandboxedEnvironment(undefined=ChainableUndefined, autoescape=False, keep_trailing_newline=True)` |
| §5 `validate_recipients`, `send_email` (ssl/starttls/none, таймаут, `EmailMessage` text/plain UTF-8, запрет `\r\n`, `DeliveryError` без секретов) | выполнено (`delivery.py`); таймаут 20 с, режимы, loopback-исключение для `none`+user (C1), валидация hostname (C4) |
| §5 `run_morning_report -> {ok,status,message,sent_to,duration_ms}`, шаги, снимок `scheduler.last_problem_keys` ≤500, `AuditLog(tool="morning_report")` | выполнено (`jobs.py`); `SNAPSHOT_LIMIT=500`, аудит пишется, задание не бросает |
| §5.1 ключи `app_settings`, пароль только шифротекстом, пусто = не менять | `settings.py` DEFAULTS + `settings_store.set_settings(..., extra_secret_keys=SECRET_KEYS)`; пустой секрет не затирает сохранённый |
| §5.2 форма, 6 маршрутов, только admin, серверная валидация (TIME_RE, порт, security, получатели ≤20, шаблон ≤20000), инлайн-ошибки 200 | выполнено (`ui/router.py:577-882`); все 6 маршрутов с `require_roles_page(Role.admin)`; блокирующие кнопки — `asyncio.to_thread` (кроме статуса RAG, см. M1) |
| §5.2 пароль никогда в HTML/логах; индикатор «задан, N симв.» | выполнено: `_report_secret_meta` расшифровывает только для `len()`, в шаблоне `input type=password` без значения; проверено тестами |
| §6 инъекции/SSRF/SSTI: `\r\n` запрещены, шаблоны sandbox, URL только из env | выполнено; в UI/CLI нет приёма URL; тесты на CRLF-инъекцию во всех трёх барьерах |
| §6.5 path traversal/симлинки наружу | `Path.resolve(strict=True)` + `relative_to(root)` + `os.walk(followlinks=False)`; symlink-тест `skip` в этой среде (нет прав), junction-вектор покрыт QA |

## 3. Корректность по зонам (что проверял в коде)

**Индексация (`rag/indexer.py`)**
- Размер файла проверяется **до** чтения (`:300`), чтение ограничено
  `max_bytes+1` (`:311`) — фикс C2 на месте; `MAX_FILE_BYTES<=0` не отключает
  защиту, применяется дефолт 1 МиБ (`_max_file_bytes`).
- Инкремент: `sha256` содержимого, `row.sha256 == digest and chunks>0` → skip;
  `force` пересчитывает; точки перед upsert удаляются (нет «хвостов» при
  уменьшении числа чанков); пустой документ удаляется из Qdrant и БД;
  исчезнувшие файлы вычищаются (сначала Qdrant, потом БД; при ошибке Qdrant
  строка сохраняется — безопасный отказ).
- Ошибки одного файла не прекращают индексацию; ошибки не содержат содержимого
  документов/секретов; широкий `except` в конце — только тип исключения.
- `point_id` — детерминированный UUID5 от `(source, chunk)`.

**Qdrant REST (`rag/store.py`)** — коды ≥400 → `QdrantError(status_code)`,
README-контракт health `"/healthz"`, имя коллекции экранируется `quote`,
`ensure_collection` проверяет размерность существующей коллекции, `search`
клампит limit 1..100, ошибки не печатают URL/ключ; `build_store` — короткий
таймаут в mock-режиме.

**Планировщик (`scheduler/service.py`, `jobs.py`)**
- Гейтинг: env-мастер-выключатель приоритетнее `scheduler.enabled`; старт только
  при env=true; `apply_settings` пересоздаёт задание, при выключении —
  `stop_scheduler`; ошибки старта логируются и не роняют lifespan.
- Timezone `ZoneInfo(NETOPS_SCHEDULER_TZ)`, невалидное время откатывается на
  `NETOPS_SCHEDULER_TIME_DEFAULT` → `08:00`; `coalesce=True`,
  `max_instances=1`, `misfire_grace_time=3600` — дублей писем от одного
  процесса нет (многопроцессный запуск — операционный риск, как и в вердикте
  security).
- `run_morning_report`: `execute_tool(..., user_role="admin")` → парсинг → diff
  со снимком → шаблоны → `send_email` → снимок → `last_run_*` + `AuditLog`;
  исключения не выходят наружу, детали ошибок инструмента — только в серверный
  лог (`_public_error`), в `message`/аудит — безопасный текст.

**Разбор алертов и шаблоны (`reports.py`)** — severity `High/Disaster` →
critical, `Average/Warning` → warning, остальное → other; `problem_key =
"name @ host"` (устойчиво к пересозданию problemid); `new/resolved` сохраняют
порядок и дедуплицируют; `summary.zabbix_alerts` не даёт нулю расходиться с
фактическим списком проблем; неизвестные переменные → пустая строка,
синтаксическая ошибка → `ReportError` (не 500).

**SMTP (`delivery.py`)** — режимы ssl/starttls/none, `timeout=20`
(`SMTP_TIMEOUT_SECONDS`), `EmailMessage` text/plain UTF-8, CRLF запрещены в теме
и адресах, адреса/получатели валидируются и дедуплицируются, лимит 20, `none`
+ учётка на не-loopback сервере запрещён (C1), hostname валидируется (C4),
исключения SMTP/OS/SSL оборачиваются без реквизитов; `with client:` корректно
закрывает соединение даже при ошибке отправки.

**Админка (`ui/router.py`, шаблоны)** — 6 новых маршрутов только admin;
ошибки инлайн со статусом 200; пароль в HTML не попадает; входные значения
экранируются autoescape; проверка Jinja-шаблонов при сохранении (O-3);
учётка без TLS и формат хоста проверяются и в UI (C1-UI/C4-UI); длинные
операции (тест-письмо, ручной запуск, переиндексация) — в `asyncio.to_thread`.

**Compose/runbooks** — Qdrant отдельным сервисом с явным пином
`qdrant/qdrant:v1.19.2`, том `qdrant_storage`, published ports отсутствуют,
`restart: unless-stopped`; `runbooks` монтируется `:ro`; в прод-стеке оба флага
по умолчанию `false`; в dev RAG включён, эмбеддинги — мок; `depends_on`
обновлён. `runbooks/**` — только вымышленные примеры, внутренних адресов и
секретов нет.

**Авто-миграция** — `app_settings.value` в модели `Text`, на Postgres
`ALTER COLUMN TYPE TEXT`, ошибка миграции логируется и не роняет старт
(совместимость старых БД сохранена); `value: VARCHAR(128)` в sqlite не
enforced. `RagDocument` создаётся `create_all` (новая таблица).

## 4. Чужие вердикты: что из C1–C4 / O-1–O-3 закрыто (проверено по коду и тестам)

| Условие | Статус в дереве |
|---|---|
| C1 (`none`+учётка) | **закрыто**: `delivery.py:212-218` (loopback-исключение), `router.py:484-496` (C1-UI); тесты `test_email_delivery.py:299-338`, `test_admin_ui_report_settings.py:484-513` |
| C2 (чтение до проверки лимита, `MAX_FILE_BYTES=0`) | **закрыто**: `indexer.py:288-319`, `_max_file_bytes`; тесты `test_rag_indexer.py:324-380` (файл не открывается вовсе, рост после `stat`, неположительный лимит) |
| C3 (DoS рендера) | **не закрыто** — только ограничение размера результата, см. M2 |
| C4 (валидация `smtp.host`) | **закрыто**: `delivery.py:52-97`, `router.py:451-476`; тесты `test_email_delivery.py:341-372`, `test_admin_ui_report_settings.py:516-528` |
| C5 («пароль задан» при нерасшифровке) | **открыто** (условие до включения отчётов в проде), см. m3 |
| O-1 (текст исключения в инлайн-HTML) | **закрыто**: в заметках только `_exc_kind()`/тип, `str(exc)` в HTML нет; тест `test_admin_ui_report_settings.py:437-449` проверяет отсутствие `internal-detail-42` |
| O-2 (детали инструмента в `last_run_message`/аудите) | **закрыто**: `ReportError.public` + `_public_error`, полный текст — только серверный лог; тест `test_scheduler_jobs.py:439-...` |
| O-3 (битый Jinja-шаблон сохраняется) | **закрыто**: рендер при сохранении, значение не пишется; тесты `test_admin_ui_report_settings.py:452-479` |

## 5. Тесты: качество, изоляция, сеть

- Состав: 10 авторских наборов (`test_rag_*` 4, `test_scheduler*` 2,
  `test_email_delivery`, `test_report_parsing`, `test_settings_store`,
  `test_admin_ui_report_settings`) + 3 независимых QA-набора
  (`*_qa.py`) + правки `test_mock_mode.py`/`test_crypto.py`.
- Тесты **не подогнаны под реализацию**: проверяют контрактные свойства и
  негативные сценарии (ошибки Qdrant/эмбеддингов/сети/БД, отказ RBAC, лимиты,
  path traversal, CRLF, SSTI-friendly input, «нет 500» на всех кнопках), а не
  внутренние вызовы. Примеры: `test_oversized_file_is_not_read_at_all`
  (запрещает `Path.open` и требует, чтобы файл не читался), «в тексте ошибки
  нет URL/ключа», `execute_tool` реальный с подменой только RAG-фасада.
- **Сеть не используется**: `httpx.MockTransport`, `MockEmbeddings`, подмена
  `smtplib`/`execute_tool`; `grep` по новым наборам не нашёл `time.sleep`,
  `requests.`, `socket.`, `httpx.Client(` (только `transport=` для MockTransport)
  и условных skip, кроме зависящих от прав среды (symlink/junction).
- Изоляция: каждый набор — отдельный subprocess со своей временной sqlite
  (env выставляется до импорта `app`); рабочая `backend/netops.db` не
  затрагивается (проверено QA; мои прогоны — тоже только временные БД).
  Ограничение проекта: запуск нескольких наборов **в одном** pytest-процессе
  может конфликтовать по env (отмечено security-guard); штатный путь —
  `run_tests.py` по одному набору, он и проверен.
- Регистрация: `run_tests.py` содержит **38** наборов (25 старых + 13 новых),
  все новые наборы зарегистрированы (в т.ч. 3 QA — п. m5 про устаревший текст
  отчёта QA).

## 6. Фактические проверки (перепрогнано ревьюером)

| Команда | Результат |
|---|---|
| `python run_tests.py` (cwd — корень репозитория) | **всего 38 · OK 38 · FAIL 0 · MISSING 0, exit 0**, 2m 48s; все 13 новых наборов OK, старые 25 OK |
| `backend/.venv/Scripts/python.exe -c "import app.main; ..."` | `import app.main OK; tools = 25`; `search_internal_docs` в `TOOLS_SCHEMA`; модули `rag.*`, `scheduler.*` импортируются |
| `docker compose -f docker-compose.yml config --quiet` (dummy-значения обязательных переменных, реальные секреты не использовались) | **exit 0** (потребовались dummy `DB_PASSWORD`, `NETOPS_ENCRYPTION_KEY`, `CHAINLIT_AUTH_SECRET`, `NETOPS_INTERNAL_SERVICE_TOKEN`; без них — ошибка интерполяции, ожидаемо `${VAR:?}`) |
| `docker compose -f docker-compose.dev.yml config --quiet` | **exit 0** |
| `docker info` | ERROR: демон не запущен — реальный подъём стека не проверялся (как и в отчётах devops/QA) |
| `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .dsh\tools\secret-scan.ps1 -Path <зона> -Mode quick` | `backend\app\rag` exit 0 · `backend\app\scheduler` 0 · `backend\app\ui` 0 · `backend\app\templates` 0 · `runbooks` 0 · `docs\features` 0 · `backend\tests` exit 1 — **3 high, все предсуществующие** (`test_agent_limit.py:94`, `test_agent_settings.py:189`, `test_e2e_flow.py:188`, значения замаскированы); в новых файлах находок 0 |
| `git status --porcelain` / `git ls-files --others --exclude-standard` | 40 записей; все — в согласованных зонах; секретных файлов/БД/`*.pem` в изменениях нет; `.dsh/**` не тронут |
| `grep` по изменённому production-коду: `print(`, `TODO`, `FIXME`, `breakpoint`, `pdb` | совпадения только в CLI `reindex_rag.py` (штатный вывод) |
| `grep` по зависимостям `backend/requirements.txt` (`apscheduler\|jinja2\|httpx\|cryptography\|qdrant`) | `httpx`, `jinja2`, `cryptography`, `apscheduler==3.11.3` уже есть; изменений `requirements*.txt` в диффе нет |

Отдельно: `docker compose config` я запускал с dummy-переменными в окружении
процесса (значения — фиктивные строки, собранные из частей; в отчёт не попали).
Индивидуальные запуски сканера по одиночным файлам (`backend\reindex_rag.py`,
`run_tests.py`, `docker-compose*.yml`) дали exit 2 («прогон не состоялся» —
сканер ожидает каталог); эти файлы покрыты зональными прогонами
`security-guard`/QA (новых находок нет), повторно зонально не подтверждал.

## 7. Не проверено (и почему)

- **Живые Qdrant / эмбеддинги / SMTP Zimbra / Zabbix / vCenter / AD** — в
  среде нет доступа; проверка только через `httpx.MockTransport`,
  `MockEmbeddings`, подмену `smtplib`/`execute_tool` (design §7, §9).
  Соответственно не проверены: реальные TLS/AUTH SMTP, коды Qdrant, реальная
  размерность эмбеддингов, качество поиска, формат реальных алертов.
- **Реальный запуск планировщика по настенному времени** и смена таймзоны/DST:
  проверены объекты заданий (`CronTrigger` поля, `next_run`, `get_state`), но
  не факт срабатывания; путь `NETOPS_SCHEDULER_ENABLED=true` с живым
  `AsyncIOScheduler` в приложении не поднимался.
- **Docker**: демона нет — реальный `up/build`, монтирование `runbooks`,
  nginx-маршруты, поведение образа не проверялись; только `compose config`
  (см. §6).
- **Postgres**: авто-миграция `app_settings.value VARCHAR→TEXT` проверена
  чтением кода и логики (sqlite-тип не enforced), на живом Postgres не
  прогонялась.
- **Симлинк наружу** в индексаторе: создание симлинков на этом Windows-хосте
  недоступно (тест `skip`); эквивалентный вектор (junction) покрыт QA и
  `_safe_source` проверен прямыми вызовами.
- **Браузер/HTMX/вёрстка**: `settings.html` + новые фрагменты читал и
  сопоставлял с маршрутами; реальное поведение HTMX/стили в браузере не
  проверялось.
- **Агент с живой LLM**: пункт 15 системного промпта добавлен, поведение модели
  (вызов `search_internal_docs`) не тестировалось — только схема/RBAC/мок.
- **Кросс-файловый прогон** нескольких наборов в одном pytest-процессе (см.
  §5) не воспроизводил: считается известным ограничением тестовой
  инфраструктуры проекта, штатный раннер изолирует наборы.

## 8. Что сделать с результатом

1. Коммит возможен: блокеров нет; M1/M2 — условия эксплуатации, а не коммита.
2. Перед включением фич в проде: закрыть M1 (to_thread для статуса RAG),
   M2 (ограничение рендера Jinja2), m3 (C5), обновить `security-verdict.md`
   (m7), поправить устаревшие цифры в `qa-report.md` (m5) и
   `docs/dev-checklist.md` (m6).
3. Мелочи m1/m2/m4/m8/m9/m10/m12/m13 — отдельными задачами владельцам
   (см. таблицу замечаний), не блокируют коммит.
