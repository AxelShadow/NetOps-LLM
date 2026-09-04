"""Конфигурация chainlit-приложения (env-переменные).

Без зависимостей: импортируется и client-ом, и app-ом, и тестом парсера
(sse_parser_test.py chainlit не импортирует). Секретов в коде нет — только env.
"""
import os


class Config:
    """Читается один раз при импорте; падает сразу, если env неполон."""

    def __init__(self) -> None:
        # База FastAPI, например http://localhost:8000 (без /internal —
        # пути /internal/chat/stream, /api/auth/* клиент добавляет сам).
        self.fastapi_internal_url: str = self._req("FASTAPI_INTERNAL_URL").rstrip("/")
        # Сервисный токен: без него /internal/* FastAPI выключены.
        self.internal_service_token: str = self._req("NETOPS_INTERNAL_SERVICE_TOKEN")
        if not self.internal_service_token:
            raise RuntimeError(
                "NETOPS_INTERNAL_SERVICE_TOKEN обязателен: без него внутренний "
                "API FastAPI недоступен (/internal/* выключены)")
        # Секрет proxy-авторизации (nginx -> Chainlit, Фаза 7).
        # Пусто = заголовки пользователя от прокси игнорируются,
        # работает только логин-форма.
        self.proxy_auth_secret: str = os.getenv("NETOPS_PROXY_AUTH_SECRET", "")
        # dev | production. CHAINLIT_HOST/CHAINLIT_PORT читает сам chainlit-cli.
        self.environment: str = os.getenv("ENVIRONMENT", "development")

    @staticmethod
    def _req(name: str) -> str:
        v = os.getenv(name)
        if not v:
            raise RuntimeError(f"Обязательная переменная {name} не задана")
        return v


config = Config()
