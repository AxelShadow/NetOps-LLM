from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NETOPS_", env_file=".env")

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

    zabbix_url: str = ""
    zabbix_token: str = ""

    dev_mode: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
