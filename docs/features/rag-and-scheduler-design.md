# RAG по runbook'ам + утренние отчёты: дизайн и контракты

> Статус: контракт зафиксирован · Обновлено: 2026-10-19 · Ветка: `main`
> Владелец контракта: Lead. Изменения контракта — только через Lead и ревью
> `security-guard` (затрагивает интеграции, секреты, планировщик).

Документ — единый источник правды для команды: что делаем, какие файлы
кому принадлежат, какие функции/ключи/шаблоны считаются контрактом.
Секретов здесь нет и быть не должно.

## 1. Цель

1. **RAG по runbook'ам**: индексировать внутренние документы (`.md`/`.txt`),
   искать по ним из чата инструментом агента `search_internal_docs(query)`,
   управлять индексом из админки (`/admin/settings`).
2. **Утренний отчёт**: APScheduler раз в сутки вызывает
   `get_infrastructure_health`, разбирает алерты (сравнение с прошлым
   прогоном: новые/устранённые), рендерит письмо по редактируемому шаблону
   и отправляет по SMTP (Zimbra). Получатели, SMTP-учётка и шаблон
   редактируются в админке; учётку создаём позже — форма должна быть готова.

## 2. Принятые решения

| # | Решение | Обоснование |
|---|---|---|
| Р-1 | Векторное хранилище — **Qdrant отдельным сервисом** docker-compose | выбор заказчика; образ `app` не растёт |
| Р-2 | Клиент Qdrant — **свой тонкий REST-клиент на httpx**, без `qdrant-client` | `qdrant-client` тянет grpcio (~десятки МБ); после инцидента ENOSPC образы бережём. В тестах — подмена httpx-транспорта |
| Р-3 | Эмбеддинги — **OpenAI-совместимый `/embeddings`** на том же сервере, что LLM (`NETOPS_EMBEDDING_*`, пусто = наследовать `NETOPS_LLM_*`); модель bge-m3 / Qwen3-Embedding-0.6B задаётся настройкой | не привязываемся к конкретному движку; dev/тесты — `MockEmbeddings` (детерминированный, без сети) |
| Р-4 | Корпус — каталог `runbooks/` в репозитории + **том на сервере** (`NETOPS_RAG_DOCS_DIR`, в контейнере `/data/runbooks`) | правки runbook'ов без пересборки |
| Р-5 | Инкрементальная индексация по sha256, состояние — таблица `rag_documents` | не пересчитывать неизменённые файлы; видно, что проиндексировано |
| Р-6 | Планировщик — **APScheduler 3.11** (`AsyncIOScheduler`) в `lifespan`; выключен по умолчанию (`NETOPS_SCHEDULER_ENABLED=false`) | без внешней инфраструктуры; тесты не запускают задания |
| Р-7 | Настройки отчёта — **runtime в `app_settings`** (не env), секреты шифруются Fernet | «учётку и шаблон настраивать из админки», без перезапуска |
| Р-8 | Доставка — **SMTP напрямую из приложения** (Zimbra), письмо `text/plain` UTF-8; тема и тело — Jinja2-шаблоны из админки | заказчик уточнил: не через API Zabbix |
| Р-9 | Разбор алертов — сравнение списка проблем с предыдущим прогоном (новые/устранённые), снимок хранится в `app_settings` (JSON) | «разбор алертов» без отдельной БД истории |
| Р-10 | Ветка `main`, секреты не коммитим, перед коммитом — `secret-scan.ps1 -Mode quick` | AGENTS.md §5–6 |

## 3. Границы файлов (кто что пишет)

Пересечение записывается в задаче на доске; чужой файл не редактируется.

