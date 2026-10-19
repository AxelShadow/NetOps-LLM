from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # extra="ignore": посторонние ключи в .env не должны попадать в текст
    # ValidationError, который pydantic печатает с ЗНАЧЕНИЕМ переменной
    # (инцидент 2026-10-16: утечка DB_PASSWORD в лог). Неизвестные NETOPS_*
    # молча игнорируются, как и ожидается для переменных других сервисов.
    model_config = SettingsConfigDict(
        env_prefix="NETOPS_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg2://netops:netops@db:5432/netops"

    jwt_secret: str
    jwt_hours: int = 12

    ad_server: str = "ldap://dc02.id.samges.ru"
    ad_domain: str = "id.samges.ru"

    llm_base_url: str
    llm_api_key: str = "lm-studio"
    llm_default_model: str = ""      # пусто = первая доступная модель
    llm_timeout: float = 300

    bootstrap_admin: str = ""
    history_messages: int = 20
    # Параметры агента (Этап 20): env-дефолты, перекрываются runtime-
    # настройками из БД (app_settings, /admin/settings)
    agent_context_chars: int = 50000   # бюджет истории, символов
    agent_max_steps: int = 20          # лимит шагов агентского цикла

    zabbix_url: str = ""
    zabbix_token: str = ""

    ad_search_base: str = "DC=id,DC=samges,DC=ru"
    auto_register_users: bool = True  # Авто-регистрация при первом входе

    dev_mode: bool = False

    mock_mode: bool = False
    # Сервисный токен для /internal/* (пусто = внутренние маршруты выключены)
    internal_service_token: str = Field(default="",
                                        env="NETOPS_INTERNAL_SERVICE_TOKEN")
    # Ключ шифрования чувствительных данных в БД (Fernet, crypto.py).
    # Пусто и dev_mode=false -> crypto.KeyConfigurationError при обращении
    # к шифруемому полю (fail-fast вместо молчаливой смены ключа).
    encryption_key: str = ""
    # Секрет proxy-авторизации для auth-check (пусто = header-режим
    # Chainlit выключен, работает только логин-форма)
    proxy_auth_secret: str = ""
    # Фаза 14: true — новый UI (лендинг / -> чат/админка), false — старый SPA
    use_new_ui: bool = False

    # --- RAG по runbook'ам: Qdrant + OpenAI-совместимые эмбеддинги ----------
    # Каталог документов (.md/.txt). В контейнере сюда монтируется том с
    # runbook'ами; относительный путь считается от cwd (backend/).
    rag_enabled: bool = False
    rag_docs_dir: str = "runbooks"
    rag_qdrant_url: str = "http://qdrant:6333"
    rag_qdrant_api_key: str = ""
    rag_qdrant_collection: str = "netops_runbooks"
    rag_top_k: int = 5
    rag_chunk_chars: int = 1200
    rag_chunk_overlap: int = 150
    rag_max_file_bytes: int = 1024 * 1024
    # Эмбеддинги: пустые значения наследуют LLM-настройки (тот же
    # OpenAI-совместимый сервер, обычно LM Studio / vLLM).
    embedding_base_url: str = ""
    embedding_model: str = ""
    embedding_api_key: str = ""
    embedding_timeout: float = 60
    embedding_dim: int = 1024

    # --- Планировщик утренних отчётов: APScheduler + SMTP (Zimbra) ---------
    scheduler_enabled: bool = False
    scheduler_tz: str = "Europe/Moscow"
    scheduler_time_default: str = "08:00"


@lru_cache
def get_settings() -> Settings:
    return Settings()
