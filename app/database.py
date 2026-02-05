"""
Configuração de conexão com o banco de dados PostgreSQL.
"""

from sqlalchemy import create_engine, event, inspect, text
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
        _ensure_profiles_full_name_column()
        _ensure_interactions_post_url_column()
        logger.info("✅ Banco de dados inicializado com sucesso")
    except Exception as e:
        logger.error(f"❌ Erro ao inicializar banco de dados: {e}")
        raise


def _ensure_profiles_full_name_column() -> None:
    """
    Garante coluna full_name na tabela profiles para bases antigas.
    """
    try:
        inspector = inspect(engine)
        if "profiles" not in inspector.get_table_names():
            return

        column_names = {col["name"] for col in inspector.get_columns("profiles")}
        if "full_name" in column_names:
            return

        dialect = engine.dialect.name
        with engine.begin() as conn:
            if dialect == "postgresql":
                conn.execute(text("ALTER TABLE profiles ADD COLUMN IF NOT EXISTS full_name VARCHAR(255)"))
            else:
                conn.execute(text("ALTER TABLE profiles ADD COLUMN full_name VARCHAR(255)"))
        logger.info("✅ Coluna profiles.full_name criada com sucesso")
    except Exception as e:
        logger.warning("⚠️ Não foi possível garantir coluna profiles.full_name: %s", e)


def _ensure_interactions_post_url_column() -> None:
    """
    Garante coluna post_url e índice/unique em interactions para dedupe rápido.
    """
    try:
        inspector = inspect(engine)
        if "interactions" not in inspector.get_table_names():
            return

        column_names = {col["name"] for col in inspector.get_columns("interactions")}
        dialect = engine.dialect.name

        with engine.begin() as conn:
            if "post_url" not in column_names:
                if dialect == "postgresql":
                    conn.execute(text("ALTER TABLE interactions ADD COLUMN IF NOT EXISTS post_url VARCHAR(500)"))
                else:
                    conn.execute(text("ALTER TABLE interactions ADD COLUMN post_url VARCHAR(500)"))

            if "comment_posted_at" not in column_names:
                if dialect == "postgresql":
                    conn.execute(text("ALTER TABLE interactions ADD COLUMN IF NOT EXISTS comment_posted_at VARCHAR(64)"))
                else:
                    conn.execute(text("ALTER TABLE interactions ADD COLUMN comment_posted_at VARCHAR(64)"))

            # índice simples por post_url
            try:
                index_names = {idx["name"] for idx in inspector.get_indexes("interactions")}
            except Exception:
                index_names = set()
            if "ix_interactions_post_url" not in index_names:
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_interactions_post_url ON interactions (post_url)"))

            # unique para dedupe por post_url + user_url + interaction_type
            try:
                unique_names = {uc["name"] for uc in inspector.get_unique_constraints("interactions")}
            except Exception:
                unique_names = set()
            if "uq_interactions_post_url_user_url_type" not in unique_names:
                conn.execute(
                    text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS "
                        "uq_interactions_post_url_user_url_type "
                        "ON interactions (post_url, user_url, interaction_type)"
                    )
                )

        logger.info("✅ Coluna/índices interactions.post_url garantidos com sucesso")
    except Exception as e:
        logger.warning("⚠️ Não foi possível garantir interactions.post_url: %s", e)


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
            conn.execute(text("SELECT 1"))
        return True
    except Exception as e:
        logger.error(f"❌ Erro na verificação de saúde do banco: {e}")
        return False
