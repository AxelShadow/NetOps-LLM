# Независимая проверка QA: slim-сборка образов (task-1)

**Проверяющий:** `qa-image-verify` (роль `qa`), не автор изменения.
**Объект:** замороженный артефакт `task-1` (rev 3, completed).
**Репозиторий:** `E:\netops-llm`, ветка `main`, `git rev-parse --short HEAD` = `8ab0257`.
**Окружение:** Docker client 24.0.6, `docker compose` v2.23.0-desktop.1, **docker-демон не запущен** (сборка образов не выполнялась).

## Вердикт QA: ПРИНЯТЬ

Критерии приёмки выполнены фактически. Правки сокращают контекст сборки backend на 99.57 %, chainlit на 99.75 %; ни один рантайм-обязательный файл не отфильтрован; синтаксис Dockerfile/compose корректен (`docker compose config --quiet` → 0); регресс-набор зелёный (25/25 OK, exit 0) после устранения двух дефектов **среды**, не связанных с правками. Дефектов правок нет; есть одно гигиеническое замечание уровня LOW и один предсуществующий риск вне диффа (см. §8).

Артефакт проверялся по хэшу (SHA-256):

| Файл | SHA-256 | Статус |
|---|---|---|
| `backend/dockerfile` | `C12B306CE74289F9261BFD177118FD50D890DA03359963355EF649839CCE924D` | изменён автором |
| `chainlit/Dockerfile` | `91B3E9AC950BA738E57561820A557DF77416595FE1CBFA73209F70859D6EF738` | изменён автором |
| `backend/.dockerignore` | `F18AC40009C59EAE9D33784A38460F98F528681A3281503622A6F044400853DE` | создан автором |
| `chainlit/.dockerignore` | `D3A8F22AE1B2942CFD823C6D8B11CC9791B7693E7A5FA5A8577D7FA9E5B81CA9` | создан автором |

Проверяющий эти четыре файла не менял (хэши совпадают до и после всех прогонов).

---

## 1. Полнота фильтрации контекста: измерения «до» / «после»

**Инструмент:** `docs/docker-image-size/context-measure.ps1` (зона записи QA).
**Запуск:**

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File docs\docker-image-size\context-measure.ps1
```

Код возврата `0`. Фактический вывод:

```
CONTEXT: backend
rules         : 52 (0 negations)
BEFORE (all)  : 7884 files, 98,89 MiB (103694496 B)
AFTER (kept)  : 54 files, 0,42 MiB (442788 B)
EXCLUDED      : 7830 files, 98,47 MiB (103251708 B, 99,57% of bytes)
    .venv               7763 files  97,24 MiB
    *.db                   1 files   0,53 MiB
    **/__pycache__        34 files   0,44 MiB
    tests                 17 files   0,21 MiB
    *_test.py              4 files   0,03 MiB
CONTEXT: chainlit
rules         : 43 (0 negations)
BEFORE (all)  : 7205 files, 146,21 MiB (153314131 B)
AFTER (kept)  : 31 files, 0,37 MiB (386037 B)
EXCLUDED      : 7174 files, 145,84 MiB (152928094 B, 99,75% of bytes)
    .venv               7157 files 145,76 MiB
    __pycache__            6 files   0,04 MiB
    *_test.py              2 files   0,02 MiB
    dev_sse                5 files   0,01 MiB
    README.md              1 files   0,01 MiB