| Зона | Владелец | Файлы |
|---|---|---|
| Фундамент (уже закоммичен) | Lead | `backend/app/config.py`, `backend/app/settings_store.py`, `backend/app/models.py` (RagDocument, AppSetting.value→TEXT), `backend/app/crypto.py` (фикс flake), `backend/app/main.py` (lifespan+миграция), `backend/app/rag/{__init__,chunking,embeddings}.py`, `backend/app/scheduler/{__init__,settings}.py`, заглушки `rag/{store,indexer,service}.py`, `scheduler/{service,jobs,reports,delivery}.py`, `requirements*.txt`, `.env.example`, `.gitignore` |
| RAG | `backend-dev-rag` | `backend/app/rag/**`, `backend/app/agent/tools.py` (регистрация инструмента), `backend/app/agent/mock.py` (мок инструмента), `backend/reindex_rag.py`, `backend/tests/test_rag_*.py` |
| Планировщик | `backend-dev-scheduler` | `backend/app/scheduler/**`, `backend/tests/test_scheduler*.py`, `test_email*.py`, `test_report*.py`, `test_settings_store.py` |
| Админка | `frontend-dev` | `backend/app/ui/router.py` (только новые маршруты), `backend/app/templates/pages/settings.html`, `backend/app/templates/components/settings/report_*.html`, `backend/tests/test_admin_ui_report_settings.py` |
| Compose/инфра | `devops` | `docker-compose.yml`, `docker-compose.dev.yml`, `runbooks/README.md`, `runbooks/example-runbook.md`, `docs/dev-checklist.md` |
| QA | `qa` | `run_tests.py`, `backend/tests/test_rag_*_qa.py`, `test_scheduler_*_qa.py`, `test_admin_ui_qa.py`, отчёт `docs/features/qa-report.md` |
| Безопасность/ревью | `security-guard`, `reviewer` | отчёты `docs/features/security-verdict.md`, `docs/features/review.md` (значения секретов не приводятся) |
| Карта проекта | `architect` | `.dsh/kb/**` (10, 30, 40, 50, 90), `_generated/` |

## 4. Контракты RAG

### 4.1 Модули

`app/rag/chunking.py` — **готов**: `chunk_markdown(text, *, max_chars, overlap) -> list[Chunk]`;
`Chunk(index, heading, text)`; гарантии: `len(text) <= max_chars`, заголовки в
формате `Раздел > Подраздел`, пустой документ → `[]`.

`app/rag/embeddings.py` — **готов**: `build_embedding_provider()` →
`EmbeddingProvider.embed_texts(list[str]) -> list[list[float]]`;
`MockEmbeddings` (mock-режим), `OpenAICompatibleEmbeddings` (сеть, `/embeddings`,
проверка размерности и числа векторов). Ошибки — `EmbeddingError`.

`app/rag/store.py` — **do RAG dev**: `QdrantStore(url, collection, auth_key, timeout)`:
`health() -> bool`, `collection_info() -> {'exists', 'points_count'}`,
`ensure_collection(dim)`, `upsert(points: list[dict])`,
`delete_by_source(source) -> None`, `search(vector, limit) -> list[{'id','score','payload'}]`.
Идентификаторы — `point_id(source, chunk_index)` (UUID5, уже готов).
Ошибки — `QdrantError`. REST-контракт — в docstring файла.

`app/rag/indexer.py` — **do RAG dev**: `docs_dir_path()`, `index_status() -> dict`,
`reindex(force=False) -> dict`; защита от `..`/симлинков; расширения
`.md/.markdown/.txt`; лимит размера `NETOPS_RAG_MAX_FILE_BYTES`; пишет
`RagDocument` (source — относительный путь, sha256, size_bytes, chunks,
indexed_at); удалённые файлы вычищаются. Payload точки:
`{source, heading, chunk, text, sha256, file_mtime?}`.

`app/rag/service.py` — **do RAG dev**: `search_documents(query, top_k=None)
-> (json, status)`; `get_status()`; `reindex_documents(force=False)`.

### 4.2 Инструмент агента

Имя: `search_internal_docs`. Регистрация в `app/agent/tools.py`:

```python
@register_tool(
    name="search_internal_docs",
    description="Поиск по внутренним runbook'ам и документации NetOps (RAG). "
                "Вызывать при вопросах «как сделать», «что написано в инструкции», "
                "по процедурам и регламентам. Возвращает фрагменты с путями.",
    parameters={"type": "object",
                "properties": {"query": {"type": "string"},
                               "top_k": {"type": "integer", "minimum": 1, "maximum": 10}},
                "required": ["query"]})
```

* RBAC: все роли (`viewer`, `engineer`, `admin`) — документы внутренние и
  read-only; решение зафиксировано, менять только через `security-guard`.
* Мок-режим: в `agent/mock.py` — `MOCK_TOOLS["search_internal_docs"]`
  (детерминированный ответ из 2–3 фейковых фрагментов).
* Ошибки (RAG выключен, Qdrant/эмбеддинги недоступны) — `status="error"` и
  понятный JSON, не исключение; аудит пишет `execute_tool`.
