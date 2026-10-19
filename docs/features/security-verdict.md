# Security-вердикт: RAG по runbook'ам + утренние отчёты (SMTP/планировщик)

> Роль: `security-guard` · Ветка: `main` · Дата: 2026-10-19
> Раунд 2 (2026-10-19): точечная перепроверка закрытия C1/C2/C3/C4 и O-1/O-2/O-3;
> итог — **MERGE-READY**, детали в разделе «Раунд 2».
> Объект ревью: рабочее дерево поверх коммита `4c7214f` (фундамент: RAG/SMTP/шифрование),
> т.е. незакоммиченные изменения RAG, планировщика, админки, compose и тестов.
> Вердикт — **блокирующий**; условия C1–C8 ниже, комментарии — по разделам.
> Значения секретов в отчёте не приводятся (только имя/путь/`<redacted, N chars>`).

## Вердикт

**MERGE-READY (APPROVE)** — раунд 2, 2026-10-19.

Повторная точечная проверка закрытия условий выполнена (раздел «Раунд 2» ниже):
**C1, C2, C4 закрыты** и подтверждены пробами; **C3 закрыт по объёму** (итоговое тело
письма ограничено 100k символов до SMTP), остаточный риск — время/CPU рендера шаблона
(admin-only) — остаётся backlog до включения планировщика в проде; **C5, C7** — backlog
до прод-включения. Блокирующих находок нет, коммит после раунда 2 разрешён.

Первичный вердикт (раунд 1) был APPROVE WITH CONDITIONS; ниже сохранена его
аргументация и добавлены результаты повторной проверки.

Инъекции в SMTP-заголовки закрыты на всех трёх уровнях (форма → шаблон → `smtplib`),
SSTI в шаблонах письма невозможен (`SandboxedEnvironment`, без loader'а — include/import
не работают), секрет SMTP хранится только шифротекстом и не попадает в HTML/API/логи/аудит,
новые маршруты закрыты `require_roles_page(Role.admin)`, новые файлы диффа чисты для
`secret-scan` (exit 0 по зонам).

| # | Severity | Статус раунда 2 (2026-10-19) | Кратко | Владелец / когда |
|---|---|---|---|---|
| C1 | MEDIUM | **ЗАКРЫТО**, подтверждено пробой | `smtp.security=none` + заданный `smtp.user` → AUTH-логин/пароль открытым текстом; теперь отказ до соединения, исключение — только loopback | backend-dev-scheduler, UI — frontend-dev-admin |
| C2 | MEDIUM | **ЗАКРЫТО**, подтверждено пробой | Индексатор читал файл в память до проверки лимита; теперь stat до чтения + чтение не более лимита+1, неположительный лимит не отключает защиту | backend-dev-rag |
| C3 | MEDIUM | **ЗАКРЫТО по объёму**; остаток — backlog | Тело письма ограничено 100k символов до SMTP; время/CPU рендера шаблона не ограничено (admin-only) — до `NETOPS_SCHEDULER_ENABLED=true` в проде | backend-dev-scheduler |
| C4 | LOW | **ЗАКРЫТО**, подтверждено пробой | `smtp.host` валидируется (hostname без схемы/порта/пути/пробелов) в delivery и в UI-форме | backend-dev-scheduler + frontend-dev-admin |
| C5 | LOW | backlog (не блокирует мерж) | После ротации `NETOPS_ENCRYPTION_KEY` админка показывает «Пароль задан», не сообщая, что секрет не расшифровывается — до прод-включения отчётов | frontend-dev-admin |
| C6 | LOW | backlog | Схема URL Qdrant/эмбеддингов в коде не проверяется; значения только из env, пользовательского ввода URL нет | backend-dev-rag |
| C7 | INFO | backlog (решение devops) | Qdrant в compose без API-ключа: порт наружу не публикуется, доступ только из сети compose | devops-infra |
| C8 | INFO | backlog | Текст ошибки валидации получателей эхом содержит невалидное значение; из формы недостижимо (CRLF ловится раньше) | backend-dev-scheduler |

Итог раунда 2: **C1, C2, C4 закрыты; C3 закрыт по объёму** (остаток — backlog до прод-включения);
C5–C8 — backlog, мерж не блокируют. Коммит разрешён.

Примечание (информационно, не условие): матрицы валидации host в UI (`ui/router.py::_valid_smtp_host`)
и в delivery (`delivery.py::_validate_smtp_host`) чуть различаются: UI строже для `[..]`/завершающей
точки, delivery строже для не-loopback IPv6. Расхождение fail-closed (отказ на отправке), риска нет;
жёлательно унифицировать в одном месте в рамках C6.

---

## 1. Секреты SMTP (Fernet, app_settings)

