# Диагноз: сборка `netops-llm-app` падает по `no space left on device`; как уменьшить образы

> Статус: актуален · Обновлено: 2026-10-16 · Индексированный коммит: 8ab0257 · Владелец: Lead
> Проверки носят статический/измерительный характер: docker-демон в рабочей среде не запущен, доступа к прод-хосту нет.
> Значения секретов в документе не приводятся (только имена переменных и пути).

## 1. Что именно произошло

```
Image netops-llm-app Building  1302.5s
failed to solve: failed to extract layer sha256:9d194c6df9ade50cb8470381779461964600d446439c67e6bf0fcd8467e63211:
write /var/lib/containerd/io.containerd.snapshotter.v1.overlayfs/snapshots/475/fs/usr/local/lib/python3.11/site-packages/markdown_it/__pycache__/main.cpython-311.pyc:
no space left on device
```

| Фрагмент | Что означает |
|---|---|
| `failed to solve` | Ошибку вернула инфраструктура сборки BuildKit, а не `RUN` в Dockerfile. |
| `failed to extract layer` | Слой **уже скачан** (blob в content store), но не распаковался в файловую систему. Место кончилось при распаковке, а не при скачивании. |
| `/var/lib/containerd/io.containerd.snapshotter.v1.overlayfs/...` | Активен containerd image store: слои распаковываются в снапшоты containerd (не в `/var/lib/docker`). Заполнен раздел, где смонтирован `/var/lib/containerd`. |
| `no space left on device` | `ENOSPC` от ядра при `write(2)`: кончились **либо блоки, либо inode**. Текст ошибки одинаков, поэтому `df -i` обязателен. |

**Главный вывод: это не ошибка Dockerfile.** Диск прод-хоста переполнен в момент распаковки слоя. Уменьшение образов снижает пиковую потребность в месте и ускоряет сборку, но само по себе не заменяет освобождение места на хосте. Поэтому работа велась по двум направлениям: (1) диагностика и освобождение места на прод-хосте, (2) уменьшение контекста сборки и слоёв.

## 2. Основная причина: переполнение хранилища на хосте

Под сборку нужно в 2–3 раза больше места, чем «весит образ»: сжатый blob слоя + распакованный rootfs в снапшоте (обычно в 2–3 раза больше сжатого) + новые слои `pip install` + build cache BuildKit + copy-up overlayfs при первой записи в файл нижнего слоя.

Практический признак: ошибка «в середине распаковки» означает, что свободного места было примерно столько же, сколько требовалось, без запаса.

**Ключевые проверки (до любой чистки, порядок обязателен):**

```bash
df -h; df -i
df -h /var/lib/containerd; df -i /var/lib/containerd
findmnt -T /var/lib/containerd -o TARGET,SOURCE,FSTYPE,SIZE,USED,AVAIL,USE%
docker system df -v
sudo du -xsh /var/lib/containerd /var/lib/docker
docker info | grep -i -E "root dir|storage driver|containerd|snapshotter"
```

Полный ранбук с разбором гипотез, безопасным порядком освобождения места, запретом `docker system prune -a --volumes` (уничтожает том `pgdata` с данными PostgreSQL) и вариантами переноса data-root: [host-disk-runbook.md](host-disk-runbook.md).

## 3. Аномалия `python3.11` / `markdown_it`

Проверено фактически:

- `backend/dockerfile` и `chainlit/Dockerfile` — оба `FROM python:3.12-slim`.
- Строка `python:3.11` отсутствует во **всей** git-истории: `git log --all -S"python:3.11"` — пусто; `git log --all -S"markdown_it"` — пусто.
- Пакет `markdown_it` отсутствует в локальных `backend/.venv` и `chainlit/.venv` и не является прямой зависимостью `requirements.txt`.
- Официальный `python:3.12-slim` не может содержать `site-packages` с чужими пакетами и Python 3.11.

**Следствие:** слой `sha256:9d194c…` порождён не этой ревизией репозитория. Это влияет на интерпретацию: наша сборка, вероятнее всего, — «жертва» нехватки места, а не источник того слоя. Проверяемые на прод-хосте версии (команды — в разделе 2 ранбука):