```

| Контекст | До (файлов) | До (байт) | После (файлов) | После (байт) | Сокращение |
|---|---|---|---|---|---|
| `./backend` | 7884 | 103 694 496 (98.89 МиБ / 103.69 МБ) | 54 | 442 788 (0.42 МиБ / 0.44 МБ) | −99.57 % байт, −7830 файлов |
| `./chainlit` | 7205 | 153 314 131 (146.21 МиБ / 153.31 МБ) | 31 | 386 037 (0.37 МиБ / 0.39 МБ) | −99.75 % байт, −7174 файла |

Цифры совпадают с независимо полученными в `docs/docker-image-size/review.md` (103.7 МБ / 7884 и 153.3 МБ / 7205).

**Семантика, которую воспроизводит скрипт** (сверено с исходником `moby/patternmatcher`, `patternmatcher.go`):

* `.dockerignore` читается как в BuildKit (`dockerignore.ReadAll`): снимается UTF-8 BOM, строки, начинающиеся с `#`, пропускаются, строка обрезается по краям, пустые пропускаются, путь нормализуется (`filepath.Clean`), `\` → `/`;
* шаблон сопоставляется с путём от корня контекста, разделитель — `/`; если прямое совпадение не сработало, проверяются все префиксы-предки (`MatchesOrParentMatches`) — так исключённый каталог исключает и всё содержимое;
* `*` не переходит через `/`, `?` — один символ кроме `/`, `[abc]` — класс, `**/x` совпадает и с `x` в корне, `dir/**` — всё под `dir/`;
* правила применяются по порядку, решает **последнее** совпадение; `!` — возврат в контекст; состояние по умолчанию — «включён»;
* регистрозависимо (как на Linux-билдере), независимо от регистронезависимой ФС Windows.

**Чем реализация может расходиться с Docker (важно для трактовки цифр):**

1. **Единица измерения.** Считаются байты содержимого файлов. Реальный контекст — tar-поток: на каждую запись 512-байтовый заголовок, выравнивание размера до 512 Б, записи каталогов. Итог занижен примерно на 100–300 КБ; для «после» относительная погрешность заметна (54 файла ≈ 0.44 МБ → реальный tar немного больше), для «до» — пренебрежима.
2. **Пустые каталоги.** `COPY app ./app` переносит и пустые каталоги; скрипт их не считает (файлов нет). В `backend/app` пусты `db`, `rag`, `scheduler`, `tools` — на результат не влияет.
3. **`filepath.Clean`.** Не разворачиваются сегменты `..` и ведущий `/`; в проверяемых `.dockerignore` таких шаблонов нет.
4. **Экранирование `\!`.** BuildKit трактует шаблон, начинающийся с `\!`, как литеральный `!`; скрипт этого не делает. В проверяемых файлах таких строк нет.
5. **Симлинки.** Скрипт пропускает reparse points и не идёт по ним; Docker кладёт симлинк как есть (байты цели не попадают). В обоих контекстах reparse points не найдено (проверено обходом) — расхождения нет.
6. **`!`-правила.** Упрощённая обработка «родитель исключён, потомок возвращён» может отличаться от BuildKit; в обоих файлах **0 отрицаний**, поэтому расхождения нет.
7. **Один каталог не прочитан** (см. §8, D-3): `backend/.pytest_cache` — 5 файлов недоступны sandbox-оболочке, в счёт «до» не вошли. Правилами оба `.dockerignore` этот каталог исключают, поэтому «после» точен.
8. Абсолютные объёмы включают локальные `backend/.venv` (97.24 МиБ), `chainlit/.venv` (145.76 МиБ) и сам файл `.dockerignore`; на чистом клоне «до» будет меньше. Переносимо относительное сокращение, а не абсолютные МиБ.

---

## 2. Достаточность фильтрации: рантайм-обязательные файлы

### 2.1 backend — `COPY requirements.txt .` (строка 9) и `COPY app ./app` (строка 14)

Фактическая команда: `context-measure.ps1 -ContextName backend -ListKept app` → **50 из 50** не-`pyc` файлов `app/` в контексте после фильтрации; `-ListKept requirements.txt` → `requirements.txt 506 B` (в контексте).