**Проверено чтением:** `backend/app/settings_store.py`, `backend/app/crypto.py`,
`backend/app/models.py` (`AppSetting`), `backend/app/main.py::_ensure_app_settings_text_column`,
`backend/app/scheduler/{settings,delivery,jobs}.py`, `backend/app/ui/router.py` (новые маршруты
`/settings/partial/report`, `/settings/report`, `/settings/report/test-email`,
`/settings/report/run-now`), `backend/app/templates/components/settings/report_form.html`.

**Пробы (фактически, интерпретатор `backend/.venv`, sqlite tmp, TestClient, ключ Fernet
генерировался программно; тестовый пароль собирался конкатенацией, в выводе только длина):**

| Проба | Результат |
|---|---|
| сохранение пароля через `POST /admin/settings/report` | `app_settings.value` начинается с токена Fernet (`gAAAAA…`), не равен открытому тексту; `crypto.is_encrypted(stored) == True` |
| расшифровка `get_secret_setting("smtp.password")` | совпадает с введённым; значение в БД только шифротекст |
| пароль в HTML: `GET /admin/settings/partial/report` и `GET /admin/settings` | значение **отсутствует** в обоих ответах; в форме `input type=password` без атрибута значения; показывается «Пароль задан, N симв.» (N — длина **открытого** пароля, `_report_secret_meta` делает `len()` расшифровки) |
| `secret_state("smtp.password")` | `{key, is_set, length}` — только имя/флаг/длина (длина шифротекста, 120 у тестового значения), значения нет |
| пустое поле формы | сохранённый секрет не затирается (`set_settings` пропускает пустые секретные ключи), остальные поля при этом обновляются |
| аудит | `AuditLog(tool="morning_report")` пишется, `arguments={"trigger":"scheduler"}`, в `result` — только счётчики; пароля нет |
| ротация `NETOPS_ENCRYPTION_KEY` (ключ заменён в процессе + `crypto.reset_cache()`) | `settings_store.SecretDecryptError`: «Секрет smtp.password не расшифровывается текущим NETOPS_ENCRYPTION_KEY…»; ни открытого значения, ни шифротекста в сообщении/логе |
| отправка при чужом ключе | `DeliveryError` с текстом «Не удалось расшифровать секрет smtp.password — проверьте NETOPS_ENCRYPTION_KEY», без значения |
| логи (перехват root-logger на DEBUG в процессе сохранения/чтения/отправки) | открытого пароля и шифротекста в логах нет |
| битый ключ Fernet (`crypto._cached_fernet` с заведомо неверной строкой) | исключение `ValueError` с текстом о формате ключа; значение ключа в тексте отсутствует |

Вывод: пункт 1 выполнен. В API (`app/api/**`) значений `app_settings` нет — `AppSetting`
читают только `/admin/settings*` (admin) и `api/chat.py` для двух безопасных ключей агента.
Остаточное замечание (не условие): legacy-ветка `crypto.decrypt_value` вернёт не-Fernet
значение как есть — если кто-то вручную положит `smtp.password` открытым текстом в БД,
он будет принят; через штатный код запись всегда шифруется.

## 2. Инъекции и SSRF

**2a. CRLF/инъекция заголовков письма — закрыто.** Файл `backend/app/scheduler/delivery.py`
(`_check_no_newlines`, `_is_valid_email`, `_normalize_addresses`), `jobs.py::_sanitize_subject`,
`ui/router.py` (валидация темы при сохранении). Пробы с подменой `smtplib.SMTP` (сети нет):

| Проба | Результат |
|---|---|
| `send_email` с CRLF в теме (`…\r\nBcc: evil@…`) | `DeliveryError`: недопустимые символы перевода строки |
| то же с одиночным LF в теме | `DeliveryError` |
| `validate_recipients` с CRLF в строке адресов | `DeliveryError` (CRLF ловится до разбора) |
| `send_email` со CRLF внутри элемента списка получателей | `DeliveryError` (адрес не проходит regex) |
| поле отправителя в настройках = адрес с CRLF (сохранён как есть) | `DeliveryError`: некорректный адрес отправителя — значение не попадает в заголовок |
| шаблон темы содержит CRLF (записан в обход формы) | `jobs._sanitize_subject` схлопывает whitespace: «T: 19.10.2026 Bcc: x» — инъекции нет |
| форма админки: тема с переводом строки | серверная ошибка «Тема письма: перевод строки недопустим» |

Итого три независимых барьера (форма → рендер/санитайз → `smtplib`), инъекция заголовков
невозможна. `EmailMessage.set_content` для тела безопасен.

**2b. Jinja2-шаблоны письма из админки — SSTI невозможен.** `reports.py` использует
`jinja2.sandbox.SandboxedEnvironment(undefined=ChainableUndefined, autoescape=False)` без
loader'а. Пробы через `reports.render_template` (фактический вывод):