* `MAX_RESULT=20000` действует: инструмент сам обрезает `text` чанков.

## 5. Контракты планировщика

`app/scheduler/settings.py` — **готов**: `DEFAULTS`, `SECRET_KEYS`,
`TIME_RE`, `SECURITY_MODES`, `TEMPLATE_MAX_CHARS`, `RECIPIENTS_MAX`.

`app/scheduler/service.py` — **do scheduler dev**: `start_scheduler()`,
`stop_scheduler()` (идемпотентны), `apply_settings() -> dict`,
`get_state() -> {enabled, running, next_run, jobs, last_run:{at,status,message}}`,
`run_job_now() -> dict`. `AsyncIOScheduler(timezone=NETOPS_SCHEDULER_TZ)`,
задание `jobs.run_morning_report` с `CronTrigger(hour, minute)`. Старт
только при `NETOPS_SCHEDULER_ENABLED=true`; ошибка старта логируется, но
не роняет приложение. Повторный `start_scheduler()` не создаёт второй
планировщик.

`app/scheduler/reports.py` — **do scheduler dev**: `parse_health_report(raw)
-> dict` (факты для шаблона), `diff_with_previous(keys, previous_keys)
-> (new, resolved)`, `render_template(template, facts) -> str` (Jinja2,
`autoescape=False` для текстового письма; неизвестные переменные → пустая
строка через `Undefined`-политику, не 500).

`app/scheduler/delivery.py` — **do scheduler dev**: `validate_recipients(raw)
-> list[str]`, `send_email(subject, body, recipients=None)`; `smtplib`,
режимы `ssl` (SMTP_SSL) / `starttls` / `none`, таймаут 20 с, UTF-8
(`email.message.EmailMessage`), запрет `\r`/`\n` в теме/адресах,
`DeliveryError` без секретов.

`app/scheduler/jobs.py` — **do scheduler dev**: `run_morning_report() -> dict`
(`{ok, status, message, sent_to, duration_ms}`); шаги — `execute_tool(
"get_infrastructure_health", {}, user_role="admin")` → парсинг → шаблоны из
`app_settings` → `send_email` → запись `scheduler.last_run_*` + `AuditLog
(tool="morning_report")`. Снимок проблем для следующего diff — ключ
`scheduler.last_problem_keys` (JSON-массив, ограничить 500 элементами).

### 5.1 Runtime-ключи (редактируются в админке)

`app_settings`, см. `app/scheduler/settings.py`. Пароль `smtp.password`
**никогда** не отображается: в форме — «задан, N символов» + поле «новый
пароль» (пусто = не менять). Сохранение — через
`settings_store.set_settings(..., extra_secret_keys=SECRET_KEYS)`.

### 5.2 Админка (`/admin/settings`, секция «Утренний отчёт»)

* переключатель включённости, время `HH:MM`, список получателей;
* SMTP: host, port, security (`starttls|ssl|none`), user, from;
* поле «новый пароль SMTP» + индикатор «пароль задан (N симв.)»;
* textarea темы и тела шаблона с подсказкой доступных переменных
  (`date`, `total_active_devices`, `zabbix_alerts`, `critical`, `warning`,
  `new_problems_count`, `resolved_count`, `datastore_low_space`,
  `host_high_load`, `vms_with_snapshots`, `problems_text`, `summary_text`);
* кнопки: «Сохранить», «Отправить тестовое письмо», «Запустить отчёт
  сейчас»; статус последнего запуска (время/статус/сообщение).
* Доступ: только `admin` (как вся страница настроек). Валидация серверная:
  время по `TIME_RE`, порт `1..65535`, security из списка, получатели
  валидны и ≤ `RECIPIENTS_MAX`, шаблон ≤ `TEMPLATE_MAX_CHARS`.
* Ошибки рендерятся инлайн (htmx-паттерн `agent_form.html`: статус 200,
  фрагмент с ошибкой).

## 6. Безопасность (обязательно для всех)

1. Секреты только в `app_settings` зашифрованными; значения не логируются,
   не отдаются в API/HTML, в отчётах — `<redacted, N chars>`.
2. Никаких пользовательских URL: Qdrant/эмбеддинги/SMTP — из env или
   app_settings, только фиксированные схемы `http/https`.
