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

    # OpenAI
    openai_api_key: str

    # Instagram (opcional)
    instagram_username: Optional[str] = None
    instagram_password: Optional[str] = None

    # Application Settings
    log_level: str = "INFO"
    max_retries: int = 3
    request_timeout: int = 30

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
