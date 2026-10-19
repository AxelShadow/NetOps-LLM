# Чек-лист локальной проверки (Фазы 12–14, migration.md)

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

## Фаза 14: переключение интерфейсов (/ и /legacy)

Старый SPA перенесён на `/legacy` (frontend/legacy/index.html), `/`
решает backend по флагу `NETOPS_USE_NEW_UI` (config.py):
`false` (дефолт, прод-режим) — редирект 303 на `/legacy/`;
`true` (dev-стек) — лендинг с выбором: чат, админка, старый интерфейс.

Автоматическая часть — `backend/tests/test_ui_switch.py` (TestClient,
временная sqlite, оба значения флага):

```bash
cd backend && .venv/Scripts/python.exe tests/test_ui_switch.py
```

Ручная часть (визуальная, через nginx dev-стека):

| № | Что проверить | Как | Ожидание |
|---|---------------|-----|----------|
| 1 | Старый интерфейс на /legacy | http://localhost:8080/legacy/ | Открывается старый SPA («NetOps LLM»), навигация работает |
| 2 | Главная / — новый UI | http://localhost:8080/ (dev-стек: NETOPS_USE_NEW_UI=true) | Лендинг: карточки «Чат», «Админка», «Старый интерфейс» |
| 3 | Главная / — старый режим | Снять флаг (NETOPS_USE_NEW_UI=false, пересоздать контейнер app) и открыть / | Редирект на /legacy/ |
| 4 | Ссылки лендинга живые | Кликнуть «Чат» и «Админка» на лендинге | /chat открывается (после логина), /admin/ — форма входа |
| 5 | nginx-стек целиком | `docker compose -f docker-compose.dev.yml up --build -d` + сид, затем / и /legacy/ через порт 8080 | Оба пути отвечают (проверки 1–2 выше) |

Прод (docker-compose.yml): флаг задаётся оператором через `.env`
(`NETOPS_USE_NEW_UI=true/false`), в compose-файле не зашит.

## Этап SNMP: подключение по SNMP (v2c) и скилл принтеров

Сетевые устройства (eltex/mikrotik/usergate) и принтеры опрашиваются
по SNMPv2c. Community хранится в инвентаре (колонки `devices.snmp_version`
/ `snmp_community`, авто-миграция `_ensure_device_columns` при старте;
тип устройства `printer` — `ALTER TYPE ... ADD VALUE IF NOT EXISTS`).

Автоматическая часть (мок-режим, временная sqlite):

```bash
cd backend && .venv/Scripts/python.exe tests/test_snmp_migration.py
cd backend && .venv/Scripts/python.exe tests/test_snmp_tools.py
```

Инструменты агента (tools.py): `snmp_info` (sysDescr/sysName/sysContact/
sysLocation/sysUpTime), `snmp_interfaces` (ifTable: имя/MTU/скорость/
статусы), `snmp_walk` (произвольный OID, GETBULK), `printer_info`
(Printer-MIB: serial, prtMarkerLifeCount — распечатанные страницы,
уровень тонера %, hrPrinterStatus, битмаска ошибок — замятие/нет бумаги/
крышка/офлайн; `device='all'` — все принтеры инвентаря). Счётчик сканов
вендорозависим (стандартный Printer-MIB его не даёт) — инструмент честно
отдаёт `pages_scanned: null` с пояснением.

Ручная часть (нужен живой SNMP-агент, мок-режим выключен):

| № | Что проверить | Как | Ожидание |
|---|---------------|-----|----------|
| 1 | Форма инвентаря с SNMP-полями | Админка → Инвентарь → «Добавить», тип printer | Поля «SNMP version» и «SNMP community» в форме |
| 2 | snmp_info на реальном устройстве | Чат: «Покажи описание и uptime core-sw» | sysDescr устройства, uptime «X д Y ч Z мин» |
| 3 | snmp_interfaces | Чат: «Какие интерфейсы у core-sw?» | Таблица ifTable со статусами up/down |
| 4 | printer_info на живом принтере | Чат: «Сколько страниц напечатано на hq-printer-1? Тонер?» | pages_printed, toner_level_percent, status |
| 5 | printer_info all | Чат: «Состояние всех принтеров» | Ответ по каждому принтеру группы |
| 6 | Недоступный принтер в all | Выключить принтер, спросить «по всем принтерам» | Ошибка только в его item, остальные живые |