| Что нужно рантайму | Фильтр | Чем подтверждено |
|---|---|---|
| `requirements.txt` | проходит | `-ListKept requirements.txt` → 1 файл, 506 B; Dockerfile строка 9 |
| `app/main.py`, `app/config.py`, `app/crypto.py`, `app/db.py`, `app/models.py` | проходят | `-ListKept app` → все 5 в списке; `app/main.py:11` `from .db import ...`, `:14` `from .models import ...` |
| `app/agent/**`, `app/api/**`, `app/auth/**`, `app/devices/**`, `app/llm/**`, `app/ui/**` (соответственно 3, 3, 4, 6, 1, 4 `.py`) | проходят | все перечислены в `-ListKept app`; граф импортов от `app/main.py`: `.api.devices`, `.ui.home`, `.ui.router`, `.db`, `.models`, `.auth.routes`, `.api.chat`, `.api.internal`, `.auth.ldap_auth`, `.config` |
| `app/templates/**` — 20 `.html` (Jinja) | проходят | `-ListKept app` → 20/20; `app/ui/router.py:34` `_TPL_DIR = .../app/templates`; правила `*.md`/`docs/`/`tests/` их не задевают |
| `app/static/**` — `css/tailwind.css`, `css/tailwind.src.css`, `img/spinner.svg`, `js/htmx.min.js` | проходят | 4/4 в `-ListKept app`; `app/main.py:188` `_ADMIN_STATIC = Path(__file__).parent / "static"`, `:189` `app.mount("/admin/static", ...)` |
| `app/**/__pycache__/*.pyc` — 26 файлов | **отфильтрованы** (`**/__pycache__`, `**/*.py[cod]`) | не нужны: интерпретатор компилирует заново; `PYTHONDONTWRITEBYTECODE=1` (строка 6). Побочный плюс: `COPY app ./app` больше не тащит `.pyc` в слой |

Итог по backend: **ни один рантайм-файл не потерян**; единственные потери внутри `app/` — `__pycache__`. Пустые каталоги `app/{db,rag,scheduler,tools}` импортами не используются (проверено grep: `from .db` ссылается на модуль-файл `app/db.py`, ссылок на `app.rag`/`app.scheduler`/`app.tools` нет).

### 2.2 chainlit — COPY-инструкции против набора запуска `chainlit run app.py`

Фактическая команда: `context-measure.ps1 -ContextName chainlit -ListKept ALL` → **31 файл**, ровно набор из COPY плюс `requirements.txt`.

| Нужно рантайму | В Dockerfile | Фильтр | Чем подтверждено |
|---|---|---|---|
| `app.py` (3930 B) | строка 15 | проходит | `-ListKept ALL` |
| `client.py` (12 956 B) | строка 15 | проходит | `-ListKept ALL`; `app.py:11` `from client import BackendError, stream_chat` |
| `adapters.py` (2295 B) | строка 15 | проходит | `-ListKept ALL`; `app.py:9` `import adapters` |
| `config.py` (1971 B) | строка 15 | проходит | `-ListKept ALL`; `client.py:21`, `auth.py:18` — `from config import config` |
| `auth.py` (3433 B) | строка 15 | проходит | `-ListKept ALL`; `app.py:10` `import auth` |
| `chainlit.md` (722 B) | строка 15 | **проходит** (в `.dockerignore` намеренно нет `*.md` — комментарий строк 49–50) | `-ListKept ALL` |
| `.chainlit/` — `config.toml` (402 B) + 23 `translations/*.json` | строка 16 | проходят | `-ListKept ALL` → все 24 файла; `config.toml` — секции `[project]`, `[features]`, `[UI]`, `[meta]` |
| `requirements.txt` | строка 8 | проходит | `-ListKept ALL` |
| Отфильтровано и не нужно: `.venv` (7157), `__pycache__` (6), `sse_parser_test.py` + `adapters_test.py` (2), `dev_sse/` (5), `README.md` (1), `chainlit/.env.example` (1), `Dockerfile` (1), `.dockerignore` (1) | — | — | ни один из них не участвует в `chainlit run app.py`; `dev_sse` — только dev-SSE-сценарии |

