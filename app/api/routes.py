"""
Endpoints da API REST.
Define as rotas para scraping, consulta de dados, etc.
"""

import logging
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from datetime import datetime
import uuid

from app.database import get_db
from app.schemas import (
    ScrapingJobCreate,
    ScrapingJobResponse,
    ScrapingCompleteResponse,
    ProfileResponse,
    PostResponse,
    InteractionResponse,
    ErrorResponse,
)
from app.models import Profile, Post, Interaction, ScrapingJob
from app.scraper.instagram_scraper import instagram_scraper

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["instagram"])


# ==================== Health Check ====================

@router.get("/health")
async def health_check():
    """Verifica saúde da aplicação."""
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
    }


# ==================== Scraping Endpoints ====================

@router.post("/scrape", response_model=ScrapingJobResponse)
async def start_scraping(
    request: ScrapingJobCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    Inicia um job de scraping de um perfil Instagram.

    Args:
        request: URL do perfil a raspar
        background_tasks: Para executar scraping em background
        db: Sessão do banco de dados

    Returns:
        Informações do job criado
    """
    try:
        logger.info(f"📥 Requisição de scraping recebida: {request.profile_url}")

        # Criar job de scraping
        job = ScrapingJob(
            profile_url=request.profile_url,
            status="pending",
        )
        db.add(job)
        db.commit()
        db.refresh(job)

        # Executar scraping em background
        background_tasks.add_task(
            _scrape_profile_background,
            job_id=job.id,
            profile_url=request.profile_url,
        )

        logger.info(f"✅ Job de scraping criado: {job.id}")

        return ScrapingJobResponse(
            id=job.id,
            profile_url=job.profile_url,
            status=job.status,
            created_at=job.created_at,
        )

    except Exception as e:
        logger.error(f"❌ Erro ao criar job de scraping: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/scrape/{job_id}", response_model=ScrapingJobResponse)
async def get_scraping_status(
    job_id: str,
    db: Session = Depends(get_db),
):
    """
    Obtém status de um job de scraping.

    Args:
        job_id: ID do job
        db: Sessão do banco de dados

    Returns:
        Status do job
    """
    try:
        job = db.query(ScrapingJob).filter(ScrapingJob.id == job_id).first()

        if not job:
            raise HTTPException(status_code=404, detail="Job não encontrado")

        return ScrapingJobResponse(
            id=job.id,
            profile_url=job.profile_url,
            status=job.status,
            started_at=job.started_at,
            completed_at=job.completed_at,
            error_message=job.error_message,
            posts_scraped=job.posts_scraped,
            interactions_scraped=job.interactions_scraped,
            created_at=job.created_at,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Erro ao obter status do job: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/scrape/{job_id}/results", response_model=ScrapingCompleteResponse)
async def get_scraping_results(
    job_id: str,
    db: Session = Depends(get_db),
):
    """
    Obtém resultados completos de um job de scraping.

    Args:
        job_id: ID do job
        db: Sessão do banco de dados

    Returns:
        Resultados do scraping
    """
    try:
        job = db.query(ScrapingJob).filter(ScrapingJob.id == job_id).first()

        if not job:
            raise HTTPException(status_code=404, detail="Job não encontrado")

        if job.status != "completed":
            raise HTTPException(
                status_code=400,
                detail=f"Job ainda não foi concluído. Status: {job.status}",
            )

        # Buscar perfil associado
        profile = db.query(Profile).filter(
            Profile.instagram_url == job.profile_url
        ).first()

        if not profile:
            raise HTTPException(status_code=404, detail="Perfil não encontrado")

        # Buscar posts e interações
        posts = db.query(Post).filter(Post.profile_id == profile.id).all()
        interactions = db.query(Interaction).filter(
            Interaction.profile_id == profile.id
        ).all()

        # Montar resposta
        result = ScrapingCompleteResponse(
            job_id=job.id,
            status=job.status,
            profile={
                "username": profile.instagram_username,
                "profile_url": profile.instagram_url,
                "bio": profile.bio,
                "is_private": profile.is_private,
                "follower_count": profile.follower_count,
                "posts": [
                    {
                        "post_url": post.post_url,
                        "caption": post.caption,
                        "like_count": post.like_count,
                        "comment_count": post.comment_count,
                        "interactions": [
                            {
                                "type": interaction.interaction_type.value,
                                "user_url": interaction.user_url,
                                "user_username": interaction.user_username,
                                "user_bio": interaction.user_bio,
                                "is_private": interaction.user_is_private,
                                "comment_text": interaction.comment_text,
                            }
                            for interaction in interactions
                            if interaction.post_id == post.id
                        ],
                    }
                    for post in posts
                ],
            },
            total_posts=len(posts),
            total_interactions=len(interactions),
            error_message=job.error_message,
            completed_at=job.completed_at,
        )

        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Erro ao obter resultados do scraping: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== Profile Endpoints ====================

@router.get("/profiles/{username}", response_model=ProfileResponse)
async def get_profile(
    username: str,
    db: Session = Depends(get_db),
):
    """
    Obtém informações de um perfil.

    Args:
        username: Username do perfil
        db: Sessão do banco de dados

    Returns:
        Informações do perfil
    """
    try:
        profile = db.query(Profile).filter(
            Profile.instagram_username == username
        ).first()

        if not profile:
            raise HTTPException(status_code=404, detail="Perfil não encontrado")

        return ProfileResponse.from_orm(profile)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Erro ao obter perfil: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/profiles/{username}/posts")
async def get_profile_posts(
    username: str,
    skip: int = 0,
    limit: int = 10,
    db: Session = Depends(get_db),
):
    """
    Obtém posts de um perfil.

    Args:
        username: Username do perfil
        skip: Número de posts a pular
        limit: Número máximo de posts a retornar
        db: Sessão do banco de dados

    Returns:
        Lista de posts
    """
    try:
        profile = db.query(Profile).filter(
            Profile.instagram_username == username
        ).first()

        if not profile:
            raise HTTPException(status_code=404, detail="Perfil não encontrado")

        posts = db.query(Post).filter(
            Post.profile_id == profile.id
        ).offset(skip).limit(limit).all()

        return {
            "username": username,
            "total": len(posts),
            "posts": [PostResponse.from_orm(post) for post in posts],
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Erro ao obter posts do perfil: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/profiles/{username}/interactions")
async def get_profile_interactions(
    username: str,
    skip: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    """
    Obtém interações de um perfil.

    Args:
        username: Username do perfil
        skip: Número de interações a pular
        limit: Número máximo de interações a retornar
        db: Sessão do banco de dados

    Returns:
        Lista de interações
    """
    try:
        profile = db.query(Profile).filter(
            Profile.instagram_username == username
        ).first()

        if not profile:
            raise HTTPException(status_code=404, detail="Perfil não encontrado")

        interactions = db.query(Interaction).filter(
            Interaction.profile_id == profile.id
        ).offset(skip).limit(limit).all()

        return {
            "username": username,
            "total": len(interactions),
            "interactions": [
                InteractionResponse.from_orm(interaction)
                for interaction in interactions
            ],
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Erro ao obter interações do perfil: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== Background Tasks ====================

async def _scrape_profile_background(job_id: str, profile_url: str):
    """
    Executa scraping em background.

    Args:
        job_id: ID do job
        profile_url: URL do perfil a raspar
    """
    db = None
    try:
        db = next(get_db())

        # Atualizar status do job
        job = db.query(ScrapingJob).filter(ScrapingJob.id == job_id).first()
        if not job:
            logger.error(f"Job não encontrado: {job_id}")
            return

        job.status = "running"
        job.started_at = datetime.utcnow()
        db.commit()

        # Executar scraping
        result = await instagram_scraper.scrape_profile(
            profile_url=profile_url,
            max_posts=5,
            db=db,
        )

        # Atualizar job com resultados
        job.status = "completed"
        job.completed_at = datetime.utcnow()
        job.posts_scraped = result["summary"]["total_posts"]
        job.interactions_scraped = result["summary"]["total_interactions"]
        db.commit()

        logger.info(f"✅ Job concluído: {job_id}")

    except Exception as e:
        logger.error(f"❌ Erro no scraping em background: {e}")

        if db:
            job = db.query(ScrapingJob).filter(ScrapingJob.id == job_id).first()
            if job:
                job.status = "failed"
                job.error_message = str(e)
                job.completed_at = datetime.utcnow()
                db.commit()

    finally:
        if db:
            db.close()