## Этап 19: композитный отчёт по принтерам + SNMP-Discovery

Проблема этапа: модель опрашивала принтеры по одному (лимит 20 шагов
агента) и додумывала данные по неопрошенным. Решение: один композитный
вызов + анти-галлюцинационные правила промпта.

Автоматическая часть (мок-режим, временная sqlite):

```bash
cd backend && .venv/Scripts/python.exe tests/test_printers_report.py
cd backend && .venv/Scripts/python.exe tests/test_printer_discovery.py
```

Инструмент `get_printers_report` (is_composite, TTL 120s): все включённые
принтеры за ОДИН вызов — параллельный опрос (16 потоков, таймаут 1с),
одна строка на принтер (статус/страницы/тонер %/ошибки/serial), сводка,
недоступные отдельным списком; бюджет 15К символов против обрезки
MAX_RESULT — при 100+ принтерах лишние здоровые уходят в
remaining_healthy. SYSTEM_PROMPT: правило 2 (не выдумывать при
отсутствии данных) и правило 14 (отчёт по всем = один вызов
get_printers_report, printer_info — только по одному).

Discovery (админка, зеркалит синхронизацию Zabbix): Инвентарь → форма
«Найти принтеры (SNMP)» — ОДНА подсеть с маской до /22 (≤1024 адресов),
community (default public), порт, таймаут. Найденные (критерий: серийная
таблица Printer-MIB непустая, фолбэк — printer-модели в sysDescr)
добавляются ВЫКЛЮЧЕННЫМИ: тип printer, группа «Принтеры», source=snmp,
community из формы, имя из sysName (или printer-{ip}). Повторный запуск
обновляет найденное (community/description), чужой host — пропускает,
пропавших не трогает.

Ручная часть (мок-стенд — чат; discovery — реальная сеть):

| № | Что проверить | Как | Ожидание |
|---|---------------|-----|----------|
| 1 | Отчёт одним вызовом | Чат: «дай информацию по принтерам» | Один шаг get_printers_report; все принтеры, без выдуманных |
| 2 | Недоступный в отчёте | Чат при выключенном принтере | unreachable-список, остальные целы |
| 3 | Discovery на реальной подсети | Админка: подсеть принтерного VLAN, public | Flash «найдено N, добавлено N», строки выкл + бейдж snmp |
| 4 | Discovery повторно | Та же подсеть ещё раз | «обновлено N», дублей нет |
| 5 | Фильтр источника snmp | Инвентарь → Источник: snmp | Только discovery-принтеры |

## Этап 20: ошибки агента, настройки, отчёт по страницам, протоколы+проверка подключения

Автоматическая часть (мок-режим, временная sqlite):

```bash
cd backend && .venv/Scripts/python.exe tests/test_agent_settings.py
cd backend && .venv/Scripts/python.exe tests/test_printers_pages_report.py
cd backend && .venv/Scripts/python.exe tests/test_printer_discovery.py   # +MAC/DNS/дедуп
python run_tests.py   # 38 наборов (25 базовых + 13 новых RAG/планировщик/админка/QA)
```

Мок-сценарии чата (dev-стек, NETOPS_MOCK_MODE=true):

| № | Что проверить | Как | Ожидание |
|---|---------------|-----|----------|
| 1 | Отчёт по страницам | Чат: «сколько страниц напечатано по всем принтерам» | Один шаг get_printers_pages_report: summary с total_pages, mono/color |
| 2 | Цветной в отчёте | Тот же запрос | У цветного — cartridges с ролями black/color и уровнями % |
| 3 | Ошибка LLM внятная | Остановить LM Studio, отправить любое сообщение | Текст с причиной (нет связи/таймаут/контекст), не «Сервер LLM недоступен»; в аудите запись tool=agent status=error |

