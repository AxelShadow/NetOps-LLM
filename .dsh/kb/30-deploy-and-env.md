> Статус: каркас · Обновлено: 2026-10-16 · Индексированный коммит: 5d169ba

# 30 — Деплой и окружения

## Назначение

Раздел описывает, как система собирается и запускается: сервисы docker-compose
(прод и dev), порты и тома, маршрутизацию nginx, полный список переменных окружения
(только имена и назначение) и порядок локального запуска и тестов.

## Ключевые файлы

| Путь | Роль |
|---|---|
| `docker-compose.yml` | прод-стек: `db` (postgres:16-alpine) + `app` + `chainlit` + `web` (nginx:alpine) |
| `docker-compose.dev.yml` | dev-стек: `app` (sqlite) + `seed` (фикстуры) + `chainlit` + `web` |
| `backend/dockerfile` | образ backend: python:3.12-slim, копирует только `app/`, CMD uvicorn :8000 |
| `chainlit/Dockerfile` | образ Chainlit: python:3.12-slim, `chainlit run app.py --headless`, порт 8001 |
| `nginx/default.conf.template` | шаблон конфига nginx; entrypoint образа прогоняет `envsubst` |
| `.env.example` | образец `.env` для прода (только имена переменных) |
| `chainlit/.env.example` | образец env для локального запуска Chainlit |
| `backend/requirements.txt` / `requirements-dev.txt` | прод- и dev-зависимости backend |
| `chainlit/requirements.txt` | зависимости Chainlit-приложения |
| `run_tests.py` | консолидированный прогон всех наборов тестов |
| `docs/dev-checklist.md` | чек-лист dev-стенда и ручных проверок |

## Структура и потоки

**Прод (`docker-compose.yml`):**

| Сервис | Образ / сборка | Порты наружу | Тома | Зависит от |
|---|---|---|---|---|
| `db` | `postgres:16-alpine` | нет | `pgdata:/var/lib/postgresql/data` | — |
| `app` | build `./backend`, `env_file: .env` | нет | нет | `db` |
| `chainlit` | build `./chainlit` | нет | нет | `app` |
| `web` | `nginx:alpine` | `${WEB_PORT:-8080}:80` | `./nginx/default.conf.template` (ro), `./frontend` → `/usr/share/nginx/html` (ro) | `app`, `chainlit` |

- `chainlit` получает `FASTAPI_INTERNAL_URL=http://app:8000`, `CHAINLIT_ROOT_PATH=/chat`,
  `NETOPS_INTERNAL_SERVICE_TOKEN`, `NETOPS_PROXY_AUTH_SECRET`, `ENVIRONMENT`;
  healthcheck бьёт `http://127.0.0.1:8001/chat/health` через `urllib` (curl в slim нет).
- Обязательные секреты compose требует через синтаксис `${VAR:?…}` — контейнеры
  падают fail-fast: `DB_PASSWORD`, `CHAINLIT_AUTH_SECRET`,
  `NETOPS_INTERNAL_SERVICE_TOKEN`, `NETOPS_ENCRYPTION_KEY` (у сервиса `app`;
  без ключа `crypto.py` отказался бы расшифровывать пароли устройств).
- У `app` нет проброса портов и healthcheck: снаружи доступен только nginx.

**Dev (`docker-compose.dev.yml`), для роут-трипа без реальных систем:**

| Сервис | Особенности |
|---|---|
| `app` | `NETOPS_DATABASE_URL=sqlite:////data/netops.db`, `NETOPS_DEV_MODE=true`, `NETOPS_MOCK_MODE=true`, `NETOPS_USE_NEW_UI=true`, том `devdata:/data` |
| `seed` | разовый `python seed_dev.py` (идемпотентно), `restart: "no"` |
| `chainlit` | те же переменные с dev-значениями, `ENVIRONMENT=development` |
| `web` | порт `8080:80`, те же тома, dev-значения токенов |

Сброс dev-данных: `docker compose -f docker-compose.dev.yml down -v`.

**Маршрутизация nginx (`nginx/default.conf.template`, шаблон → `envsubst`):**

