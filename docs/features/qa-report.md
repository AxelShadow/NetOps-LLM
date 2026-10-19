# QA-отчёт: RAG по runbook'ам + утренний отчёт

> Статус: независимая проверка завершена · Дата: 2026-10-19 · Ветка: `main` · HEAD: `cb9d483`
> Роль: `qa` (`.dsh/roles/qa.md`) · Задача: `task-5` на доске
> Контракт: [`rag-and-scheduler-design.md`](rag-and-scheduler-design.md) (разделы 4–7)
> Фичи «RAG + утренний отчёт» закоммичены (`0656bd5`, UX-доработка `cb9d483`);
> текущий **незакоммиченный** дифф — хотфикс бага из эксплуатации (task-11)
> и правки QA-тестов под него; коммит хотфикса ждёт зелёного прогона.
> **Итоговый прогон (после хотфикса `truncate`/task-11 и фиксов
> C1/C2/C3/C4/O-1/O-2/O-3): `python run_tests.py` → exit 0, всего 38 ·
> OK 38 · FAIL 0 · MISSING 0, 2m 40s** (см. §2.1).

Документ фиксирует **фактические** прогоны: каждая цифра ниже получена командой,
указанной рядом. Секретов, значений `.env` и внутренних адресов здесь нет.

---

## 1. Что проверено

**Уже закоммичено** (`0656bd5` — «Этапы 21–22: RAG по runbook'ам + утренние
отчёты», `cb9d483` — UX-карточки на Dashboard): зона 1 — RAG
(`backend/app/rag/**`, инструмент `search_internal_docs` в
`app/agent/tools.py`/`app/agent/mock.py`, `backend/reindex_rag.py`);
зона 2 — утренний отчёт (`backend/app/scheduler/**`, админка
`app/ui/router.py`, `components/settings/report_*.html`,
`components/settings/rag_status.html`, `pages/settings.html`); сопутствующее
(`app/api/chat.py` — пункт 15 промпта, `docker-compose*.yml`, `.env.example`,
`docs/dev-checklist.md`, 10 авторских тест-файлов, `runbooks/**`).

**Незакоммиченный дифф (проверен в этом прогоне) — хотфикс task-11 + QA:**
`app/agent/tools.py` (`execute_tool(..., truncate=True)`), `scheduler/jobs.py`
(`truncate=False` для отчёта), `scheduler/reports.py` (длина в public-ошибке),
`ui/router.py` (run-now без дубля префикса), авторские тесты
(`test_scheduler_jobs.py`, `test_admin_ui_report_settings.py`), мои QA-тесты
(`test_scheduler_qa.py`, `test_admin_ui_report_qa.py`) и этот отчёт — 9 файлов.

Проверялись критерии приёмки контракта §4–§7 и постановки задачи, а не
намерения авторов. Продуктовый код и авторские тесты QA **не правил**;
дефекты/наблюдения только фиксировались ниже, замечания переданы авторам.

---

## 2. Фактические прогоны

Окружение: Windows, `backend/.venv/Scripts/python.exe`, временная sqlite
(env `NETOPS_DATABASE_URL` подменяется до импорта `app.*`), сеть не используется.
Рабочая `backend/netops.db` не изменялась (mtime 09.09.2026 20:02 — задолго
до прогонов; все прогоны шли на временных БД).

### 2.1 Консолидированный раннер (из корня репозитория)

| Команда | Код | Итог |
|---|---|---|
| `python run_tests.py` (**итоговый**, после хотфикса task-11 и фиксов) | **0** | **всего 38 · OK 38 · FAIL 0 · MISSING 0**, время **2m 40s** |

Наборы 23–35 в реестре `SUITES` — новые (13 = 10 авторских + 3 независимых QA),
порядок: после backend-наборов, перед chainlit; механика `OK/FAIL/MISSING`
не менялась; всего наборов **38** = 25 старых + 13 новых.

| № | Набор | Последний прогон |
|---|---|---|
| 23 | `tests/test_rag_store.py` | OK (1.0s) |
| 24 | `tests/test_rag_indexer.py` | OK (3.8s) |
| 25 | `tests/test_rag_service.py` | OK (1.6s) |
| 26 | `tests/test_rag_tool.py` | OK (2.8s) |
| 27 | `tests/test_scheduler_service.py` | OK (1.9s) |
| 28 | `tests/test_scheduler_jobs.py` | OK (3.2s) |
| 29 | `tests/test_email_delivery.py` | OK (3.1s) |
| 30 | `tests/test_report_parsing.py` | OK (0.8s) |
| 31 | `tests/test_settings_store.py` | OK (2.0s) |
| 32 | `tests/test_admin_ui_report_settings.py` | OK (15.5s) |
| 33 | `tests/test_rag_pipeline_qa.py` (QA) | OK (2.7s) |
| 34 | `tests/test_scheduler_qa.py` (QA) | OK (4.8s) |
| 35 | `tests/test_admin_ui_report_qa.py` (QA) | OK (14.3s) |