Ручная часть (визуальная, dev-стек через nginx :8080):

| № | Что проверить | Как | Ожидание |
|---|---------------|-----|----------|
| 4 | Кнопка «Добавить» | Инвентарь → «Добавить устройство» | Кнопка формы «Добавить» (не «Сохранить»); чекбоксы SSH/SNMP |
| 5 | Ошибка имени видна | Ввести имя существующего устройства → Добавить | Красный блок с текстом ошибки в модалке (не тишина/400 в консоли) |
| 6 | Проверка подключения: провал | Тип edgecore, host недоступный, отметить SNMP → Добавить | Ошибка в модалке («SNMP … не отвечает (community …?)»), устройство не создано |
| 7 | Проверка подключения: успех | Тип aruba, живой хост, SSH+SNMP → Добавить | Устройство создано |
| 8 | Настройки агента | /admin/settings → секция «Параметры агента» | Поля бюджет контекста (50000) и лимит шагов (20); изменение сохраняется |
| 9 | Валидация настроек | Ввести 999999 / «abc» → Сохранить | Инлайн-ошибка с диапазоном, значение не применилось |
| 10 | Discovery с MAC/DNS | Найти принтеры в реальной подсети | У новых строк MAC и DNS-имя (в карточке/БД); повторный запуск с изменённым IP переносит по MAC |

## Этап 21: RAG по runbook'ам + утренний отчёт

Состав стека: `web` (nginx, :8080) → `chainlit` (/chat) → `app` (FastAPI) →
`qdrant` (векторный индекс, **порт наружу не пробрасывается**: доступен только
из сети compose как `qdrant:6333`). Корпус runbook'ов — каталог `./runbooks`
на хосте, в контейнере `/data/runbooks:ro` (правки без пересборки образа).
Отдельный сервис вместо зависимости образа выбран после инцидента ENOSPC
(`docs/docker-image-size/00-diagnosis.md`).

Флаги (имена без значений): `NETOPS_RAG_ENABLED` (dev-стек: `true`, прод:
`false` по умолчанию, включается оператором в `.env`), `NETOPS_RAG_DOCS_DIR`,
`NETOPS_RAG_QDRANT_URL`, `NETOPS_SCHEDULER_ENABLED` (прод: `false` по
умолчанию), `NETOPS_EMBEDDING_*` (пусто = наследуются `NETOPS_LLM_*`).
В dev-стеке эмбеддинги — мок (`NETOPS_MOCK_MODE=true`), Qdrant — реальный.

> **Честная оговорка о среде.** В среде разработки агентов **нет
> docker-демона** (`docker info` → ошибка), поэтому команды `docker compose
> up/exec/ps` из этого блока здесь **фактически не выполнялись**. Проверено
> статически: `docker compose config --quiet` для обоих файлов с одноразовыми
> dummy-переменными → код 0. Реальный подъём, индексация и отправка письма
> проверяются на хосте с Docker.

### Автоматически / статически (без docker-демона)

```bash
# валидация конфигурации (значения dummy-переменных в окружение, не из .env)
docker compose -f docker-compose.yml config --quiet
docker compose -f docker-compose.dev.yml config --quiet
python run_tests.py    # 38 наборов (базовые + новые RAG/планировщик/админка/QA)
```

### Стенд (нужен docker-демон)

```bash
docker compose -f docker-compose.dev.yml up --build -d   # web+chainlit+app+qdrant
docker compose -f docker-compose.dev.yml run --rm seed   # фикстуры
docker compose -f docker-compose.dev.yml ps              # qdrant и app — Up
```

Порядок действий при изменениях в `runbooks/` (dev-стенд):

```bash
docker compose -f docker-compose.dev.yml up -d --force-recreate app   # если менялись env (restart env не перечитывает)
# Qdrant отвечает приложению (readyz, 200)
docker compose -f docker-compose.dev.yml exec app python -c "import httpx; print(httpx.get('http://qdrant:6333/readyz', timeout=5).status_code)"
# статус индекса: documents/chunks и время последней индексации
docker compose -f docker-compose.dev.yml exec app python -c "from app.rag.service import get_status; print(get_status())"
# переиндексация (force=True — пересчитать все файлы)
docker compose -f docker-compose.dev.yml exec app python -c "from app.rag.service import reindex_documents; print(reindex_documents(force=True))"
```

