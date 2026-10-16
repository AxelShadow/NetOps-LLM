FROM python:3.12-slim
WORKDIR /app

# PYTHONDONTWRITEBYTECODE: не писать .pyc в контейнере; PYTHONUNBUFFERED: живые логи.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt .
# --no-cache-dir: pip-кэш не остаётся в слое; --no-compile: без .pyc при установке.
RUN pip install --no-cache-dir --no-compile -r requirements.txt
# Вместо COPY . . (затягивал .venv ~146 МБ, .files/, тесты, README) —
# только рантайм по графу импортов:
#   app.py -> adapters.py, auth.py, client.py -> config.py
#   chainlit.md — приветствие; .chainlit/ — config.toml + translations UI
COPY app.py client.py adapters.py config.py auth.py chainlit.md ./
COPY .chainlit ./.chainlit
# CHAINLIT_HOST обязателен: cli по умолчанию слушает 127.0.0.1
ENV CHAINLIT_HOST=0.0.0.0 CHAINLIT_PORT=8001
# Секреты (NETOPS_INTERNAL_SERVICE_TOKEN, NETOPS_PROXY_AUTH_SECRET,
# CHAINLIT_AUTH_SECRET) передаются через env docker-compose (Фаза 11)
EXPOSE 8001
CMD ["chainlit", "run", "app.py", "--headless"]