Предыдущие полные прогоны: 3m 55s (после фиксов C1/C2/C3/C4/O-1/O-2/O-3) и
2m 40s (после хотфикса `truncate`/task-11) — оба **38/38 OK**; в шапке — последний.

Промежуточный прогон 38 наборов в момент, когда авторы ещё вносили фиксы, дал
транзиентные **36 OK / 2 FAIL** (`test-admin`-наборы: маршрут уже обновлён, тесты
ещё нет) — это было состояние незавершённого изменения, а не дефект кода; Lead
уведомлён сразу, после стабилизации дерева итоговый прогон **38/38 OK**.
Предсуществующий флейк `tests/test_crypto.py` — **OK**.
Регресс старых наборов: 25/25 OK, в т.ч. `admin_ui_test.py`,
`test_admin_ui_settings.py`, `test_agent_tools_rbac.py`, `test_e2e_flow.py`,
`test_mock_mode.py`, `test_ui_switch.py`.

### 2.2 Независимые QA-наборы (свои файлы роли `qa`) — теперь часть раннера

Три QA-набора зарегистрированы в `run_tests.py` (позиции 33–35, по просьбе Lead)
и входят в итоговые 38/38 OK. Дополнительно они прогнаны отдельно для точных
счётчиков (тот же интерпретатор, cwd `backend/`):

| Команда | Код | Итог |
|---|---|---|
| `.\.venv\Scripts\python.exe -m pytest tests/test_rag_pipeline_qa.py -q` | 0 | **8 passed** |
| `.\.venv\Scripts\python.exe -m pytest tests/test_scheduler_qa.py -q` | 0 | **21 passed** |
| `.\.venv\Scripts\python.exe -m pytest tests/test_admin_ui_report_qa.py -q` | 0 | **30 passed** |

Итого независимых QA-проверок: **59 passed, 0 failed**.
Авторские новые наборы отдельно: **230 passed, 1 skipped**
(symlink-тест индексатора — нет прав на symlink в этой среде).
Всего по новым фичам: **289 passed + 1 skipped**, и все 38 наборов раннера зелёные.

### 2.3 CLI переиндексации `backend/reindex_rag.py` (временная БД)