### Ручная проверка (dev-стенд через nginx :8080)

| № | Что проверить | Как | Ожидание |
|---|---------------|-----|----------|
| 1 | Qdrant жив | `exec app python -c "import httpx; print(httpx.get('http://qdrant:6333/readyz').status_code)"` | 200; порт 6333 на хосте **не** слушается (`docker compose ps` — у qdrant нет published ports) |
| 2 | Индекс наполнен | статус индекса (см. блок выше) или `/admin/settings` → «RAG по runbook'ам» | `documents` ≥ 2 (README + example-runbook), `chunks` > 0, `last_indexed_at` свежий, `qdrant_ok=true` |
| 3 | Переиндексация | изменить строку в `runbooks/example-runbook.md` → переиндексация | новое содержимое находится через поиск, `errors` пуст; повторный запуск — `skipped` = число файлов |
| 4 | Поиск из чата | Чат → «Что написано в инструкции про ротацию журналов?» | шаг инструмента `search_internal_docs`, фрагменты с путями `runbooks/...` |
| 5 | RAG выключен | **не в dev-стеке**: в мок-режиме RAG включён виртуально (indexer `_runtime_enabled`). Проверять на прод-конфигурации/локально: `NETOPS_MOCK_MODE=false`, `NETOPS_RAG_ENABLED=false` → тот же вопрос | инструмент отдаёт `status="error"` с понятной причиной (не 500); чат не падает |
| 6 | Статус в админке | /admin/settings (admin) → секции «RAG по runbook'ам» и «Утренний отчёт» | статус индекса (`GET /admin/settings/partial/rag`) и статус планировщика видны; формы сохраняются; инлайн-ошибки валидации (время `HH:MM`, порт, получатели) |
| 7 | Утренний отчёт вручную | /admin/settings → «Запустить отчёт сейчас» (`POST /admin/settings/report/run-now`) | статус последнего запуска: время/статус/сообщение; при отсутствии SMTP — понятная ошибка «не настроено», не 500 |
| 8 | Тестовое письмо | /admin/settings → «Отправить тестовое письмо» (`POST /admin/settings/report/test-email`) | при настроенном SMTP письмо приходит получателям; пароль SMTP в HTML/логах **не** отображается (только «задан, N симв.») |

Маршруты — из дизайна (`docs/features/rag-and-scheduler-design.md` §5.2) и
UI-задачи frontend-dev; п.6–8 проверяются после сборки образа со страницей
настроек. Если UI ещё не собран в вашем чекауте, п.2–5 проверяются командами
из блока выше.

### Прод (`docker-compose.yml`), на сервере с Docker

1. Перед `up` — проверить место: новый образ `qdrant/qdrant:v1.19.2`
   (~75 МБ сжатый плюс распакованный rootfs) и том `qdrant_storage`;
   требования к запасу места — `docs/docker-image-size/host-disk-runbook.md`.
2. Пин тега Qdrant менять только осознанно, сверив совместимость с текущим
   индексом (мажорные обновления Qdrant могут требовать переиндексации).
3. Включение RAG/планировщика — только через `.env` оператором
   (`NETOPS_RAG_ENABLED=true`, `NETOPS_SCHEDULER_ENABLED=true`), после
   переиндексации корпуса из `./runbooks`.
4. **`docker compose down -v` на проде запрещён**: удаляет тома `pgdata`
   (БД PostgreSQL) и `qdrant_storage` (индекс RAG). Индекс восстанавливается
   переиндексацией, БД — только из дампа. `docker volume prune --all` тоже
   запрещён (см. host-disk-runbook, раздел 3.7).
5. Проверка после выката: `docker compose ps` (qdrant Up, у него нет
   published ports), `/admin/settings` — статус индекса, тестовое письмо.