| Location | Назначение |
|---|---|
| `= /` | proxy на `app:8000` — backend сам решает: лендинг или 303 на `/legacy/` |
| `/legacy/` | `try_files $uri /legacy/index.html` — старый SPA из тома `./frontend` |
| `/` | proxy на `app:8000` (без fallback на SPA: неизвестный путь → честный 404) |
| `/api/` | proxy на `app:8000`, `proxy_buffering off`, `proxy_read_timeout 600s` (SSE) |
| `= /admin` | 302 на `/admin/`; `/admin/` — proxy на `app:8000`, timeout 300s |
| `/internal/` | `internal;` — снаружи недостижимо (404) |
| `= /_auth` | `internal;` → `app:8000/internal/auth-check`, прокидывает сервисный токен |
| `/chat` | `auth_request /_auth` → `chainlit:8001`, WebSocket-upgrade, `X-Proxy-Auth-Secret` и `X-User-*` из ответа auth-check; 401 → 302 на `/admin/login` |

- `absolute_redirect off` — редиректы относительные (иначе при `WEB_PORT≠80` браузер
  уходит на :80).
- Подстановка только `${NETOPS_INTERNAL_SERVICE_TOKEN}` и `${NETOPS_PROXY_AUTH_SECRET}`;
  nginx-переменные (`$host`, `$http_upgrade`) envsubst не трогает.
- Пустой `NETOPS_INTERNAL_SERVICE_TOKEN` выключает `/internal/*` на backend →
  auth-check отвечает 401 → `/chat` уводит на форму логина админки.

**Переменные окружения (только имена и назначение; значения — в `.env`, вне git):**

| Переменная | Где читается | Назначение |
|---|---|---|
| `NETOPS_DATABASE_URL` | `config.py` | строка подключения SQLAlchemy (Postgres/`sqlite`) |
| `NETOPS_JWT_SECRET` | `config.py` | подпись JWT (HS256); обязательна |
| `NETOPS_JWT_HOURS` | `config.py` | время жизни токена и cookie (дефолт 12 ч) |
| `NETOPS_AD_SERVER`, `NETOPS_AD_DOMAIN`, `NETOPS_AD_SEARCH_BASE` | `ldap_auth.py` | параметры LDAP/AD |
| `NETOPS_AUTO_REGISTER_USERS` | `auth/routes.py` | авто-регистрация неизвестных как `viewer` |
| `NETOPS_BOOTSTRAP_ADMIN` | `main.py`, `auth/routes.py` | UPN первого администратора |
| `NETOPS_LLM_BASE_URL`, `NETOPS_LLM_API_KEY`, `NETOPS_LLM_DEFAULT_MODEL`, `NETOPS_LLM_TIMEOUT` | `llm/client.py` | OpenAI-совместимый провайдер; URL обязателен |
| `NETOPS_HISTORY_MESSAGES` | `api/chat.py` | сколько сообщений истории идёт в контекст |
| `NETOPS_AGENT_CONTEXT_CHARS`, `NETOPS_AGENT_MAX_STEPS` | `api/chat.py` | env-дефолты параметров агента (перекрываются `app_settings`) |
| `NETOPS_ZABBIX_URL`, `NETOPS_ZABBIX_TOKEN` | `devices/zabbix.py` | Zabbix JSON-RPC |
| `NETOPS_DEV_MODE` | `ldap_auth.py`, шаблоны | вход мимо AD (только dev) |
| `NETOPS_MOCK_MODE` | `agent/tools.py`, `llm/client.py` | мок-данные вместо сетевых вызовов |
| `NETOPS_INTERNAL_SERVICE_TOKEN` | `api/internal.py`, nginx | сервисный токен `/internal/*` (server-to-server) |
| `NETOPS_PROXY_AUTH_SECRET` | `chainlit/auth.py`, nginx | доверие заголовкам `X-User-*` от nginx |
| `NETOPS_USE_NEW_UI` | `ui/home.py` | `false` — старый SPA основной, `true` — лендинг |
| `NETOPS_ENCRYPTION_KEY` | `crypto.py`, compose `app` | Fernet-ключ шифрования паролей/community; **обязателен вне dev** (`NETOPS_DEV_MODE=false`), иначе `KeyConfigurationError` |
| `DB_PASSWORD` | `docker-compose.yml` | пароль Postgres для сервиса `db` |
| `WEB_PORT` | `docker-compose.yml` | внешний порт nginx (дефолт 8080) |
| `CHAINLIT_AUTH_SECRET` | compose | секрет cookie Chainlit (генерация: `chainlit create-secret`) |
| `CHAINLIT_HOST`, `CHAINLIT_PORT`, `CHAINLIT_ROOT_PATH` | compose / `.env` Chainlit | адрес и подпуть Chainlit (`/chat`) |
| `FASTAPI_INTERNAL_URL` | `chainlit/config.py` | база FastAPI для внутренних вызовов |
| `ENVIRONMENT` | `chainlit/config.py` | `development` / `production` |