1. На хосте параллельно собирался другой стек на Python 3.11 (проверить `journalctl -u docker`, `docker compose ls -a`, времена других сборок).
2. В `<каталоге-деплоя>` лежит иной/изменённый checkout (проверить `git rev-parse HEAD` → ожидается `8ab0257`, `git status --short`, `grep -rn 'FROM.*python:' .`).
3. Тег `python:3.12-slim` во внутреннем реестре перетегирован с 3.11 (проверить `PYTHON_VERSION` в `.Config.Env` и `docker history --no-trunc python:3.12-slim`).
4. Рядом лежит посторонний Dockerfile с `python:3.11`.
5. Слой принадлежит чужому pull, а строка `Image netops-llm-app Building` — только ближайший контекст вывода (прямое доказательство: перебор `docker image inspect <id> --format '{{join .RootFS.Layers " "}}'` и поиск дигest).

## 4. Что изменено в репозитории (уменьшение образов)

| Файл | Изменение | Зачем |
|---|---|---|
| `backend/.dockerignore` (новый) | Исключены `.venv`, `__pycache__`, `*.py[cod]`, `*.db`, `.env`/`.env.*`, `*.key`/`*.pem`, `tests/`, служебные скрипты | Контекст сборки перестал включать 97 МБ venv и локальную БД; секреты не попадают в контекст |
| `chainlit/.dockerignore` (новый) | То же + `dev_sse/`, `README.md`; `*.md` намеренно **не** исключается (иначе выпал бы нужный рантайму `chainlit.md`) | Контекст перестал включать 146 МБ venv |
| `backend/dockerfile` | `ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1`; `pip install --no-cache-dir --no-compile` | В образе нет `.pyc` — именно запись `.pyc` падала на проде; меньше файлов и объёма |
| `chainlit/Dockerfile` | `COPY . .` → явные `COPY app.py client.py adapters.py config.py auth.py chainlit.md ./` + `COPY .chainlit ./.chainlit`; те же ENV и `--no-compile` | Раньше в образ затягивалось всё содержимое каталога, включая `.venv` ~146 МБ, `.files/`, тесты, README. Цели `COPY`/`CMD`/`EXPOSE` сохранены |

Не менялись: `docker-compose*.yml`, `nginx/**`, `requirements.txt` и пины зависимостей, код приложения, секреты и схема их передачи (`env_file`/`environment` + fail-fast `${VAR:?}`).

### Измерения до/после (фактические, статический подсчёт с эмуляцией семантики `.dockerignore`)

| Контекст | До | После | Сокращение |
|---|---|---|---|
| `./backend` | 7884 файла / 103 694 496 Б (98.89 МБ) | 54 файла / 442 788 Б (0.42 МБ) | ≈ 98.5 МБ (99.6 %) |
| `./chainlit` | 7205 файлов / 153 314 131 Б (146.21 МБ) | 31 файл / 386 037 Б (0.37 МБ) | ≈ 145.8 МБ (99.7 %) |

Дополнительно: слой `COPY` в образе chainlit уменьшается примерно на 145.8 МБ; отказ от `.pyc` (верхняя оценка по локальным venv 3.12) — около 30 МБ (chainlit) и до 39 МБ (backend, вместе с dev-зависимостями).

Реальные размеры образов **не измерены**: в рабочей среде нет docker-демона. Проверить на хосте с демоном:

```bash
docker image ls --format 'table {{.Repository}}:{{.Tag}}\t{{.Size}}'
docker history --no-trunc netops-llm-chainlit | head -30
docker build --progress=plain -t probe ./backend   # при наличии места
```

## 5. План действий оператора (сжатая версия)

1. **Снять доказательства** (раздел 2 выше + раздел 1 ранбука): `df -h`, `df -i`, `docker system df -v`, `du` по `/var/lib/containerd`, `journalctl -u docker`. Ничего не удалять.
2. **Освободить место по возрастанию риска:** `docker builder prune --force --keep-storage 5GB` → `docker builder prune --force` → `docker container prune --force` → `docker image prune --force` → `docker image prune --all --force --filter until=168h` (после проверки доступности реестра) → при необходимости точечное `docker rmi <ID>`.
   **Запрещено:** `docker system prune -a --volumes`, `docker volume prune --all --force`, `docker compose down -v` — уничтожают том `pgdata` (данные PostgreSQL). Перед деструктивными шагами — выгруженный дамп БД (`pg_dump -Fc`) вне чистимого диска.