Отдельно проверено, что **`COPY . .` в chainlit устранён**: статический поиск `^\s*COPY\s+\.\s+\.\s*$` → 0 совпадений (в `git show HEAD:chainlit/Dockerfile` — `COPY . .` был; в `backend/dockerfile` и раньше были только `COPY requirements.txt .` + `COPY app ./app`). Каталог `public/` в дереве отсутствует, поэтому его и не должно быть в COPY (прежний `COPY . .` его тоже не давал).

---

## 3. Синтаксис: Dockerfile и compose

### 3.1 Статическая проверка инструкций

| Проверка | `backend/dockerfile` (16 строк) | `chainlit/Dockerfile` (22 строки) |
|---|---|---|
| `FROM` | 1 (`python:3.12-slim`) | 1 (`python:3.12-slim`) |
| `WORKDIR` | 1 (`/app`) | 1 (`/app`) |
| `ENV` | 1 (`PYTHONDONTWRITEBYTECODE=1`, `PYTHONUNBUFFERED=1`) | 2 (`...=1`, `CHAINLIT_HOST=0.0.0.0`, `CHAINLIT_PORT=8001`) |
| `COPY` | 2 | 3 |
| `RUN` | 1 | 1 |
| `CMD` | 1 (`uvicorn app.main:app --host 0.0.0.0 --port 8000`) | 1 (`chainlit run app.py --headless`) |
| `EXPOSE` | 0 (не требуется) | 1 (`8001` — согласовано с healthcheck compose `:8001` и `CHAINLIT_PORT`) |
| `pip install --no-cache-dir` | есть (`--no-cache-dir --no-compile`) | есть (`--no-cache-dir --no-compile`) |
| голая `COPY . .` | 0 | **0** (устранена) |
| Источники COPY существуют | `requirements.txt` — да, `app` — да | `requirements.txt`, `app.py`, `client.py`, `adapters.py`, `config.py`, `auth.py`, `chainlit.md`, `.chainlit` — все да |
| Ссылки на несуществующие пути | нет | нет |

Команда (фактическая): разбор обеих строк `COPY` с `Test-Path` по каждому источнику — все `exists=True`.

### 3.2 compose

`docker compose config` **без фильтра не запускался** (печатает значения переменных). Четыре переменные, которые compose требует через `:?`, были заданы одноразовыми заглушками в окружении процесса; файлы при этом не создавались и не изменялись:

```powershell
docker compose config --quiet      # → exit 0
docker compose config --services   # → db, app, chainlit, web   (exit 0)
docker compose config --images     # → postgres:16-alpine, netops-llm-app, netops-llm-chainlit, nginx:alpine (exit 0)
```

* Без этих заглушек `docker compose config` возвращает 15 на `${NETOPS_ENCRYPTION_KEY:?}` (`docker-compose.yml:16-18`) — это штатный fail-fast, а не дефект правок; совпадает с `00-diagnosis.md` и `review.md`.
* Контексты сборки `./backend` (`docker-compose.yml:13`) и `./chainlit` (`:25`) совпадают с местами размещения новых `.dockerignore`; `docker-compose.dev.yml:17,41,53` использует те же контексты.
* Bind-mount источники существуют: `nginx/default.conf.template` ✓, каталог `frontend/` ✓; `env_file` (`docker-compose.yml:14`) — корневой файл окружения присутствует (проверено инструментом `glob`; содержимое не читалось).
* Демон не запущен (ожидаемо): `docker version` → `error during connect ... the system cannot find the file specified`; `docker compose version` → `v2.23.0-desktop.1`.

---

## 4. Регресс-тесты: `python run_tests.py`

Прогонов три — чтобы честно отделить «сломано правками» от «сломано средой». Рабочая БД `backend/netops.db`, SHA-256 `3B71BBDD650EDB7BE62417D1AC21BA1156AC3AF8DCFCE573440BCEEC7B6A9893` — **не изменилась ни в одном прогоне** (изоляция через `NETOPS_DATABASE_URL` подтверждена).