- `backend/app/config.py` читает `env_file=".env"`: локальный `backend/.env`
  **перекрывает** корневой `.env` при запуске из каталога `backend/`. Файлы `.env` и
  `backend/.env` в git не попадают (`.gitignore`) и по правилам безопасности не читаются.
- `NETOPS_ENCRYPTION_KEY` (`.env.example`, добавлен `5d169ba`; поведение
  исправлено отдельным коммитом): вне dev ключ **обязателен** — при пустом
  значении `crypto.py` бросает `KeyConfigurationError` вместо генерации нового
  ключа, который сделал бы ранее записанные пароли нечитаемыми. В dev-режиме
  временный ключ генерируется один раз на процесс, значение ключа в лог не
  пишется (только факт и длина). Ротация ключа требует перешифрования значений
  в БД — процедура не описана (см. «Открытые вопросы»).

## Команды и проверки

```bash
# --- прод ---
cp .env.example .env          # заполнить секреты (в git не коммитить)
docker compose config         # проверка подстановки переменных
docker compose up --build -d

# --- dev-стенд (nginx :8080 → /chat → app) ---
docker compose -f docker-compose.dev.yml up --build -d
docker compose -f docker-compose.dev.yml run --rm seed
docker compose -f docker-compose.dev.yml down -v        # сброс данных

# --- локально, без docker (только backend) ---
cd backend
NETOPS_MOCK_MODE=true NETOPS_DEV_MODE=true \
  NETOPS_DATABASE_URL=sqlite:///./dev-tmp.db \
  NETOPS_BOOTSTRAP_ADMIN=admin@example.com NETOPS_JWT_SECRET=dev \
  uvicorn app.main:app --port 8000
# --- создание venv'ов (нужны для run_tests.py) ---
cd backend && python -m venv .venv && .venv\Scripts\pip install -r requirements.txt -r requirements-dev.txt
cd chainlit && python -m venv .venv && .venv\Scripts\pip install -r requirements.txt

# --- тесты ---
python run_tests.py            # из корня; exit 0 — OK, 1 — FAIL, 2 — нет venv
```
## Открытые вопросы

- **не проверено:** `docker compose config` и сборка образов в этом окружении не
  запускались (Docker не вызывался); корректность шаблона nginx после `envsubst`
  подтверждается только чтением файла.
- **не проверено:** значения в `.env` (корень) и `backend/.env` не читались, а какие
  значения `NETOPS_USE_NEW_UI` / `WEB_PORT` / адреса LLM заданы на сервере — неизвестно;
  полнота списка переменных сверена с `.env.example`, `chainlit/.env.example`,
  `backend/app/config.py`, `docker-compose*.yml` и `nginx/default.conf.template`.
- **требует уточнения:** healthcheck есть только у сервиса `chainlit`; у `app` и `db`
  проверок нет, поэтому nginx может отдавать 502 при живом compose-статусе.
- Заполнить по ходу работы: файл Dockerfile backend называется `backend/dockerfile`
  (нижний регистр) — при явном `dockerfile:` в compose имя указывать точно; также не
  описаны процедура обновления прод-стенда и ротация `NETOPS_ENCRYPTION_KEY` /
  `NETOPS_JWT_SECRET` / `NETOPS_ZABBIX_TOKEN`.
