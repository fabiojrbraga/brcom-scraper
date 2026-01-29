"""
Configuração de conexão com o banco de dados PostgreSQL.
"""

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import NullPool
from config import settings
import logging

logger = logging.getLogger(__name__)

# Normalizar URL do Postgres para SQLAlchemy
database_url = settings.database_url
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

# Criar engine do SQLAlchemy
engine = create_engine(
    database_url,
    echo=settings.fastapi_env == "development",
    poolclass=NullPool if settings.fastapi_env == "production" else None,
    connect_args={"connect_timeout": settings.request_timeout}
)

# Criar session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db() -> Session:
    """
    Dependência para obter sessão de banco de dados.
    Uso: db: Session = Depends(get_db)
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """
    Inicializa o banco de dados criando todas as tabelas.
    Deve ser chamado uma vez na inicialização da aplicação.
    """
    from app.models import Base
    
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("✅ Banco de dados inicializado com sucesso")
    except Exception as e:
        logger.error(f"❌ Erro ao inicializar banco de dados: {e}")
        raise


def drop_db():
    """
    Remove todas as tabelas do banco de dados.
    ⚠️ Use apenas em desenvolvimento!
    """
    from app.models import Base
    
    if settings.fastapi_env != "development":
        raise RuntimeError("❌ Não é permitido dropar banco em produção!")
    
    try:
        Base.metadata.drop_all(bind=engine)
        logger.warning("⚠️ Banco de dados foi limpo")
    except Exception as e:
        logger.error(f"❌ Erro ao limpar banco de dados: {e}")
        raise


def health_check() -> bool:
    """
    Verifica a conexão com o banco de dados.
    """
    try:
        with engine.connect() as conn:
            conn.execute("SELECT 1")
        return True
    except Exception as e:
        logger.error(f"❌ Erro na verificação de saúde do banco: {e}")
        return False