| # | Команда | Код | OK / FAIL / MISSING | Причина FAIL |
|---|---|---|---|---|
| A | `python run_tests.py` (окружение без обходов) | **1** | 4 / 21 / 0 (1m39s) | 20 × `sqlite3.OperationalError: unable to open database file`; 1 × `ModuleNotFoundError: No module named 'cryptography'` |
| B | тот же раннер + сессионный шим `tempfile.mkdtemp` в `PYTHONPATH` | **1** | 15 / 10 / 0 (1m18s) | 10 × `ModuleNotFoundError: No module named 'cryptography'` |
| C | тот же раннер + шим + локально вендоренный `cryptography 46.0.7` в `PYTHONPATH` | **0** | **25 / 0 / 0** (2m01s) | — |

Прогон C завершился строкой `ИТОГОВАЯ СВОДКА / всего: 25 / OK: 25 / FAIL: 0 / MISSING: 0`, `EXIT_CODE=0`; зелёными стали в том числе `tests/test_crypto.py`, `tests/test_e2e_flow.py`, `tests/test_ui_switch.py`, `tests/test_snmp_migration.py`, `tests/test_agent_settings.py`, `tests/test_printers_report.py`, `sse_parser_test.py`, `adapters_test.py`, `dev_sse/scenarios_test.py`.

### Доказательство, что FAIL'ы A и B не вызваны правками

1. **Правки не читает ни один тест и ни один модуль приложения.** `grep` по шаблону `(dockerignore|Dockerfile|dockerfile)` с фильтром `*.py` по всему репозиторию → **0 совпадений**. Изменённые файлы — только `backend/dockerfile`, `chainlit/Dockerfile`, два `.dockerignore`; ни один из них не импортируется и не открывается.
2. **Причина A воспроизводится без кода проекта вообще:**

```powershell
& 'E:\netops-llm\backend\.venv\Scripts\python.exe' -c "import tempfile,sqlite3,os; d=tempfile.mkdtemp(prefix='netops_probe_'); sqlite3.connect(os.path.join(d,'x.db')).close()"
# → sqlite3.OperationalError: unable to open database file
```

Изоляция причины:

| Способ создать каталог | просто записать файл | открыть sqlite |
|---|---|---|
| `tempfile.mkdtemp(...)` / `os.mkdir(path, 0o700)` | `PermissionError` | `sqlite3.OperationalError` |
| `os.mkdir(path)` / `os.makedirs(path)` | OK | OK |
| расположение: `$env:TEMP`, `E:\netops-llm`, подкаталог | результат зависит от mode, не от места | — |

`icacls` на каталоге, созданном `mkdtemp`, отвечает `Access is denied` — то есть каталог получает DACL, запрещающий запись sandbox-процессу. **Все 20 упавших в прогоне A наборов используют `tempfile.mkdtemp(...)` для временной sqlite** (проверено grep по `mkdtemp`/`NETOPS_DATABASE_URL`), а зелёный `smoke_test.py` использует `tempfile.mktemp()` — файл прямо в `$env:TEMP`. Это дефект прав доступа среды DSH на Windows, а не кода и не правок.
3. **Причина B — отсутствующая зависимость локального venv:** `backend/.venv/Scripts/python.exe -c "import importlib.util as u; print(u.find_spec('cryptography') is not None)"` → `False`, при том что `backend/requirements.txt:18` требует `cryptography>=42.0,<52.0`, а в дереве лежит локально вендоренный `cryptography 46.0.7` в `.tools/site` (не на `sys.path` венва). Прогон C с `E:\netops-llm\.tools\site` в `PYTHONPATH` это подтверждает: падают ровно те наборы, что импортируют `cryptography` (через `app/models.py:111-129` → `app/crypto.py:29`). К правкам отношения не имеет; в образе зависимость ставится из `requirements.txt`.
4. Побочно: `pytest`-предупреждения `PytestCacheWarning ... [WinError 5] ..., '.pytest_cache\v\cache'` — некритичны, тот же дефект прав доступа (см. D-3).