| Payload | Результат |
|---|---|
| `{{ ''.__class__.__mro__ }}` | пустая строка (атрибут заблокирован) |
| `{{ ''.__class__.__mro__[1].__subclasses__() }}` | `ReportError`/`SecurityError` на доступ к `__class__` |
| `{{ cycler.__init__.__globals__.os.popen('id').read() }}` | `SecurityError` на доступ к `__init__` |
| `{{ lipsum.__globals__['os'].popen('id').read() }}` | `SecurityError` на доступ к `__globals__` |
| `{% include '/etc/passwd' %}` / `{% import 'os' as x %}` | `TypeError` (у окружения нет loader'а) — чтения файлов нет |
| `{{ config }}`, `{{ self.__init__.__globals__ }}`, `{{ request.application.__globals__ }}`, `{{ namespace.__init__.__globals__ }}` | пустые строки |

RCE/чтение файлов/доступ к `__globals__` невозможны. **Но** sandbox не ограничивает
вычислительную сложность: `{{ 'A' * 3000000 }}` → 3 000 000 символов за 0.17 с (оператор `*`
разрешён), три вложенных цикла по `range(200)` → 1.06 с, а вложенность ×`range(100000)` не
ограничена (отдельный `range` капнут `MAX_RANGE=100000`, но вложение перемножается) →
шаблон до `TEMPLATE_MAX_CHARS=20000` символов может надолго занять поток и съесть память.
Триггер — только admin (кнопка «Запустить»/планировщик), поэтому severity MEDIUM, не HIGH →
**условие C3**.

**2c. URL Qdrant/эмбеддингов — только из env, пользовательского ввода нет.** `rag/store.py::build_store`
и `rag/embeddings.py::build_embedding_provider` читают только
`NETOPS_RAG_QDRANT_URL`/`NETOPS_EMBEDDING_*`/`NETOPS_LLM_*`; ни один UI/API-маршрут не принимает
URL (переиндексация — `POST /admin/settings/rag/reindex` без параметров, CLI — только `--force/--status`).
В сеть приложение ходит только к Qdrant, серверу эмбеддингов и SMTP-серверу из настроек;
сторонних/телеметрийных вызовов в новом коде нет. Проверка схемы `http/https` в коде
отсутствует (значение приходит из env оператора) → **C6** (hardening).

**2d. SMTP host без схемы.** Валидации host нет: сохранённый `smtp.host` проверяется только
на непустоту и уходит первым аргументом в `smtplib.SMTP/SMTP_SSL`. Проба: host со схемой
(`https://smtp.example.test`) принят и передан в сокет как есть (`connect('https://smtp.example.test', 587)`).
Практического SSRF-эффекта нет (протокол фиксирован SMTP, TLS-контекст создаётся, значение
задаёт admin), но host может содержать схему/пробелы/CRLF/порт — стоит валидировать → **C4**.
Порт валидируется (`1..65535`), режим защиты — из списка `starttls|ssl|none`.

**2e. `smtp.security=none` + заданный `smtp.user` — критичная конфигурационная ловушка.**
Проба с подменой `smtplib.SMTP`: при `security=none`, `smtp.user` и сохранённом пароле
клиент выполняет `login(user, <расшифрованный пароль>)` **без `starttls`** — учётные данные
Zimbra идут открытым текстом по сети. UI предлагает `none` в списке (`security_modes`) без
предупреждения, серверная валидация его пропускает. Это **C1 (MEDIUM, до мержа)** —
**закрыто в раунде 2** (см. раздел «Раунд 2»): запретить `none` при заданном `user`, кроме loopback-хоста,
запретить `none` при заданном `user`, кроме loopback-хоста (`localhost` / петлевой адрес),
или требовать явного подтверждения в UI; сообщение — без значений.

## 3. Индексатор (`backend/app/rag/indexer.py`)

**Проверено чтением:** `docs_dir_path` (`Path.resolve`), `_safe_source` (`resolve(strict=True)` +
`relative_to(root)` + отсев `..`), `_iter_files` (`os.walk(followlinks=False)`), лимит
`NETOPS_RAG_MAX_FILE_BYTES` (default 1 MiB), декодирование `utf-8/errors=replace`, payload точки.

**Пробы (tmp-каталоги; реальные Qdrant/эмбеддинги недоступны — только файловая часть):**

| Проба | Результат |
|---|---|
| файл внутри корня | `source='ok.md'` — принят |
| жёсткий путь вне корня (`_safe_source(root, outside/secret.txt)`) | `(None, 'путь вне каталога документов отброшен')` |
| junction-каталог внутри корня → каталог вне корня (`mklink /J`) | `os.walk` спускается в junction, но `_safe_source` **отбрасывает** файл: `path.resolve()` раскрывает reparse point → `relative_to` падает |
| файл-симлинк наружу | создать в этой среде нельзя (нет привилегии симлинков; `OSError`) — код-путь тот же `resolve+relative_to`, проверено junction'ом; **оговорка**: симлинк фактически не воспроизведён |
| файл с точками в имени (`..hidden.md`) | нормальный `source`; обход через `..` исключён `resolve()`, в `source` только путь внутри корня |
| ошибка чтения/non-existent файл | попадает в `errors` как «не удалось прочитать файл (OСШИБКА)», индексация продолжается |
| **порядок проверки размера** | **дефект (закрыт в раунде 2)**: было — `read_bytes()` до проверки `max_bytes`; стало — stat до чтения, `read(max_bytes+1)`, неположительный лимит → дефолт 1 МиБ (см. раздел «Раунд 2»), **C2** |
| бинарные файлы с расширением `.md/.txt` | отдельной проверки нет: бинарь декодируется `errors="replace"` и индексируется как текст (не уязвимость; мусор в индексе/лишние эмбеддинги) — рекомендация в C2 |
| внешние адреса в payload | payload = `{source, heading, chunk, text, sha256}` — URL/адресов не добавляется |

Дополнительно: каталог документов задаётся только `NETOPS_RAG_DOCS_DIR` (env); в compose
монтируется `./runbooks:/data/runbooks:ro` — приложение документы не пишет. Если оператор
укажет слишком широкий каталог, в индекс попадёт лишнее — это риск конфигурации (см. «Риски»).

## 4. Планировщик (`backend/app/scheduler/service.py`, `jobs.py`, `reports.py`, `delivery.py`)

| Проверка | Факт |
|---|---|
| флаг `NETOPS_SCHEDULER_ENABLED` | `start_scheduler()` при env=false — no-op: `_scheduler is None`, `get_state()["enabled"] == False`; env=true + `scheduler.enabled=false` — тоже `None`; env=false + `scheduler.enabled=true` — тоже `None` (мастер-выключатель приоритетнее) |
| повторный старт | идемпотентен (под `threading.RLock`, `_scheduler` переиспользуется) |
| задание | одно (`JOB_ID` = `morning_report`, `CronTrigger`, `max_instances=1`, `coalesce=True`, `misfire_grace_time=3600`) — дублей писем от одного процесса нет |
| ручной запуск/тестовое письмо | оба маршрута — только `require_roles_page(Role.admin)` (см. п.5); engineer/anonymous → 403/redirect (тесты + мой чтение зависимостей) |
| аудит | `jobs._record_result` пишет `AuditLog(tool="morning_report", status, result=сообщение без секретов)`; проба: 1 запись, статус ok, аргумент `{"trigger":"scheduler"}` |
| DoS через получателей | `validate_recipients` + `RECIPIENTS_MAX=20` (проверяется и в delivery, и в роутере); дубли дедуплицируются |
| DoS через шаблоны | длина шаблона ≤ `TEMPLATE_MAX_CHARS=20000` проверяется в форме; **закрыто по объёму в раунде 2** (тело ≤100k символов до SMTP), время рендера не ограничено → C3-остаток (backlog) |
| ошибки | `run_morning_report` не бросает: `ReportError`/`DeliveryError` → `status="error"` + понятный `message`; неизвестные — тип исключения, без секретов |
| `smtp.security=none` с user | см. п.2e → C1 |
| многопроцессность | `Dockerfile`: `uvicorn` без `--workers` → один процесс/один планировщик; при масштабировании репликами будут дубли писем (операционный риск, не дефект кода) |

## 5. Админка (RBAC/CSRF/IDOR/экранирование)

**Маршруты:** добавлено 6 (`GET /settings/partial/report`, `POST /settings/report`,
`POST /settings/report/test-email`, `POST /settings/report/run-now`, `GET /settings/partial/rag`,
`POST /settings/rag/reindex`) — **все** с `Depends(require_roles_page(Role.admin))`;
`require_roles_page` возвращает 403 при чужой роли (страница 403), anonymous — 401→redirect на
`/admin/login`. RBAC не ослаблен, существующие маршруты не тронуты (кроме вставки
секций в `settings.html`).

- **IDOR:** нет — ни один новый маршрут не принимает идентификатор ресурса.
- **CSRF:** модель проекта не менялась — cookie `netops_token` ставится `httponly=True, samesite=lax`
  (`ui/router.py`), новых токенов/форм с ослаблением нет; POST'ы новых действий идут в той же
  модели, что и существующие admin-действия (SameSite=Lax не отправляет cookie при cross-site POST).
  Токенов CSRF в проекте нет ни на одном admin-маршруте — это свойство существующей модели, не регресс.
- **Экранирование:** проверено **фактически**: `templates.env.autoescape` = `select_autoescape()`
  (Starlette 1.3.1 сам включает), рендер шаблона с подставленным `<script>alert('xss')</script>`
  даёт `&lt;script&gt;…` — автоэкранирование включено для `.html` и для `from_string`.
  Поля формы отчёта (`smtp_host`, `smtp_user`, тема/тело, `notice`/`error`, `last_run.message`)
  выводятся через `{{ }}` → XSS через сохранённые значения невозможен.
- **Секрет в HTML** — см. п.1 (нет).
- `_rag_status_safe`/`_report_status`/`_report_response` всегда возвращают статус 200 с
  фрагментом (htmx-паттерн), текст ошибок — тип исключения/понятное сообщение; `Traceback` в
  ответах нет (проверено тестами и моими пробами). В `test-email`/`run-now` на неизвестном
  исключении в notice попадает `str(exc)[:200]` — admin-only страница, секретов там нет
  (SMTP-ошибки обёрнуты в `DeliveryError`), но это место лучше сузить до `type(exc).__name__` (мелочь, backlog).

## 6. Сканер секретов (`secret-scan.ps1`, PowerShell 5.1 через `powershell.exe -NoProfile -ExecutionPolicy Bypass -File`)

Запуск по зонам диффа (`-Mode quick`, значения в выводе только маскированные):

| Зона | Exit | Находки | Вердикт |
|---|---|---|---|
| `backend\app\rag` | 0 | 0 | PASS |
| `backend\app\scheduler` | 0 | 0 | PASS |
| `backend\app\ui` | 0 | 0 | PASS |
| `backend\app\templates` | 0 | 0 | PASS |
| `backend\app` (весь) | 0 | 0 | PASS |
| `backend\tests` | 1 | 3 high — **все предсуществующие** (`test_agent_limit.py:94`, `test_agent_settings.py:189`, `test_e2e_flow.py:188`, правило `assigned-secret`, значения замаскированы) | новых нет |
| `runbooks` | 0 | 0 | PASS |
| `docs` (включая `docs/features`, `docs/dev-checklist.md`) | 0 | 0 | PASS |
| `docker-compose.dev.yml` (в общем прогоне репозитория) | — | 4 high на строках 33/38/75/77 — **все предсуществующие** (dev-значения); новые строки диффа (16–24, 41–45, 49–52, 103–105) находок не дали | новых нет |
| `docker-compose.yml` | — | 0 | PASS |
| `.dsh` | 0 | 0 | PASS |

С `-IncludeNetwork` (правило `network-internal`):

| Зона | Exit | Находки |
|---|---|---|
| `backend\app` | 0 | 4 medium — предсуществующие (`agent/tools.py:745`, `devices/printer_discovery.py:33,37`, `templates/pages/inventory.html:87`); в новых файлах RAG/планировщика — 0 |
| `backend\tests` | 1 | 3 high (те же 3 предсуществующих) + 28 medium network — все в старых тестах; в новых тестах — 0 |
| `runbooks`, `docs\features` | 0 | 0 |

**Корневой прогон `secret-scan.ps1 -Mode quick` по всему репозиторию дал exit 2** («прогон
не состоялся»): сканер не смог прочитать каталоги `.tmp-pip` в корне (ACL/права в этой
среде, не связано с диффом). Это **не** «чисто» по формальному коду выхода, поэтому
вердикт по пункту 6 основан на зональных прогонах выше (все зоны диффа — exit 0, новых
находок нет). Остальные 33 high корневого прогона — предсуществующий шум вне git: вендорная
`cryptography` в `.deps-local/` и `.tools/site/` (gitignored), `backend/seed_devices.py`,
старые тесты, `chainlit/README.md`, dev-compose. Они не относятся к ревьюируемому диффу;
базлайн `docs/docker-image-size/00-diagnosis.md` §7 (23 high/46 medium) в текущей среде
отличается количественно из-за локальных `.deps-local/.tools` — предсуществующая картина.

**Секретные файлы не читались:** сканер сам пометил файлы окружения (`.env`, `backend/.env`,
`.env.example`) как «значения не читались»; я их не открывал и значения не печатал.
`.gitignore` содержит правила для `.env`, `.secrets/`, `encryption_key.pem`, `*.key`,
`*.db/*.sqlite*`; `git status --short` не содержит секретных/ключевых файлов/БД,
staged-файлов нет (всё — рабочее дерево).

## 7. Внешние сервисы / внутренние адреса

- В новом коде нет обращений к внешним сервисам: только Qdrant, эмбеддинги/LLM и SMTP из
  env/app_settings; телеметрии/аналитики нет.
- Внутренние адреса/пароли в отчёте не раскрыты, значения секретов не приводились.
  Внешние `web_fetch`/`web_search` **не использовались** (запрещено политикой для внутренних данных).
- Аудит/логи/UI не содержат учётных данных: проверено пробами п.1 и чтением всех
  `log.*`/`print` в новых файлах.
- Остаточный риск конфигурации: содержимое `runbooks/` и результаты RAG-поиска уходят на
  сервер эмбеддингов/LLM. По дизайну это внутренний сервер; если `NETOPS_LLM_BASE_URL`/
  `NETOPS_EMBEDDING_BASE_URL` укажут на внешний провайдер, внутренние документы покинут
  периметр. Требование эксплуатации: эти URL — только внутренние (зафиксировать
  комментарием в `docs/dev-checklist.md`/файле-примере окружения, владелец `devops`/`architect`).

---

## Раунд 2 (2026-10-19): точечная проверка закрытия C1/C2/C3/C4 + O-1/O-2/O-3

Полное ревью не повторялось (по указанию Lead). Проверены только изменённые места:
`app/scheduler/{delivery,jobs,reports}.py`, `app/rag/indexer.py`, `app/ui/router.py`,
`components/settings/report_form.html`. Пробы — inline-python в `backend/.venv` (sqlite tmp,
`TestClient`, подмена `smtplib.SMTP`, фейковые Qdrant/эмбеддинги; сети и docker нет).

### C1 — SMTP без TLS с учётной записью

Код: `delivery._is_loopback_host` + проверка в `_load_smtp_config` (отказ при `security=none`,
непустом user и не-loopback host); зеркальная проверка в `ui/router.py` при сохранении формы.

| Проба (подмена `smtplib.SMTP`) | Результат |
|---|---|
| `none` + user + внешний host | `DeliveryError` «SMTP без TLS с учётной записью запрещён…»; **соединение не открывалось вообще** (журнал фейкового клиента пуст), `login()` не вызывался |
| `none` + user + loopback-имя (`localhost`) | разрешено: connect → ehlo → login → send, без `starttls` (осознанное исключение для локального релея) |
| `none` + user + loopback-адрес (петлевой IP) | разрешено (то же поведение) |
| `none` без user + внешний host | разрешено: connect → ehlo → send, `login()` не вызывается (нечему утекать) |
| `starttls` + user | `starttls` вызывается **до** `login` — учётные данные после TLS |
| текст ошибки C1 | не содержит ни host, ни user, ни пароля (проверено поиском значений в тексте) |
| UI-форма: `none` + user + внешний host | инлайн-ошибка «учётная запись без TLS запрещена…», настройки **не сохранены** (в БД остался прежний `starttls`) |
| UI-форма: `none` + user + loopback | сохраняется успешно |

### C2 — размер файла в индексаторе

Код: `indexer._file_size` (stat без чтения) → проверка лимита → `handle.read(max_bytes + 1)` →
повторная проверка `len(data) > max_bytes`; `_max_file_bytes`: неположительное значение → дефолт
`DEFAULT_MAX_FILE_BYTES` (1 МиБ) с предупреждением в лог.

| Проба (tmp-каталог, фейковые store/provider) | Результат |
|---|---|
| `big.md` 2 МиБ при лимите 1 МиБ (default) | файл **ни разу не открыт** (spy на `Path.open`: `big opened = False`), в `errors` — «файл больше лимита (1048576 байт), пропущен»; обычный `ok.md` проиндексирован |
| гонка «файл вырос после stat» (stat подменён на 10 байт, лимит 1 КиБ) | фактическое чтение ровно `limit + 1` = **1025 байт** (spy), затем отказ «больше лимита» — гигантский файл в память не попадает |
| `NETOPS_RAG_MAX_FILE_BYTES=0` | действующий лимит **1048576** (дефолт), предупреждение в лог — лимит не отключается |
| `NETOPS_RAG_MAX_FILE_BYTES=-5` | действующий лимит 1048576 (дефолт) |
| `NETOPS_RAG_MAX_FILE_BYTES=2097152` | лимит применяется как задан |

### C4 — валидация `smtp.host`

Код: `delivery._SMTP_HOST_RE`/`_validate_smtp_host` (hostname, ≤253, без схемы/порта/пути/
пробелов; loopback и IP — отдельно) + `ui/router.py::_valid_smtp_host` при сохранении формы.

| Host | delivery | UI-форма |
|---|---|---|
| со схемой (`https://…`) | `DeliveryError`, соединения нет | инлайн-ошибка, не сохранено |
| `host:port` | `DeliveryError`, соединения нет | инлайн-ошибка, не сохранено |
| с пробелом / слэшем / обратным слэшем / путём | `DeliveryError`, соединения нет | инлайн-ошибка, не сохранено |
| с CRLF (`…\r\nX-inject: 1`) | `DeliveryError` (не доходит до сокета) | — |
| длиннее 253 символов / `smtp..example.test` | `DeliveryError` | инлайн-ошибка |
| валидные (`smtp.example.test`, `mail-01.example.test`, завершающая точка) | приняты, connect с этим host | приняты |

### O-1 / O-2 / O-3 — гигиена ошибок

Код: `jobs._public_error` (`public` у `ReportError`, безопасный текст `DeliveryError`, иначе
только класс), `jobs._fetch_health` (детали инструмента — в лог), `ui/router.py::_exc_kind`
(в HTML — только класс/причина), проверка рендера шаблонов при сохранении (`render_template`),
`reports.ReportError(public=…)`.

| Проба | Результат |
|---|---|
| `ReportError` с внутренней деталью → `run_morning_report` | в `message` только `ReportError: <public>`; детали **нет** ни в `message`, ни в `scheduler.last_run_message`, ни в `audit_log.result`; деталь есть в серверном логе; пароля в логе нет |
| `DeliveryError` при отправке | в `message` класс + безопасный текст (SMTP-код), host/user/пароль отсутствуют |
| неизвестное исключение с внутренней деталью | в `message` только тип (`RuntimeError`), деталь — только в логе |
| UI `POST /settings/report/test-email` при `DeliveryError` с host в тексте | в HTML только «ошибка доставки (DeliveryError). Подробности в логе сервера»; host и пароль в HTML отсутствуют |
| UI `POST /settings/report/run-now` при `RuntimeError` с деталью | в HTML только `RuntimeError` + отсылка к логу; деталь/пароль в HTML отсутствуют |
| UI сохранение шаблона с синтаксической ошибкой (`{% for %}`) | инлайн «шаблон содержит синтаксическую ошибку (TemplateSyntaxError)», шаблон **не сохранён** (осталось прежнее значение) |
| регресс маскирования секрета | во фрагменте `/settings/partial/report` пароля нет, индикатор «Пароль задан» есть; пароля нет и в серверном логе |

### Jinja2 SandboxedEnvironment — повторный SSTI-прогон

`reports._env` — `jinja2.sandbox.SandboxedEnvironment` (jinja2 3.1.6), без loader'а.

| Payload | Результат |
|---|---|
| `{{ ''.__class__.__mro__ }}`, `{{ config }}`, `{{ self.__init__.__globals__ }}`, `{{ request.application.__globals__ }}`, `{{ namespace.__init__.__globals__ }}`, `{{ get_flashed_messages.__globals__ }}`, `{{ range.__globals__ }}` | пустая строка (доступ закрыт/undefined) |
| `{{ ''.__class__.__mro__[1].__subclasses__() }}`, `{{ ().__class__.__bases__[0].__subclasses__() }}` | `ReportError`/`SecurityError` на доступ к `__class__` — RCE-цепочка недоступна |
| `{{ cycler.__init__.__globals__.os.popen('id').read() }}` | `SecurityError` на `__init__` |
| `{{ lipsum.__globals__['os'].popen('id').read() }}` | `SecurityError` на `__globals__` |
| `{% include '/etc/passwd' %}`, `{% import 'os' as x %}`, `{% from 'os' import popen %}` | `ReportError`/`TypeError` — loader'а нет, чтения файлов нет |

Итог: RCE и чтение файлов из шаблона письма невозможны.

### C3 — остаточный риск после фикса

`jobs.MAX_RENDERED_BODY_CHARS = 100_000`: `_truncate_body` обрезает итоговое тело и пишет в лог
только длину; сквозная проба (шаблон тела `{{ 'A' * 300000 }}`) — отправлено тело ровно
**100 000** символов, в логе предупреждение об обрезке. **Остаётся**: сам рендер не ограничен
по времени (вложенные циклы; замер 8 млн итераций — 0.36 с, масштабируется линейно) — admin-only,
вынесено в backlog до включения планировщика в проде.

### Регрессионные прогоны раунда 2

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest tests/test_email_delivery.py tests/test_scheduler_jobs.py tests/test_rag_indexer.py tests/test_report_parsing.py -q   # 110 passed, 1 skipped
.\.venv\Scripts\python.exe -m pytest tests/test_admin_ui_report_settings.py tests/test_settings_store.py -q                                                  # 52 passed
```

`secret-scan.ps1 -Mode quick` по изменённым зонам (`backend\app\scheduler`, `backend\app\rag`,
`backend\app\ui`, `backend\app\templates`) — **exit 0, находок 0** в каждой.

## Риски (остаточные, принятые)

1. RAG-инструмент `search_internal_docs` доступен **всем** ролям (viewer/engineer/admin) —
   решение контракта §4.2, подтверждаю: документы read-only и внутренние; требование
   эксплуатации — **не хранить в `runbooks/` секретов и учётных данных** (сейчас сканер
   по `runbooks/` чист).
2. `runbooks/` — том на сервере (`:ro` для контейнера): ошибка оператора с корнем каталога
   или подкладом может расширить корпус; контент уходит в Qdrant/LLM (внутренние).
3. Qdrant без API-ключа в compose; порт наружу не публикуется, доступ — только из сети
   compose (C7).
4. `smtp.security=none` разрешён кодом (C1) — до фикса не включать в проде.
5. Многопроцессный запуск app даст дубли утренних писем (операционный риск).
6. Легаси-ветка `crypto.decrypt_value` принимает нешифрованное значение (только при ручной
   записи в БД).
7. Admin-страница `/admin/settings` показывает `smtp.user`/`smtp.host` — это не секреты;
   доступ только admin.

## Не проверено (честно, с причиной)

- Реальные Qdrant / SMTP Zimbra / сервер эмбеддингов: в среде нет docker-демона и сервисов —
  проверялось подменами `smtplib.SMTP`, `httpx`-клиентов и MockEmbeddings; сетевые сценарии
  (TLS-хендшейк, AUTH Zimbra, коды SMTP, Qdrant REST) — **на стенде, не здесь**.
- `docker compose config` и реальный подъём стека: docker CLI есть, демона нет; compose
  проверял `devops`; мной **не** перепроверялся.
- Создание файловых симлинков на этом Windows-хосте невозможно (нет привилегии) — защита
  от симлинков проверена через junction (каталог-репарсинг): файл отброшен корректно.
- Полный `run_tests.py` — зона `qa`; независимо прогонял наборы по зонам (см. ниже),
  кросс-файловый прогон `test_admin_ui_report_settings.py` вместе с RAG-наборами даёт ошибки
  изоляции окружения (каждый файл выставляет переменные `NETOPS_*` до импорта `app`) — это
  вопрос тестовой инфраструктуры (`qa`/`backend-dev`), не продукта; по отдельности — зелёные.
- Браузерное поведение HTMX/SameSite — анализировал по коду/заголовкам cookie, без браузера.
- Ротация ключа на реальной Postgres-БД (проверено in-process на sqlite).
- Внешние веб-сервисы не проверялись и не опрашивались (не требуется и запрещено для
  внутренних данных).

## Выполненные команды проверок (фактические результаты)

```powershell
# сканер по зонам диффа (PowerShell 5.1)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .dsh\tools\secret-scan.ps1 -Path backend\app\rag -Mode quick        # exit 0, 0 находок
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .dsh\tools\secret-scan.ps1 -Path backend\app\scheduler -Mode quick  # exit 0, 0
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .dsh\tools\secret-scan.ps1 -Path backend\app\ui -Mode quick         # exit 0, 0
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .dsh\tools\secret-scan.ps1 -Path backend\app\templates -Mode quick  # exit 0, 0
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .dsh\tools\secret-scan.ps1 -Path backend\tests -Mode quick         # exit 1: 3 high, все предсуществующие
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .dsh\tools\secret-scan.ps1 -Path runbooks -Mode quick             # exit 0, 0
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .dsh\tools\secret-scan.ps1 -Path docs -Mode quick -IncludeNetwork # exit 0, 0
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .dsh\tools\secret-scan.ps1 -Mode quick -Json                        # exit 2: см. оговорку про .tmp-pip

# тесты (интерпретатор backend/.venv, cwd backend)
.\.venv\Scripts\python.exe -m pytest tests\test_settings_store.py tests\test_email_delivery.py -q              # 59 passed
.\.venv\Scripts\python.exe -m pytest tests\test_scheduler_service.py tests\test_scheduler_jobs.py tests\test_email_delivery.py tests\test_report_parsing.py -q  # 89 passed
.\.venv\Scripts\python.exe -m pytest tests\test_admin_ui_report_settings.py -q                                 # 22 passed
.\.venv\Scripts\python.exe -m pytest tests\test_rag_indexer.py tests\test_rag_service.py tests\test_rag_store.py tests\test_rag_tool.py -q  # 62 passed, 1 skipped
```

Плюс собственные пробы (inline-python, без записи файлов в репозиторий): шифрование и маскирование
секрета через TestClient, ротация ключа, CRLF-инъекции с подменой `smtplib`, SSTI-payload'ы
(`__class__/__mro__/__globals__/config/include/import`), DoS-пробы Jinja2, junction/симлинки и
порядок чтения файла в индексаторе, гейтинг планировщика по флагам, аудит `morning_report`,
autoescape админ-шаблонов, RBAC инструмента `search_internal_docs` для трёх ролей.

## Что осталось / условия снятия вето

Статус после раунда 2 (2026-10-19): **вето снято, мерж разрешён** — C1, C2, C4 закрыты
и проверены повторно, C3 закрыт по объёму. Открытые пункты — backlog, не блокируют мерж:

- **C3-остаток** (время/CPU рендера шаблона, admin-only) — до включения
  `NETOPS_SCHEDULER_ENABLED=true` в проде (ограничение рендера/таймаут).
- **C5** — до включения утренних отчётов в проде: индикация в админке, что сохранённый
  секрет не расшифровывается текущим ключом (после ротации `NETOPS_ENCRYPTION_KEY`).
- **C6, C8** — hardening-backlog (проверка схемы URL, унификация валидации host,
  длина/очистка эха невалидного получателя в тексте ошибки).
- **C7** — `devops`: принять/отклонить API-ключ Qdrant; зафиксировать требование
  «внутренние LLM/embeddings URL» в `.env.example`/`dev-checklist.md`.
- `qa`: кросс-файловая изоляция окружения в новых наборах (по отдельности наборы зелёные).
- При любых последующих правках в зонах секретов/интеграций — повторный
  `secret-scan -Mode quick` по изменённым зонам перед коммитом.
