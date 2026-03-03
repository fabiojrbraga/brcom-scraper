"""
Configuração centralizada da aplicação.
Carrega variáveis de ambiente e define configurações globais.
"""

from pydantic_settings import BaseSettings
from typing import Optional
import logging

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """Configurações da aplicação via variáveis de ambiente."""

    # FastAPI
    fastapi_env: str = "development"
    fastapi_host: str = "0.0.0.0"
    fastapi_port: int = 8000

    # PostgreSQL
    database_url: str

    # Browserless
    browserless_host: str
    browserless_token: str
    browserless_ws_url: Optional[str] = None
    browserless_session_enabled: bool = False
    browserless_session_ttl_ms: int = 300000
    browserless_session_stealth: bool = False
    browserless_session_headless: bool = True
    browserless_reconnect_timeout_ms: int = 60000
    browserless_max_concurrency: int = 2
    browserless_request_retries: int = 3
    browserless_retry_backoff_seconds: float = 1.0
    browser_use_max_retries: int = 3
    browser_use_retry_backoff: int = 2
    # "none" evita erros intermitentes de websocket/CDP em alguns proxies/browserless.
    browser_use_ws_compression: str = "none"  # auto | none | deflate
    browser_use_min_page_load_wait_s: float = 1.0
    browser_use_network_idle_wait_s: float = 8.0
    browser_use_wait_between_actions_s: float = 0.2

    # OpenAI
    openai_api_key: str
    openai_model_text: str = "gpt-4o-mini"
    openai_model_vision: str = "gpt-4o-mini"
    openai_fallback_model_text: Optional[str] = None
    openai_fallback_model_vision: Optional[str] = None
    openai_temperature_text: float = 1.0
    openai_temperature_vision: float = 1.0

    # Instagram (opcional)
    instagram_username: Optional[str] = None
    instagram_password: Optional[str] = None
    instagram_session_strict_validation: bool = False

    # Investing (opcional)
    investing_username: Optional[str] = None
    investing_password: Optional[str] = None
    investing_session_strict_validation: bool = False

    # Application Settings
    log_level: str = "INFO"
    max_retries: int = 3
    request_timeout: int = 30

    # API Authentication
    api_keys: Optional[str] = None  # comma-separated list
    api_key: Optional[str] = None   # backward-compatible single key
    api_auth_header_name: str = "X-API-Key"
    api_auth_public_paths: str = "/api/health,/docs,/openapi.json"
    profile_cache_ttl_days: int = 2

    class Config:
        env_file = ".env"
        case_sensitive = False

    def __init__(self, **data):
        super().__init__(**data)
        # Configurar logging
        logging.basicConfig(
            level=getattr(logging, self.log_level),
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        )


# Instância global de configurações
settings = Settings()

logger.info(f"Aplicação iniciada em modo: {settings.fastapi_env}")
logger.info(f"Banco de dados: {settings.database_url.split('@')[1] if '@' in settings.database_url else 'configurado'}")
