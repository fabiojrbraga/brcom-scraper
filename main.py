"""
Aplicação FastAPI principal.
Ponto de entrada da aplicação Instagram Scraper.
"""

import logging
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager

from config import settings
from app.database import init_db, health_check
from app.api.routes import router
from app.api.auth import require_private_api_key
from app.scraper.instagram_scraper import instagram_scraper

logger = logging.getLogger(__name__)


# ==================== Lifespan ====================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Gerencia o ciclo de vida da aplicação.
    Executa código de inicialização e limpeza.
    """
    # Startup
    logger.info("🚀 Iniciando aplicação Instagram Scraper...")
    
    try:
        init_db()
        logger.info("✅ Banco de dados inicializado")
    except Exception as e:
        logger.error(f"❌ Erro ao inicializar banco de dados: {e}")
        raise

    if not health_check():
        logger.warning("⚠️ Banco de dados não está acessível")

    yield

    # Shutdown
    logger.info("🛑 Encerrando aplicação...")
    await instagram_scraper.close()
    logger.info("✅ Aplicação encerrada")


# ==================== FastAPI App ====================

app = FastAPI(
    title="Instagram Scraper API",
    description="API para raspagem de dados do Instagram usando IA e Browser Automation",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


# ==================== CORS ====================

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ==================== Routes ====================

app.include_router(
    router,
    dependencies=[Depends(require_private_api_key)],
)


# ==================== Protected Docs ====================

@app.get("/openapi.json", include_in_schema=False, dependencies=[Depends(require_private_api_key)])
async def openapi_json():
    return JSONResponse(
        get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )
    )


@app.get("/docs", include_in_schema=False, dependencies=[Depends(require_private_api_key)])
async def swagger_ui():
    return get_swagger_ui_html(
        openapi_url="/openapi.json",
        title=f"{app.title} - Docs",
    )


# ==================== Root ====================

@app.get("/")
async def root():
    """Endpoint raiz com informações da API."""
    return {
        "name": "Instagram Scraper API",
        "version": "1.0.0",
        "environment": settings.fastapi_env,
        "docs": "/docs",
        "health": "/api/health",
    }


# ==================== Error Handlers ====================

@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    """Handler global para exceções não tratadas."""
    logger.error(f"❌ Erro não tratado: {exc}")
    return {
        "detail": "Erro interno do servidor",
        "status_code": 500,
    }


# ==================== Main ====================

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=settings.fastapi_host,
        port=settings.fastapi_port,
        log_level=settings.log_level.lower(),
    )
