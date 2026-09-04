FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# CHAINLIT_HOST обязателен: cli по умолчанию слушает 127.0.0.1
ENV CHAINLIT_HOST=0.0.0.0 CHAINLIT_PORT=8001
# Секреты (NETOPS_INTERNAL_SERVICE_TOKEN, NETOPS_PROXY_AUTH_SECRET,
# CHAINLIT_AUTH_SECRET) передаются через env docker-compose (Фаза 11)
EXPOSE 8001
CMD ["chainlit", "run", "app.py", "--headless"]