3. Шаблоны писем — Jinja2 **без** HTML-эскейпа, но и без `|safe`/`autoescape`
   ловушек: письмо текстовое; данные из Zabbix могут содержать любые символы —
   вставляются как текст.
4. Заголовки письма: `\r`/`\n` запрещены (инъекция). Адреса — regex,
   максимум `RECIPIENTS_MAX`.
5. Индексатор: путь документа обязан оставаться внутри `NETOPS_RAG_DOCS_DIR`
   (`Path.resolve()` + `is_relative_to`), симлинки наружу отбрасываются.
6. Не менять RBAC существующих страниц; новые маршруты — `require_roles_page(Role.admin)`.
7. Диагностика (проверка SMTP/Qdrant/эмбеддингов) показывает только
   тип ошибки и код, без учётных данных и содержимого документов.
8. Перед коммитом: `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .dsh\tools\secret-scan.ps1 -Mode quick`
   — новые файлы не должны добавлять находок; предсуществующие 23 high /
   46 medium фиксируются в отчёте как известные (см. `docs/docker-image-size/00-diagnosis.md` §7).

## 7. Тестовая стратегия (без сети и docker)

* RAG: `MockEmbeddings` + подмена httpx-транспорта Qdrant
  (`httpx.MockTransport`) + временный каталог runbook'ов в `tmp_path`;
  проверки: чанки/заголовки, инкремент по sha256, удаление файлов,
  path traversal, размерность, tool RBAC/ошибки/аудит, мок-режим.
* Планировщик: `AsyncIOScheduler` не поднимать в тестах приложения; тесты
  сервиса — с `NETOPS_SCHEDULER_ENABLED=false` и явными вызовами
  `run_morning_report()`; SMTP — подмена `smtplib.SMTP`/`SMTP_SSL`
  (без реальной сети); шаблоны — рендер в строку; валидация — границы.
* Админка: TestClient + cookie-сессия (как `tests/test_admin_ui_settings.py`),
  non-admin → 403/redirect, секрет не попадает в HTML, ошибки валидации.
* Все новые наборы регистрируются в `run_tests.py` (owner — `qa`).

## 8. Команды

```powershell
# тесты backend (из backend/, интерпретатор backend/.venv)
.\.venv\Scripts\python.exe tests\test_rag_indexer.py
# весь прогон (из корня)
python run_tests.py
# переиндексация runbook'ов (после релиза)
cd backend && .\.venv\Scripts\python.exe reindex_rag.py
```

## 9. Известные ограничения среды (важно, чтобы не терять время)

* `tempfile.mkdtemp()` на этом Windows-хосте создаёт нерабочие DACL;
  в `backend/.venv` стоит локальный `sitecustomize.py` (не в git), который
  чинит `mkdtemp`. В репозиторий его не коммитить.
* `backend/.venv` закрыт для записи из песочницы: новые пакеты ставятся в
  `.deps-local/` (в `.gitignore`) и подключаются файлом `.pth` в venv.
  `apscheduler==3.11.3` там уже стоит.
* Хук блокирует **запись литералов** вида `token = "<значение ≥6>"`,
  `api_key = ...`, `secret: ...`. Это не повод ослаблять правила: тестовые
  значения собирайте программно (`"test" + "-token"`, `"dev"` и т.п.).
* Docker-демона в среде нет: compose проверяется через
  `docker compose config` с dummy-переменными или чтением файла; реальная
  сборка — на сервере (в отчёте честно писать «не проверено»).
* Эмбеддинг-сервер и Qdrant локально недоступны: тесты только с моками.

## 10. Открытые вопросы (не блокируют старт)

1. Какая модель эмбеддингов реально поднята на LLM-сервере и её размерность
   (`bge-m3` = 1024, `Qwen3-Embedding-0.6B` = 1024) — уточнить у владельца
   LLM-сервера; код принимает размерность настройкой.
2. SMTP-учётка Zimbra будет создана позже; до этого письма не уходят —
   форма должна корректно показывать «не настроено».
3. Пин образа Qdrant (`qdrant/qdrant:vX.Y.Z`) — выбрать конкретный тег при
   первом деплое на сервере (в среде нет docker, проверить нельзя).
4. Нужен ли HTML-вариант письма — пока текст; при требовании «красиво» —
   отдельная задача с шаблоном и `EmailMessage.set_content(html=...)`.