**Как это интерпретировать:** «сырой» прогон в этой сессии даёт код 1 по причинам среды; после устранения двух дефектов среды (без изменения файлов репозитория и без ослабления тестов) набор полностью зелёный — регресса правки не дают. Шим и добавление `PYTHONPATH` — сессионные обходы прав доступа и локального окружения; файлы проекта при этом не менялись (`git status` показывает только ожидаемые `M backend/dockerfile`, `M chainlit/Dockerfile`, `?? backend/.dockerignore`, `?? chainlit/.dockerignore`, `?? docs/docker-image-size/`).

---

## 5. secret-scan

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .dsh\tools\secret-scan.ps1 -Mode quick
# → exit 1: найдено 23, high 23, medium 0, low 0; ВЕРДИКТ: FAIL
```

Все 23 находки — в файлах, которых правки не касались: `.tools/site/cryptography/**` (9), `docker-compose.dev.yml` (4), `backend/seed_devices.py` (3), `backend/internal_api_test.py`, `backend/internal_ext_test.py`, `backend/tests/test_agent_limit.py`, `test_agent_settings.py`, `test_e2e_flow.py` (по 1), `chainlit/README.md` (2). **Ни одной находки в четырёх файлах `task-1`.** Это известный предсуществующий фон (зафиксирован в `00-diagnosis.md` и `security-verdict.md`); значения не выводились — сканер маскирует их сам.

Адресная проверка изменённых файлов (копии четырёх файлов в каталоге вне репозитория + параметр `-Path`): `ВЕРДИКТ: PASS`, просканировано 4 файла, находок 0, **exit 0**.

Файлы из перечня политики сканером не читались: 4 файла пропущены, выведены только их имена и имена переменных, без значений.

**Вердикт по артефакту `task-1`: чисто.** Вердикт инструмента по репозиторию в целом — FAIL (предсуществующий, отдельная задача на разбор).

---

## 6. Что не проверено локально (и чем это проверит оператор на проде)

| Не проверено | Почему | Команда оператору (на хосте с заполненным файлом окружения) |
|---|---|---|
| Реальная сборка обоих образов | docker-демон не запущен (24.0.6 client, сервер недоступен); Docker Desktop не стартовал | `DOCKER_BUILDKIT=1 docker compose build app` → `DOCKER_BUILDKIT=1 docker compose build chainlit` (ожидаем exit 0) |
| Реальный размер контекста, принятый билдером | то же | `DOCKER_BUILDKIT=1 docker build --progress=plain ./chainlit 2>&1 \| grep -i "transferring context"` → ожидаемо порядка 0.4 МБ (до правок — десятки/сотни МБ) |
| Размер образов и состав слоёв | то же | `docker image ls --format '{{.Repository}}\t{{.Tag}}\t{{.Size}}' \| grep netops`; `docker history --no-trunc netops-llm-chainlit \| head -40` — в chainlit **не должно** быть слоя размером ~146 МБ от `COPY . .` |
| Какие файлы реально попали в образ | то же | `docker compose run --rm --no-deps app ls -la /app /app/app \| head -30`; `docker compose run --rm --no-deps chainlit ls -la /app /app/.chainlit` |
| Рантайм-поведение (шаблоны/статика/приветствие/health) | нет запущенных контейнеров | `docker compose up -d`; затем `curl -fsS http://<адрес-стенда>:8001/chat/health` и `curl -fsS -o /dev/null -w '%{http_code}\n' http://<адрес-стенда>:8080/admin/static/js/htmx.min.js`; визуально проверить приветствие `chainlit.md` в UI |
| Реальная семантика `.dockerignore` на Linux-билдере | нет билдера | сравнить предупреждения `docker build` и число файлов в контексте с §1; при расхождении смотреть `--progress=plain` |
| Полный `run_tests.py` без сессионных обходов | дефект прав доступа среды DSH (см. §4) | на рабочей машине/CI с нормальными правами на `%TEMP%` и установленным `cryptography`: `python run_tests.py` → ожидаемо код 0, 25/25 OK |

Предупреждение к первой команде: `backend/dockerfile` отслеживается git **в нижнем регистре**, а `docker compose build app` (`docker-compose.yml:13`, ключ `dockerfile:` не задан) ищет по умолчанию `Dockerfile`. На регистронезависимой ФС (Windows/macOS) это работает, на Linux может дать `Cannot locate specified Dockerfile: Dockerfile`. См. D-2.

---

## 7. Дефекты правок

**Дефектов, блокирующих приёмку, не найдено.** Ни один рантайм-обязательный файл не отфильтрован, ни одна COPY-инструкция не ссылается на отсутствующий путь, синтаксис корректен, регресса нет.

## 8. Замечания и риски (не блокируют приёмку)

| ID | Уровень | Где | Суть | Влияние |
|---|---|---|---|---|
| **D-1** | LOW (гигиена, зона автора) | `backend/.dockerignore:48-57` (блок «тесты и служебные скрипты») | В контекст сборки по-прежнему попадают `seed_devices.py` (в нём 3 находки `secret-scan` уровня high — заглушки dev-стенда), `seed_dev.py`, `requirements-dev.txt`. Они не копируются в образ (COPY-набор — только `requirements.txt` + `app/`), но уезжают билдеру вместе с контекстом, тогда как комментарий строки 4 обещает «только `requirements.txt` и `app/`» | Объём: ~10 КБ из 442 КБ «после». Утечки в образ нет. Рекомендация: добавить `seed_*.py`, `requirements-dev.txt` отдельной задачей автора |
| **D-2** | Предсуществующий риск, **вне диффа task-1** | `docker-compose.yml:13` (`build: ./backend`, ключа `dockerfile:` нет) и отслеживаемый `backend/dockerfile` в нижнем регистре; то же в `docker-compose.dev.yml:17,41` | Compose по умолчанию ищет `Dockerfile`. На Linux-хосте `docker compose build app` может упасть с `Cannot locate specified Dockerfile: Dockerfile` | Подтверждается фактом: `git show HEAD:backend/dockerfile` → файл в нижнем регистре уже в HEAD; `grep 'dockerfile:'` по `*.yml` → 0 совпадений; у `chainlit/Dockerfile` регистр правильный. Если целевой хост Linux — до реальной сборки либо добавить `dockerfile: dockerfile` в compose, либо переименовать файл (решение — за Lead/автором; правка не входит в замороженный артефакт) |
| **D-3** | Ограничение среды | `backend/.pytest_cache` (5 файлов) | Sandbox-оболочка не может ни перечислить, ни прочитать каталог: `Get-ChildItem` → `UnauthorizedAccessException`, `icacls` → `Access is denied`, `pytest` → `PytestCacheWarning ... [WinError 5]` | На измерения не влияет: каталог исключается обоими `.dockerignore` (`.pytest_cache`, `**/.pytest_cache`); в счёт «до» не вошли ≤5 мелких файлов. Тесты — только некритичные warning'и. Ремонт прав требует непривилегированного запуска с подтверждением, недоступного в этой сессии (approval отключён) |

## 9. Что осталось

1. Оператору — прогнать §6 на целевом хосте и подтвердить реальные размеры контекста/образов и рантайм.
2. Решить по D-1 (добавить `seed_*.py`/`requirements-dev.txt` в `backend/.dockerignore`) и по D-2 (регистр имени Dockerfile для Linux-сборки).
3. Предсуществующий FAIL `secret-scan` по репозиторию (23 high вне диффа) — отдельная задача обезвреживания; правила сканера не ослаблялись.