| Команда | Код | Факт |
|---|---|---|
| `reindex_rag.py --status` | 0 | JSON статуса (`enabled:false`, `documents:0`, `docs_dir` = каталог runbook'ов) |
| `reindex_rag.py` (при `NETOPS_RAG_ENABLED=false`) | 1 | `{"ok": false, "errors": ["RAG выключен (NETOPS_RAG_ENABLED=false)"]}` |

Коды выхода соответствуют docstring CLI; рабочая БД не затрагивалась
(использовалась временная sqlite через env).

### 2.4 secret-scan (`.dsh/tools/secret-scan.ps1 -Mode quick`)

| Область | Код | Находки |
|---|---|---|
| `-Path backend\app\rag` | 0 | 0 |
| `-Path backend\app\scheduler` | 0 | 0 |
| `-Path backend\app\ui` | 0 | 0 |
| `-Path backend\app\templates` | 0 | 0 |
| `-Path runbooks` | 0 | 0 |
| `-Path docs\features` | 0 | 0 (4 файла: дизайн, этот отчёт, security-verdict, review) |
| копии 3 новых QA-тестов + `run_tests.py` | 0 | 0 |
| **только добавленные строки диффа + все новые файлы** (42 файла после фиксов; повторно после хотфикса task-11 — 9 файлов незакоммиченного диффа) | **0** | **0** |
| весь репозиторий (для контекста) | 2 | **33 high**, 0 medium/low, 1221 файл |

Репозиторный FAIL — **предсуществующий** и не связан с диффом: находки в
`.deps-local/cryptography/**`, `.tools/site/cryptography/**` (сторонние
библиотеки), `backend/internal_api_test.py`, `backend/internal_ext_test.py`,
`backend/seed_devices.py`, `backend/tests/test_agent_limit.py`,
`test_agent_settings.py`, `test_e2e_flow.py`, `chainlit/README.md`,
`docker-compose.dev.yml` (только **старые** строки 33/38/75/77 —
добавленные строки сканированы отдельно, находок 0).
**Дифф не добавляет ни одной находки.** Замечание: в design §6.8 базлайн
указан как «23 high / 46 medium», фактически сейчас 33 high / 0 medium —
базлайн в документе устарел (см. наблюдение O-6).

---

## 3. Матрица «критерий приёмки → проверка → результат»

Легенда: **PASS** — подтверждено фактическим прогоном; **PASS\*** —
подтверждено только с подменами (реальная внешняя система недоступна, см. §5).

### 3.1 RAG по runbook'ам (design §4, §6.5, §7)

| # | Критерий приёмки | Чем проверено | Результат |
|---|---|---|---|
| R-1 | Индекс → поиск сквозным путём: файлы `.md/.txt` индексируются, поиск возвращает JSON `{query, hits:[{source,heading,chunk,score,text}]}` | `test_rag_pipeline_qa.py::test_e2e_index_search_tool_audit` (реальные indexer+service+execute_tool, Qdrant = `httpx.MockTransport`, эмбеддинги = `MockEmbeddings`) | **PASS\*** |
| R-2 | Payload точки: относительный `source`, `sha256`, номер чанка, текст; без абсолютных путей | тот же тест (проверка всех payload) | PASS |
| R-3 | Инкрементальность по sha256: неизменённый файл пропускается, изменённый переиндексируется, удалённый вычищается из Qdrant и `rag_documents` | `test_incremental_reindex_and_deleted_document` | PASS\* |
| R-4 | Инструмент `search_internal_docs`: регистрация, схема `query/required`, RBAC все роли, мок-режим | авторский `test_rag_tool.py` (10) + мой e2e (реальный `execute_tool`, аудит `ok`) | PASS |
| R-5 | Ошибки RAG (выключен / Qdrant недоступен) → `status="error"` и понятный JSON, не исключение; аудит пишет `execute_tool` | `test_rag_disabled_degrades_without_exception`, `test_qdrant_unavailable_degrades_without_exception` | PASS\* |
| R-6 | Эмбеддинги вернули HTTP 500 → деградация без исключения, без «молчаливого» индекса | `test_embeddings_http_500_degrades` (реальный `OpenAICompatibleEmbeddings` с подменой `httpx` только в модуле эмбеддингов) | PASS\* |
| R-7 | Пустой/отсутствующий каталог документов → понятный статус/ошибка, без 500 | `test_empty_and_missing_docs_dir` | PASS |
| R-8 | Файл больше `NETOPS_RAG_MAX_FILE_BYTES` пропускается, валидные индексируются | `test_oversized_file_skipped_valid_one_indexed` | PASS |
| R-9 | Path traversal: файлы вне `NETOPS_RAG_DOCS_DIR` не индексируются | `test_path_traversal_via_junction_rejected` (junction наружу + прямой контроль `_safe_source`); symlink-вариант у автора — skipped (нет прав) | PASS (junction) / **не проверено** (symlink) |
| R-10 | Контракт REST-клиента Qdrant (`health/collection_info/ensure_collection/upsert/delete/search`), ошибки без URL и ключа | авторский `test_rag_store.py` (25) | PASS\* |
| R-11 | `reindex()` устойчив к сбоям отдельного файла/хранилища, возвращает `errors[]` и `ok=false` | мои R-5…R-8 + авторский `test_rag_indexer.py` (19) | PASS\* |
| R-12 | CLI `backend/reindex_rag.py` коды 0/1 и машинный вывод | §2.3 | PASS |
| R-13 | **C2**: размер файла проверяется до чтения (`stat`) + чтение не более лимита+1 байт; выросший между проверкой и чтением файл не читается целиком | авторский `test_rag_indexer.py` (19, включая oversized/race-кейсы) + мой `test_oversized_file_skipped_valid_one_indexed` | PASS (кроме реальной гонки, см. §5.11) |

### 3.2 Утренний отчёт и планировщик (design §5, §6.1–§6.4, §7)

| # | Критерий приёмки | Чем проверено | Результат |
|---|---|---|---|
| S-1 | Реестр runtime-ключей: `DEFAULTS`, `SECRET_KEYS`, `TIME_RE`, `SECURITY_MODES`, лимиты | авторский `test_settings_store.py` (23), `test_scheduler_service.py` (13) | PASS |
| S-2 | Разбор реальной формы JSON `get_infrastructure_health`: все переменные шаблона из §5.2, группировка severity, рендер дефолтных шаблонов | `test_parse_real_health_json_contract` (JSON мок-инструмента, форма идентична реальной) + `test_real_tool_report_shape_when_zabbix_fails` (реальный `get_infrastructure_health`, Zabbix подменён) + авторский `test_report_parsing.py` (29) | PASS\* |
| S-3 | Diff new/resolved между двумя прогонами; снимок `scheduler.last_problem_keys`; лимит 500 | `test_diff_new_and_resolved_between_runs` + авторский тест лимита | PASS |
| S-4 | Неизвестная переменная Jinja2 → пустая строка, не 500 | `test_unknown_template_variable_renders_empty` + авторские | PASS |
| S-5 | Сквозной прогон: health → шаблоны → SMTP → `scheduler.last_run_*` + `AuditLog(tool="morning_report")` | `test_report_job_end_to_end` | PASS\* (SMTP подменён) |
| S-6 | SMTP-режимы `ssl` / `starttls` через подмену `smtplib` | `test_smtp_modes_end_to_end` (parametrize) + авторский `test_email_delivery.py` (48) | PASS\* |
| S-7 | Инъекция `\r\n`: тема санитизируется, адреса с CR/LF отклоняются, прямой `send_email` бросает `DeliveryError` | `test_header_injection_end_to_end` | PASS |
| S-8 | Секрет SMTP не попадает в письмо/логи/аудит/`last_run_message`; в БД — шифротекст | `test_report_job_end_to_end` (caplog DEBUG, тело письма, audit, app_settings) + `test_settings_store` авторский | PASS |
| S-9 | Пустой пароль из формы не затирает сохранённый | `test_empty_password_does_not_overwrite_saved` + `test_empty_password_keeps_saved_secret` (админка) | PASS |
| S-10 | Планировщик не стартует при `NETOPS_SCHEDULER_ENABLED=false` и в тестах; `apply_settings/start/stop` идемпотентны | `test_scheduler_not_started_when_env_disabled`, `test_lifespan_does_not_start_scheduler` | PASS |
| S-11 | Ошибки доставки (нет получателей, SMTP недоступен) → `ok=false` в результате, без raise, с аудитом | `test_run_job_now_no_recipients`, `test_run_job_now_smtp_unavailable` | PASS\* |
| S-12 | Ошибка шаблона/JSON отчёта не роняет задание | авторский `test_scheduler_jobs.py` (14) + A-11 (O-3) | PASS |
| S-13 | **C1**: режим `security=none` с учётной записью на не-loopback хосте запрещён (`DeliveryError`, письмо не уходит); без логина — разрешено; для loopback (`localhost`/IPv4/`::1`) — разрешено | `test_smtp_none_with_user_on_external_host_denied`, `test_smtp_none_without_user_allowed`, `test_smtp_none_with_user_on_loopback_allowed` (3 адреса) | PASS\* (SMTP-транспорт подменён) |
| S-14 | **C3**: раздутое тело письма (шаблон из админки) обрезается до 100 000 символов, в лог — только длина | `test_huge_rendered_body_is_capped` | PASS |
| S-15 | **C4**: некорректное имя SMTP-хоста (схема/порт/путь) отклоняется до подключения, значение не утекает в текст ошибки | `test_smtp_host_with_scheme_rejected` | PASS |
| S-16 | **O-2**: детали ошибки инструмента — только в серверный лог, не в `result.message`/`last_run_message`/аудит | `test_tool_error_details_not_leaked_to_telemetry` | PASS |
| S-17 | **Хотфикс task-11**: большой валидный JSON `get_infrastructure_health` (> `MAX_RESULT`) не обрезается планировщиком (`execute_tool(..., truncate=False)`), у агента дефолт `truncate=True` сохранён; задание на таком отчёте отправляется (регресс «ReportError: некорректный JSON») | `test_big_health_json_not_truncated_and_report_sent` (реальный `execute_tool` + инструмент на 600 проблем, JSON > 20 000), `_patch_health` фиксирует `truncate=False` в вызове задания | PASS |

### 3.3 Админка и безопасность (design §5.2, §6)

| # | Критерий приёмки | Чем проверено | Результат |
|---|---|---|---|
| A-1 | Секции «Утренний отчёт» и «RAG» на `/admin/settings`, HTMX-фрагменты, поля формы и подсказка переменных | `test_settings_page_sections_and_no_secret`, `test_partials_*` + авторский `test_admin_ui_report_settings.py` (30) | PASS |
| A-2 | RBAC: только `admin`; viewer/engineer → 403 на 6 маршрутах, аноним → редирект на логин | `test_rbac_non_admin_denied_on_all_report_and_rag_routes`, `test_anonymous_redirected_to_login` | PASS |
| A-3 | Серверная валидация: время `HH:MM`, порт 1..65535, security из списка, получатели валидны и ≤20, шаблон ≤20000, имя SMTP-хоста | `test_save_validation_error_inline_and_no_changes` (16 кейсов), `test_save_too_many_recipients_inline` | PASS |
| A-4 | Ошибки валидации — инлайн (статус 200), мусор не сохраняется | там же (после ошибки `app_settings` пуст) | PASS |
| A-5 | Пароль SMTP: в HTML только «Пароль задан, N симв.», значение/шифротекст не утекают; `apply_settings()` вызывается после сохранения | `test_save_valid_form_persists_and_applies`, `test_secret_not_in_html_or_logs` | PASS |
| A-6 | Кнопки «Тестовое письмо», «Запустить отчёт», «Переиндексировать» → инлайн-результат, никогда не 500 | `test_test_email_endpoint_graceful_and_no_secret`, `test_run_now_endpoint_graceful`, `test_reindex_endpoint_graceful` | PASS |
| A-7 | RAG-эндпоинт переиндексации без 500 при выключенном RAG (реальный сервис) | `test_reindex_endpoint_with_real_disabled_rag` | PASS |
| A-8 | Фрагменты админки устойчивы к падению scheduler/rag-модулей (не 500) | `test_partials_resilient_to_module_failures` | PASS |
| A-9 | Сообщение о последнем запуске экранируется в HTML (нет XSS через лог/ошибку инструмента) | `test_last_run_message_is_html_escaped` | PASS |
| A-10 | existing-страницы/секции не сломаны (параметры агента, проверки LLM/Zabbix/VMware) | авторский `test_admin_ui_report_settings.py::test_agent_partial_still_works`, `test_admin_ui_settings.py` (в раннере) | PASS |
| A-11 | **C1-UI / C4-UI / O-1 / O-3**: учётка без TLS на внешнем хосте отклоняется; loopback разрешён; `https://host`/`host:port` в «Сервере» отклоняются; битый Jinja2-шаблон не сохраняется; текст исключения не попадает в HTML | `test_save_validation_error_inline_and_no_changes` (+C4/C1/O-3 кейсы), `test_save_none_with_user_on_loopback_allowed`, `test_test_email_endpoint_graceful_and_no_secret`; авторские `test_save_no_tls_with_user_rejected`, `test_save_no_tls_loopback_allowed`, `test_save_host_with_scheme_or_port_rejected`, `test_save_broken_*_template_rejected`, `test_test_email_error_no_500` | PASS |
| A-12 | **task-11**: кнопка «Запустить отчёт сейчас» показывает `result.message` как есть (без дубля «Отчёт не отправлен: Отчёт не отправлен: …»), ровно одно вхождение префикса; при пустом message — фолбэк + число получателей | `test_run_now_endpoint_graceful` (сообщение с префиксом, успех, фолбэки) | PASS |

---

## 4. Дефекты и наблюдения

Блокеров и major-дефектов **нет**: итоговый прогон 38/38 OK, 59 независимых
QA-проверок зелёные. Дефекты O-1/O-2/O-3, замечания безопасности
C1/C2/C3/C4 и баг из эксплуатации task-11 исправлены авторами; ниже — что
именно проверено после фиксов, затем оставшиеся информационные наблюдения.

### 4.1 Исправлено после фиксов и проверено QA (2026-10-19, вечер)

| № | Что исправлено | Файлы (после фикса) | Чем проверено | Результат |
|---|---|---|---|---|
| C1 | `security=none` с SMTP-учётной записью запрещён для не-loopback хоста (`DeliveryError` до подключения); loopback/без логина — разрешены; то же в UI-валидации формы (C1-UI) | `backend/app/scheduler/delivery.py`, серверная валидация в `ui/router.py` | `test_smtp_none_with_user_on_external_host_denied`, `test_smtp_none_without_user_allowed`, `test_smtp_none_with_user_on_loopback_allowed` (localhost / IPv4-loopback / `::1`), `test_save_none_with_user_on_loopback_allowed`, `test_save_validation_error_inline_and_no_changes` (C1-кейс); авторские `test_save_no_tls_with_user_rejected`, `test_save_no_tls_loopback_allowed` | **PASS** |
| C2 | Индексатор проверяет размер до чтения (`stat`) и читает не более лимита+1 байт (защита от гонки `stat`→`read`) | `backend/app/rag/indexer.py` | авторский `test_rag_indexer.py` (19, включая oversized/race-кейсы) + мой `test_oversized_file_skipped_valid_one_indexed` | **PASS** |
| C3 | Тело письма из шаблона админки ограничено 100 000 символов; в лог — только длина | `backend/app/scheduler/jobs.py` | `test_huge_rendered_body_is_capped` | **PASS** |
| C4 | Имя SMTP-хоста валидируется (hostname/IP без схемы, порта, пути, пробелов); значение не попадает в текст ошибки; то же в UI (C4-UI) | `backend/app/scheduler/delivery.py`, `ui/router.py` | `test_smtp_host_with_scheme_rejected`, `test_save_validation_error_inline_and_no_changes` (C4-кейсы: `https://host`, `host:port`); авторский `test_save_host_with_scheme_or_port_rejected` | **PASS** |
| O-1 | Текст непредвиденного исключения больше не выводится в инлайн-HTML — только тип ошибки (полный текст в серверный лог); `DeliveryError` тоже показывается как тип | `backend/app/ui/router.py` (`_exc_kind`, кнопки/статусы) | `test_test_email_endpoint_graceful_and_no_secret` (в HTML только `ошибка доставки (DeliveryError)`, без деталей), `test_reindex_endpoint_graceful`, `test_run_now_endpoint_graceful`; авторские `test_test_email_error_no_500`, `test_run_now_error_no_500` | **PASS** |
| O-2 | Детали ошибки `get_infrastructure_health` пишутся только в серверный лог; в `result.message`, `scheduler.last_run_message` и аудит — только публичный статус | `backend/app/scheduler/jobs.py` (`_fetch_health`, `_exc_public_message`) | `test_tool_error_details_not_leaked_to_telemetry` (маркер `internal-detail-77` отсутствует в message/audit/last_run, присутствует в caplog) | **PASS** |
| O-3 | Битый Jinja2-шаблон отклоняется при сохранении формы (инлайн, статус 200), а не при плановом запуске | `backend/app/ui/router.py` (`settings_report_save`) | `test_save_validation_error_inline_and_no_changes` (O-3-кейсы для темы и тела); авторские `test_save_broken_body_template_rejected`, `test_save_broken_subject_template_rejected` | **PASS** |
| task-11 (хотфикс из эксплуатации) | «`ReportError: некорректный JSON`» при ручном запуске: `execute_tool` получил `truncate: bool = True` (прежнее поведение для агента), планировщик вызывает `truncate=False`; `parse_health_report` в public-ошибке даёт длину; UI run-now показывает `message` как есть (без дубля префикса) | `app/agent/tools.py`, `scheduler/jobs.py`, `scheduler/reports.py`, `ui/router.py` | `test_big_health_json_not_truncated_and_report_sent` (большой JSON > `MAX_RESULT` через реальный `execute_tool`: дефолт обрезает и ломает JSON, `truncate=False` — полный валидный JSON, отчёт уходит); `_patch_health` теперь проверяет, что задание передаёт `truncate=False`; `test_run_now_endpoint_graceful` (A-12) | **PASS** |

Дополнительно проверена изоляция тестов: реальный invoke `get_infrastructure_health`
в `test_real_tool_report_shape_when_zabbix_fails` больше не зависит от порядка
запуска (явное выключение мок-режима в autouse-фикстуре + отдельная пустая БД
для инвентаря в самом тесте); проверено одиночно и в общем каталоге.

### 4.2 Оставшиеся информационные наблюдения (не блокируют)

### O-4 · info · поведение `reindex` на пустом каталоге
* `index_status()` сообщает `error="В каталоге документов нет файлов…"`,
  а `reindex()` на том же каталоге возвращает `ok=true, indexed=0` без
  ошибок. Не дефект (индексировать нечего), но сообщения расходятся;
  зафиксировано тестом `test_empty_and_missing_docs_dir`.
* владелец: `backend-dev-rag` (при желании — согласовать текст).

### O-5 · info · Windows junction внутри каталога runbook'ов
* Проверено пробой: `os.walk(followlinks=False)` **заходит** в NTFS
  junction (в отличие от symlink), но `_safe_source` через
  `Path.resolve()` отбрасывает файл вне `NETOPS_RAG_DOCS_DIR`
  (`путь вне каталога документов отброшен`). Утечки нет; каждый такой
  путь добавляет `error` и делает `ok=false` — «безопасный отказ».
  Желательно не размещать junction'ы внутри тома runbook'ов.
* владелец: `devops` (runbook-том монтируется `:ro`) / `backend-dev-rag`.

### O-6 · info · базлайн secret-scan в design §6.8 устарел
* В документе: «предсуществующие 23 high / 46 medium»; фактически
  `-Mode quick` по репозиторию: **33 high / 0 medium** (см. §2.4).
  Все находки — вне диффа; дифф не добавляет ни одной.
* владелец: `security-guard` / `devops` (обновить базлайн и, при желании,
  исключить `.deps-local/**`, `.tools/site/**` из репозиторного скана).

### O-7 · info · аудит утреннего отчёта не различает ручной и плановый запуск
* `jobs._record_result` всегда пишет `arguments={"trigger": "scheduler"}`.
  Контракт §5 требует только `AuditLog(tool="morning_report")`, поэтому
  это не дефект, но при разборе инцидентов полезно различать кнопку и cron.
* владелец: `backend-dev-scheduler` (опционально).

---

## 5. Не проверено (и почему)

1. **Реальный Qdrant и сервер эмбеддингов** — в среде нет docker-демона и
   доступа к embedding-серверу; все проверки RAG — с `httpx.MockTransport` и
   `MockEmbeddings` (design §7). Не проверены: реальная размерность модели,
   пин образа Qdrant, сетевые таймауты, качество релевантности выдачи.
2. **Реальный SMTP/Zimbra** — учётной записи нет (design §10.2); проверены
   режимы ssl/starttls, политика C1 (`none` только без логина или для
   loopback), C4-валидация хоста и ошибки через подмену `smtplib`; реальное
   TLS-соединение, живой loopback-релей (например, `smtp4dev`) и кодировка
   писем на Zimbra не проверялись.
3. **docker/compose** — нет docker-демона: `docker compose config/build`,
   фактический старт Qdrant-сервиса, монтирование `runbooks/` в контейнер,
   nginx-маршруты и образ backend не проверялись; изменения
   `docker-compose*.yml` — зона `devops`/`security-guard`.
4. **Живые Zabbix / VMware vCenter / AD** — недоступны; форма отчёта
   проверена на реальной функции инструмента с подменённым Zabbix-клиентом
   и на мок-данных, но не на реальных алертах (severity/форматы времени).
   Для task-11 регресс-тест использует синтетический отчёт на 600 проблем
   (> `MAX_RESULT`) через **реальный** `execute_tool`; реальный JSON парка с
   живого Zabbix (фактический размер/форма) не снимался.
5. **Реальный запуск планировщика** по cron/настенному времени и смена
   таймзоны/DST — проверяются только объекты заданий и состояние
   (`get_state`, `apply_settings`), без ожидания срабатывания; путь
   `NETOPS_SCHEDULER_ENABLED=true` с реальным `AsyncIOScheduler` независимо
   не поднимался (юниты автора проходят).
6. **Визуальная часть UI** — HTML/HTMX поведение в браузере, вёрстка,
   наличие tailwind-классов в собранном CSS, скриншоты — не проверялись
   (только наличие полей/маркеров и статусы ответов).
7. **Symlink-атака на индексатор** — создание симлинков в этой среде без
   прав; symlink-тест автора skipped. Проверен эквивалентный вектор через
   junction (O-5).
8. **Нагрузка/большой корпус** — реальных runbook'ов, кроме
   `runbooks/example-runbook.md`, нет; производительность индексации и
   поведение при тысячах документов не измерялись.
9. **`secret-scan -Mode full` и `-IncludeNetwork`** — полный режим и сетевые
   правила не запускались (quick достаточно по DoD; сетевые правила — зона
   `security-guard`).
10. **Аутентификация AD/SSO в админке** — использовались dev-логин и JWT
    cookie, как в существующих тестах админки; реальный AD-вход не
    проверялся.
11. **C2 «гонка» `stat`→`read`** — защита реализована (проверка размера до
    чтения + `read(limit+1)`), но реальная конкурентная запись файла в момент
    индексации не воспроизводилась (нет управляемой гонки в этой среде);
    проверены oversized/лимитные ветки.
12. **C1 на живом SMTP** — политика «без TLS только loopback/без логина»
    проверена через подмену `smtplib` и UI-валидацию; реальный отказ
    Zimbra/релея на такой конфигурации не проверялся (сервера нет).
13. **C3 на реальном MTA** — обрезка до 100 000 символов проверена
    синтетическим шаблоном; приём такого письма живым SMTP/Zimbra не
    проверялся.
14. **C4-UI в браузере** — формы с невалидным «Сервером» проверены через
    TestClient (инлайн-ошибка, ничего не сохраняется); HTMX-поведение в
    браузере — в п. 6.

---

## 6. Риски и регресс

* **Регресс существующего поведения**: `python run_tests.py` — все 25 старых
  наборов и 13 новых зелёные (38/38 OK), включая админку, RBAC инструментов,
  e2e, mock-режим, SNMP, принтеры, crypto. Новых падений нет.
* **Системный промпт агента** (`app/api/chat.py`) получил пункт 15 про
  `search_internal_docs`; тесты промпт не проверяют (поведение агента с
  живой LLM не тестировалось) — риск только поведенческий, не ломающий.
* **Размер `TOOLS_SCHEMA`** вырос 24→25 — тесты RBAC/агента проходят;
  потребители, завязанные на число инструментов, обновлены (авторский тест
  фиксирует 25).
* **Планировщик в lifespan**: в тестах и при `NETOPS_SCHEDULER_ENABLED=false`
  не создаётся (подтверждено); в проде старт не проверялся живьём.
* **Приватность/секреты**: дифф не добавляет находок secret-scan; в HTML
  секретов нет; SMTP-пароль хранится шифротекстом. Бывшие наблюдения O-1/O-2
  (технические детали в admin-UI/телеметрии) исправлены и проверены (§4.1).
* **Поведение отправки изменено политикой C1**: конфигурация
  `security=none` + учётная запись на внешнем SMTP-хосте теперь отклоняется
  (это осознанное ужесточение; существующим деплоям нужен starttls/ssl или
  loopback-релей) — важно для документации/эксплуатации (`docs/dev-checklist.md`,
  `.env.example`).
* **Поведение `execute_tool` разделено флагом `truncate`**: у агента дефолт
  `truncate=True` (бюджет контекста LLM не изменился), у планировщика —
  `truncate=False` (полный JSON). Внутренние потребители результата не должны
  рассчитывать на обрезку по `MAX_RESULT`; аудит по-прежнему пишет срез
  4000 символов — это стоит учесть при добавлении новых фоновых задач.
* **Рабочая БД**: тесты и CLI-прогон изолированы (временная sqlite),
  `backend/netops.db` не изменялась (mtime 09.09.2026 20:02).

---

## 7. Вердикт

**ПРИНЯТЬ (с оговорками).**

Обоснование:

1. Все критерии приёмки контракта подтверждены независимыми прогонами:
   **итоговый `python run_tests.py` → 38/38 OK, 0 FAIL, 0 MISSING,
   exit 0 (2m 40s)**; независимые QA-наборы → **59 passed, 0 failed**;
   авторские новые наборы → **230 passed, 1 skipped** (symlink — нет прав).
2. Регресс старых 25 наборов отсутствует; предсуществующий флейк
   `test_crypto.py` зелёный.
3. Целевые secret-scan чисты (в т.ч. отдельный скан 42 файлов «только
   добавленные строки диффа + новые файлы» — 0 находок); рабочая БД не
   затронута; новых зависимостей не появилось.
4. Блокеров и major-дефектов нет. Дефекты O-1/O-2/O-3, баг из эксплуатации
   task-11 (`truncate`/битый JSON) и замечания безопасности C1/C2/C3/C4
   исправлены авторами, проверены после фиксов (§4.1): C1 (SMTP без TLS),
   C2 (лимит файла до чтения), C3 (лимит тела письма), C4 (валидация имени
   SMTP-хоста), O-1 (детали исключений не в HTML), O-2 (детали ошибки
   инструмента не в телеметрии), O-3 (битый Jinja2-шаблон отклоняется при
   сохранении), task-11 (большой отчёт не обрезается; UI run-now без дубля
   префикса). Открытые O-4…O-7 — информационные, не блокируют.
5. Оговорки: реальные Qdrant/эмбеддинги/SMTP/Zabbix/VMware/AD и
   docker-развёртывание в среде недоступны — их обязательно проверить на
   сервере при первом деплое (design §9, §10, §5); до этого фичи считаются
   проверенными только на уровне моков. Политика C1 меняет требования к
   конфигурации эксплуатации — учесть при деплое. Баг task-11 воспроизведён
   синтетическим отчётом > `MAX_RESULT` через реальный `execute_tool`;
   реальный JSON парка с живого Zabbix не проверялся (§5.4).

Коммит хотфикса (task-11 + QA-правки) возможен после зелёного прогона —
он получен (38/38 OK); фичи Этапов 21–22 уже закоммичены (`0656bd5`,
`cb9d483`), вердикты `security-guard`/`reviewer` — в
`docs/features/security-verdict.md` и `docs/features/review.md`.
Коммитов в рамках QA-задачи не делалось.

---

## 8. Артефакты QA

Добавлено (только свои файлы):

* `backend/tests/test_rag_pipeline_qa.py` — 8 независимых сквозных
  RAG-проверок (индекс→поиск→инструмент→аудит, деградация, лимиты,
  path traversal).
* `backend/tests/test_scheduler_qa.py` — 21 проверка отчёта (реальная форма
  JSON инструмента, diff, SMTP-режимы + C1/C3/C4, O-2, инъекции, секреты,
  жизненный цикл планировщика; регресс task-11 «большой JSON не обрезается»;
  изоляция от порядка запуска).
* `backend/tests/test_admin_ui_report_qa.py` — 30 проверок админки (RBAC,
  валидация + C1-UI/C4-UI/O-3, маскирование секрета, устойчивость
  эндпоинтов, XSS-экранирование, O-1, контракт run-now без дубля префикса).
* `docs/features/qa-report.md` — этот отчёт.

Изменено: `run_tests.py` — добавлены 13 наборов (позиции 23–35), всего **38**;
механика и семантика `OK/FAIL/MISSING` не менялись; QA-тесты обновлены под
хотфикс task-11 (счётчики: RAG 8, отчёт 21, админка 30). Коммитов QA не
делала — фичи закоммичены Lead (`0656bd5`, `cb9d483`), хотфикс ждёт коммита.