3. **Если места физически мало** — расширить LV или перенести data-root containerd/Docker на больший раздел (раздел 4 ранбука; с окном и откатом).
4. **Пересобрать дисциплинированно:** сначала `docker compose build app`, затем `docker compose build chainlit`, **без** `--no-cache`; при нехватке — по одному сервису. Образы стали меньше по контексту и слоям, но запас места 2–3× от размера образа всё равно нужен.
5. **Проверить результат:** `docker compose up -d`, `docker compose ps`, `docker compose logs chainlit`, `curl -fsS http://127.0.0.1:${WEB_PORT:-8080}/` (внутренние адреса наружу не выносить).
6. **Проверить аномалию** `python3.11` по разделу 3: если тег `python:3.12-slim` окажется перетегированным — остановить сборки и восстановить базовый образ.

## 6. Проверки и вердикты

| Контур | Артефакт | Итог |
|---|---|---|
| QA (`task-3`) | [verification.md](verification.md) | **ПРИНЯТЬ.** Контекст: backend 7884 файла / 98.89 МиБ → 54 / 0.42 МиБ (−99.57 %); chainlit 7205 / 146.21 МиБ → 31 / 0.37 МиБ (−99.75 %). Рантайм-файлы не потеряны (backend `app/**` 50/50, chainlit ровно COPY-набор 31 файл). `docker compose config --quiet` → 0 (с одноразовыми заглушками переменных). Регресс: `python run_tests.py` → exit 0, 25/25 OK после устранения двух дефектов **среды** (DACL на каталогах `tempfile.mkdtemp()`; отсутствие `cryptography` в локальном venv) — к правкам отношения не имеют. Дефектов правок нет |
| Security-guard (`task-4`) | [security-verdict.md](security-verdict.md) | **APPROVE WITH CONDITIONS.** В образ правками секреты не запекаются, прод-инварианты (`${VAR:?}`, отсутствие проброса портов, `internal;` в nginx) не ослаблены, отрицаний `!` в `.dockerignore` нет. Условия C1–C3 (инцидент, см. §7) |
| Reviewer (`task-5`, `task-7`) | [review.md](review.md) | task-1 — **APPROVE WITH COMMENTS** (блокирующих нет, замечания T1-1…T1-4). task-2 — сначала **REQUEST CHANGES** (T2-1 опасная `docker volume prune --all`, T2-2 перезапись `daemon.json`), после доработки T2-1…T2-9 — **APPROVE**, но найдена новая важная R-1 (безусловное копирование результата `jq` могло установить пустой `daemon.json`); R-1…R-5 закрыты, точечная проверка правки выполнена Lead по замороженному файлу (`install` только в ветке успеха после `test -s` + `grep` + `json.tool`, безусловного копирования нет, гейты 5/7a/7b машинные, fence-чётность 112). Итоговый вердикт по ранбуку — **APPROVE** |
| Architect (`task-6`) | `.dsh/kb/30-deploy-and-env.md`, `.dsh/kb/90-decisions.md`, `.dsh/kb/_generated/` | Карта обновлена на коммит `8ab0257`, добавлена запись ADR `ДД-001`, индексы `_generated/` перегенерированы, процессное правило про запуск импорт-проб вне корня репозитория внесено |

## 7. Риски, ограничения и сопутствующие находки

- **`backend/dockerfile` в нижнем регистре против дефолтного `Dockerfile`** (подтверждено независимо QA (D-2) и reviewer'ом (T1-4); риск формулируется по [Compose Build Specification](https://docs.docker.com/reference/compose-file/build/#dockerfile): при строковой форме `build:` «the default is `Dockerfile`», а «lack of a Dockerfile within this folder throws an error»): `docker-compose.yml:13` (`build: ./backend`) не задаёт ключ `dockerfile:`, а в git файл называется `backend/dockerfile`; тот же риск в `docker-compose.dev.yml:17,41` (у `chainlit` файл назван правильно). **При этом ваша сборка началась** — значит либо имя было найдено, либо состояние чекаута на проде отличается от репозитория. Это второй признак (вместе с аномалией `python3.11`) в пользу **расхождения прод-чекаута с репозиторием**; проверяется одним действием: `git rev-parse HEAD`, `git status --short`, `ls -l <каталог-деплоя>/backend/`. Устранение (вне текущего диффа): добавить `dockerfile: dockerfile` в оба compose-файла или переименовать файл в `Dockerfile` — решение после проверки состояния прода.
- **Реальная сборка не проверялась** (демон не запущен), фактический размер образов не измерен — только контекст и `COPY`-payload.
- **`--no-compile` + `PYTHONDONTWRITEBYTECODE=1`**: импорт при старте чуть медленнее (нет bytecode-кэша). Откат — убрать `--no-compile`. QA и reviewer подтвердили экспериментом, что `uvicorn`/Chainlit при этом работают.
- **Предсуществующий FAIL сканера секретов:** `secret-scan.ps1 -Mode quick` возвращает код 1 и 23 находки уровня high (`docker-compose.dev.yml`, `backend/seed_devices.py`, тесты, `chainlit/README.md`, `.tools/site/cryptography/**`); `-Mode full -IncludeNetwork` добавляет 46 medium (`network-internal` в фикстурах). Значения не раскрываются. В четырёх изменённых файлах находок нет (адресный скан — PASS). Нужна отдельная задача на разбор и обезвреживание.
- **Предсуществующий дефект dev-стека:** сервис `seed` в `docker-compose.dev.yml` строит образ из `./backend` и выполняет `python seed_dev.py`, но этот файл в образ никогда не копировался (`COPY app ./app` был и в первом коммите) → `docker compose -f docker-compose.dev.yml run --rm seed` падает. Правки не ухудшили ситуацию; исправление вне границ задачи (нужно решение: отдельный dev-Dockerfile или монтирование скрипта, а не добавление dev-скрипта в прод-образ).
- **Гигиеническое замечание QA (D-1, не блокирует):** в контексте `./backend` остаются `seed_dev.py`, `seed_devices.py`, `requirements-dev.txt` (~10 КБ); в образ они не копируются, но комментарий `backend/.dockerignore:4` обещает «только `requirements.txt` и `app/`». Устраняется отдельной правкой автора (после неё потребуется повторная проверка контекста).
- **Локальный `docker compose config` падает с кодом 15** на `${NETOPS_ENCRYPTION_KEY:?}` — это штатный fail-fast (значение пустое), а не дефект. Проверка структуры выполнялась с одноразовыми dummy-значениями переменных процесса: `--services` → `db app chainlit web`, `--images` → 4 образа, `--quiet` → 0.
- **Ограничения среды прогона тестов** (не связаны с правками): каталоги, созданные `tempfile.mkdtemp()`/`os.mkdir(..., 0o700)`, получают DACL без права записи (20 наборов падали на sqlite), и в локальном `backend/.venv` нет `cryptography`. После сессионных обходов (шим `mkdtemp` + локальный вендоренный `cryptography`) `python run_tests.py` → **exit 0, 25/25 OK**.
- **Инцидент процесса (S3, значение не раскрывается):** при импорт-пробе с рабочим каталогом = корень репозитория `pydantic-settings` прочитал корневой `.env` (`backend/app/config.py:7`, относительный `env_file=".env"` + `extra='forbid'`) и в тексте `ValidationError` вывел значение переменной `DB_PASSWORD`, `<redacted, N chars>`; оно попало в лог инструмента агента (транскрипт сессии). В отчёты и в файлы репозитория значение не переносилось (подтверждено поиском по 4 файлам, `docs/docker-image-size/**` и временным артефактам; побочный каталог `E:\netops-llm\.chainlit\`, созданный пробой, удалён). Меры: **ротировать `DB_PASSWORD`**, если этот `.env` используется на реальном стенде (и обновить деплой, `docker-compose.yml:6`); транскрипт не выгружать и считать значение скомпрометированным; импорт-пробы `app.*` выполнять только с cwd `backend/` или в изолированном каталоге без `.env`. Процессное правило внесено в `.dsh/kb/30-deploy-and-env.md`; код-харденинг (`extra="ignore"` в `backend/app/config.py:7`) — рекомендованный follow-up для `backend-dev`.
- **Рекомендованные follow-up:** `*.env`/`.envrc` в оба `.dockerignore`; pin базовых образов по digest; hash-lock зависимостей; негативный тест «`Settings` не печатает значения посторонних ключей»; разбор находок сканера (23 high + 46 medium).
