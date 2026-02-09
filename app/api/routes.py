"""
Endpoints da API REST.
Define as rotas para scraping, consulta de dados, etc.
"""

import logging
import asyncio
from typing import Any
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import func
from sqlalchemy.orm.attributes import flag_modified
from datetime import datetime
from urllib.parse import urlparse

from app.database import get_db
from app.schemas import (
    ScrapingJobCreate,
    ScrapingJobResponse,
    ScrapingCompleteResponse,
    ProfileScrapeRequest,
    ProfileScrapeResponse,
    GenericScrapeRequest,
    GenericScrapeResponse,
    GenericScrapeJobResultResponse,
    InvestingScrapeRequest,
    InvestingScrapeJobResultResponse,
    ProfileResponse,
    PostResponse,
    InteractionResponse,
    ErrorResponse,
)
from app.models import Profile, Post, Interaction, ScrapingJob, InstagramSession
from app.scraper.instagram_scraper import instagram_scraper
from app.scraper.browser_use_agent import browser_use_agent
from config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["instagram"])


def _extract_instagram_username(profile_url: str) -> str:
    """Extrai username de URL completa ou valor simples."""
    value = (profile_url or "").strip()
    if not value:
        return ""
    if not value.startswith(("http://", "https://")):
        return value.strip("/").split("/")[0].strip()

    parsed = urlparse(value)
    if "instagram.com" not in parsed.netloc.lower():
        return ""
    path_parts = [part for part in parsed.path.split("/") if part]
    return path_parts[0].strip() if path_parts else ""


def _normalize_profile_url(profile_url: str) -> str:
    """Normaliza URL para formato canônico do Instagram."""
    username = _extract_instagram_username(profile_url)
    if username:
        return f"https://www.instagram.com/{username}/"
    return (profile_url or "").strip()


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_session_username(username: str | None) -> str | None:
    normalized = (username or "").strip().lstrip("@").lower()
    return normalized or None


def _get_active_instagram_session(
    db: Session,
    session_username: str | None = None,
) -> InstagramSession | None:
    query = db.query(InstagramSession).filter(InstagramSession.is_active.is_(True))
    normalized_username = _normalize_session_username(session_username)
    if normalized_username:
        query = query.filter(func.lower(InstagramSession.instagram_username) == normalized_username)
    return query.order_by(InstagramSession.updated_at.desc()).first()


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
        normalized_profile_url = _normalize_profile_url(request.profile_url)
        logger.info(
            "📥 Requisição de scraping recebida: %s (normalizado: %s)",
            request.profile_url,
            normalized_profile_url,
        )

        # Criar job de scraping
        request_payload = request.model_dump(mode="json", exclude_unset=True)
        request_payload["profile_url"] = normalized_profile_url
        job = ScrapingJob(
            profile_url=normalized_profile_url,
            status="pending",
            metadata_json={"request": request_payload},
        )
        db.add(job)
        db.commit()
        db.refresh(job)

        # Executar scraping em background
        background_tasks.add_task(
            _scrape_profile_background,
            job_id=job.id,
            profile_url=normalized_profile_url,
            options={k: v for k, v in request_payload.items() if k != "profile_url"},
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
            return JSONResponse(
                status_code=200,
                content={
                    "detail": f"Job ainda n?o foi conclu?do. Status: {job.status}",
                },
            )

        metadata = job.metadata_json or {}
        request_payload = metadata.get("request", {}) if isinstance(metadata.get("request"), dict) else {}
        flow = metadata.get("flow") or request_payload.get("flow")
        flow_result = metadata.get("result")

        if flow == "recent_likes" and isinstance(flow_result, dict):
            posts = flow_result.get("posts", []) or []
            summary = flow_result.get("summary", {}) or {}
            profile_payload = flow_result.get("profile", {}) or {}
            return ScrapingCompleteResponse(
                job_id=job.id,
                status=job.status,
                flow="recent_likes",
                profile={
                    "username": profile_payload.get("username") or "",
                    "full_name": profile_payload.get("full_name"),
                    "profile_url": profile_payload.get("profile_url") or job.profile_url,
                    "bio": profile_payload.get("bio"),
                    "is_private": bool(profile_payload.get("is_private", False)),
                    "follower_count": profile_payload.get("follower_count"),
                    "posts": [
                        {
                            "post_url": post.get("post_url", ""),
                            "caption": post.get("caption"),
                            "like_count": _safe_int(post.get("like_count", 0) or 0),
                            "comment_count": _safe_int(post.get("comment_count", 0) or 0),
                            "interactions": [],
                        }
                        for post in posts
                        if post.get("post_url")
                    ],
                },
                extracted_posts=posts,
                total_posts=_safe_int(summary.get("total_posts", len(posts)) or 0),
                total_interactions=_safe_int(summary.get("total_like_users", 0) or 0),
                raw_result=flow_result,
                error_message=job.error_message,
                completed_at=job.completed_at,
            )

        # Buscar perfil associado
        profile = db.query(Profile).filter(
            Profile.instagram_url == job.profile_url
        ).first()

        if not profile:
            normalized_job_url = _normalize_profile_url(job.profile_url)
            if normalized_job_url and normalized_job_url != job.profile_url:
                profile = db.query(Profile).filter(
                    Profile.instagram_url == normalized_job_url
                ).first()

        if not profile:
            username = _extract_instagram_username(job.profile_url)
            if username:
                profile = db.query(Profile).filter(
                    Profile.instagram_username == username
                ).first()

        if not profile and isinstance(flow_result, dict):
            posts = flow_result.get("posts", []) or []
            interactions = flow_result.get("interactions", []) or []
            summary = flow_result.get("summary", {}) or {}
            profile_payload = flow_result.get("profile", {}) or {}
            return ScrapingCompleteResponse(
                job_id=job.id,
                status=job.status,
                flow=flow or "default",
                profile={
                    "username": profile_payload.get("username") or _extract_instagram_username(job.profile_url),
                    "full_name": profile_payload.get("full_name"),
                    "profile_url": profile_payload.get("profile_url") or _normalize_profile_url(job.profile_url),
                    "bio": profile_payload.get("bio"),
                    "is_private": bool(profile_payload.get("is_private", False)),
                    "follower_count": profile_payload.get("follower_count"),
                    "posts": [
                        {
                            "post_url": post.get("post_url", ""),
                            "caption": post.get("caption"),
                            "like_count": _safe_int(post.get("like_count", 0) or 0),
                            "comment_count": _safe_int(post.get("comment_count", 0) or 0),
                            "interactions": [],
                        }
                        for post in posts
                        if post.get("post_url")
                    ],
                },
                total_posts=_safe_int(summary.get("total_posts", len(posts)) or 0),
                total_interactions=_safe_int(summary.get("total_interactions", len(interactions)) or 0),
                raw_result=flow_result,
                error_message=job.error_message,
                completed_at=job.completed_at,
            )

        if not profile:
            logger.warning(
                "Perfil não encontrado para job %s; retornando resultado baseado no metadata/job sem consulta de perfil.",
                job.id,
            )
            return ScrapingCompleteResponse(
                job_id=job.id,
                status=job.status,
                flow=flow or "default",
                profile={
                    "username": _extract_instagram_username(job.profile_url),
                    "full_name": None,
                    "profile_url": _normalize_profile_url(job.profile_url),
                    "bio": None,
                    "is_private": False,
                    "follower_count": None,
                    "posts": [],
                },
                total_posts=_safe_int(job.posts_scraped, 0),
                total_interactions=_safe_int(job.interactions_scraped, 0),
                raw_result=flow_result if isinstance(flow_result, dict) else None,
                error_message=job.error_message,
                completed_at=job.completed_at,
            )

        # Buscar posts e interações
        posts = db.query(Post).filter(Post.profile_id == profile.id).all()
        interactions = db.query(Interaction).filter(
            Interaction.profile_id == profile.id
        ).all()

        # Montar resposta
        result = ScrapingCompleteResponse(
            job_id=job.id,
            status=job.status,
            flow="default",
            profile={
                "username": profile.instagram_username,
                "full_name": profile.full_name,
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

@router.post("/profiles/scrape", response_model=ProfileScrapeResponse)
async def scrape_profile_info(
    request: ProfileScrapeRequest,
    db: Session = Depends(get_db),
):
    """
    Raspa um perfil específico e retorna somente dados do perfil.
    """
    try:
        normalized_profile_url = _normalize_profile_url(request.profile_url)
        logger.info(
            "📥 Requisição de scrape de perfil recebida: %s (normalizado: %s)",
            request.profile_url,
            normalized_profile_url,
        )
        result = await instagram_scraper.scrape_profile_info(
            profile_url=normalized_profile_url,
            db=db,
            save_to_db=True,
            cache_ttl_days=settings.profile_cache_ttl_days,
            session_username=request.session_username,
        )
        return ProfileScrapeResponse(**result)
    except Exception as e:
        logger.error("❌ Erro ao raspar perfil %s: %s", request.profile_url, e)
        detail = str(e).strip() or repr(e) or "Erro interno ao raspar perfil."
        raise HTTPException(status_code=500, detail=detail)

@router.get("/profiles/scrape")
async def scrape_profile_info_get_not_allowed():
    """
    Evita conflito com rota dinamica /profiles/{username}.
    """
    raise HTTPException(
        status_code=405,
        detail="Use POST /api/profiles/scrape com body JSON (profile_url).",
    )


@router.post("/generic_scrape", response_model=ScrapingJobResponse)
async def generic_scrape(
    request: GenericScrapeRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    Scraping generico de qualquer site via Browser Use em Browserless.
    """
    try:
        target_url = (request.url or "").strip()
        instruction_prompt = (request.prompt or "").strip()
        session_username = _normalize_session_username(request.session_username)
        if not target_url:
            raise HTTPException(status_code=400, detail="Campo 'url' e obrigatorio.")
        if not instruction_prompt:
            raise HTTPException(status_code=400, detail="Campo 'prompt' e obrigatorio.")

        logger.info("🌐 Requisicao generic_scrape recebida: %s", target_url)

        request_payload = request.model_dump(mode="json", exclude_unset=True)
        request_payload["url"] = target_url
        if session_username:
            request_payload["session_username"] = session_username

        job = ScrapingJob(
            profile_url=target_url,
            status="pending",
            metadata_json={
                "flow": "generic",
                "request": request_payload,
            },
        )
        db.add(job)
        db.commit()
        db.refresh(job)

        background_tasks.add_task(
            _generic_scrape_background,
            job.id,
            target_url,
            instruction_prompt,
            bool(request.test_mode),
            int(request.test_duration_seconds),
            session_username,
        )

        return ScrapingJobResponse(
            id=job.id,
            profile_url=job.profile_url,
            status=job.status,
            created_at=job.created_at,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("❌ Erro no generic_scrape para %s: %s", request.url, e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/generic_scrape/{job_id}", response_model=ScrapingJobResponse)
async def get_generic_scrape_status(job_id: str, db: Session = Depends(get_db)):
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
        logger.error("❌ Erro ao obter status do generic_scrape: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/generic_scrape/{job_id}/results", response_model=GenericScrapeJobResultResponse)
async def get_generic_scrape_results(job_id: str, db: Session = Depends(get_db)):
    try:
        job = db.query(ScrapingJob).filter(ScrapingJob.id == job_id).first()
        if not job:
            raise HTTPException(status_code=404, detail="Job não encontrado")

        if job.status not in ("completed", "failed"):
            return JSONResponse(
                status_code=200,
                content={"detail": f"Job ainda não foi concluído. Status: {job.status}"},
            )

        metadata = job.metadata_json if isinstance(job.metadata_json, dict) else {}
        request_payload = metadata.get("request") if isinstance(metadata.get("request"), dict) else {}
        result_payload = metadata.get("result") if isinstance(metadata.get("result"), dict) else {}

        return GenericScrapeJobResultResponse(
            job_id=job.id,
            status=job.status,
            url=str(request_payload.get("url") or job.profile_url),
            prompt=str(request_payload.get("prompt") or ""),
            data=result_payload.get("data"),
            raw_result=result_payload.get("raw_result"),
            error_message=job.error_message or result_payload.get("error"),
            completed_at=job.completed_at,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("❌ Erro ao obter resultado do generic_scrape: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/investing_scrape", response_model=ScrapingJobResponse)
async def investing_scrape(
    request: InvestingScrapeRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    Job assincrono de scraping no Investing com sessao autenticada reutilizavel.
    """
    try:
        target_url = (request.url or "").strip()
        instruction_prompt = (request.prompt or "").strip()
        if not target_url:
            raise HTTPException(status_code=400, detail="Campo 'url' e obrigatorio.")
        if not instruction_prompt:
            raise HTTPException(status_code=400, detail="Campo 'prompt' e obrigatorio.")

        request_payload = request.model_dump(mode="json", exclude_unset=True)
        request_payload["url"] = target_url

        job = ScrapingJob(
            profile_url=target_url,
            status="pending",
            metadata_json={
                "flow": "investing",
                "request": request_payload,
            },
        )
        db.add(job)
        db.commit()
        db.refresh(job)

        background_tasks.add_task(
            _investing_scrape_background,
            job.id,
            target_url,
            instruction_prompt,
            bool(request.force_login),
            bool(request.test_mode),
            int(request.test_duration_seconds),
        )

        return ScrapingJobResponse(
            id=job.id,
            profile_url=job.profile_url,
            status=job.status,
            created_at=job.created_at,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("❌ Erro no investing_scrape para %s: %s", request.url, e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/investing_scrape/{job_id}", response_model=ScrapingJobResponse)
async def get_investing_scrape_status(job_id: str, db: Session = Depends(get_db)):
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
        logger.error("❌ Erro ao obter status do investing_scrape: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/investing_scrape/{job_id}/results", response_model=InvestingScrapeJobResultResponse)
async def get_investing_scrape_results(job_id: str, db: Session = Depends(get_db)):
    try:
        job = db.query(ScrapingJob).filter(ScrapingJob.id == job_id).first()
        if not job:
            raise HTTPException(status_code=404, detail="Job não encontrado")

        if job.status not in ("completed", "failed"):
            return JSONResponse(
                status_code=200,
                content={"detail": f"Job ainda não foi concluído. Status: {job.status}"},
            )

        metadata = job.metadata_json if isinstance(job.metadata_json, dict) else {}
        request_payload = metadata.get("request") if isinstance(metadata.get("request"), dict) else {}
        result_payload = metadata.get("result") if isinstance(metadata.get("result"), dict) else {}

        return InvestingScrapeJobResultResponse(
            job_id=job.id,
            status=job.status,
            url=str(request_payload.get("url") or job.profile_url),
            prompt=str(request_payload.get("prompt") or ""),
            data=result_payload.get("data"),
            raw_result=result_payload.get("raw_result"),
            error_message=job.error_message or result_payload.get("error"),
            completed_at=job.completed_at,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("❌ Erro ao obter resultado do investing_scrape: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/profiles/{username}", response_model=ProfileResponse)
async def get_profile(
    username: str,
    db: Session = Depends(get_db),
):
    """
    Obtem informacoes de um perfil.

    Args:
        username: Username do perfil
        db: Sessao do banco de dados

    Returns:
        Informacoes do perfil
    """
    try:
        profile = db.query(Profile).filter(
            Profile.instagram_username == username
        ).first()

        if not profile:
            logger.info(
                "Perfil %s nao encontrado no banco. Executando scrape sob demanda...",
                username,
            )
            profile_url = _normalize_profile_url(f"https://www.instagram.com/{username}/")
            await instagram_scraper.scrape_profile_info(
                profile_url=profile_url,
                db=db,
                save_to_db=True,
                cache_ttl_days=settings.profile_cache_ttl_days,
            )
            profile = db.query(Profile).filter(
                Profile.instagram_username == username
            ).first()

        if not profile:
            raise HTTPException(status_code=404, detail="Perfil nao encontrado")

        return ProfileResponse.from_orm(profile)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Erro ao obter perfil: {e}")
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


# ==================== Session Endpoints ====================

@router.get("/instagram_sessions")
async def list_instagram_sessions(
    username: str | None = None,
    active_only: bool = True,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    """
    Lista sessões de Instagram importadas/salvas no banco.
    """
    try:
        safe_limit = min(max(int(limit), 1), 500)
        normalized_username = _normalize_session_username(username)
        query = db.query(InstagramSession)
        if active_only:
            query = query.filter(InstagramSession.is_active.is_(True))
        if normalized_username:
            query = query.filter(InstagramSession.instagram_username == normalized_username)

        sessions = query.order_by(InstagramSession.updated_at.desc()).limit(safe_limit).all()
        items = []
        for session in sessions:
            storage_state = session.storage_state if isinstance(session.storage_state, dict) else {}
            cookies = browser_use_agent.get_cookies(storage_state)
            items.append(
                {
                    "id": session.id,
                    "instagram_username": session.instagram_username,
                    "is_active": bool(session.is_active),
                    "cookies_count": len(cookies),
                    "has_user_agent": bool(browser_use_agent.get_user_agent(storage_state)),
                    "last_used_at": session.last_used_at,
                    "created_at": session.created_at,
                    "updated_at": session.updated_at,
                }
            )

        return {
            "total": len(items),
            "filters": {
                "username": normalized_username,
                "active_only": bool(active_only),
                "limit": safe_limit,
            },
            "items": items,
        }
    except Exception as e:
        logger.error("❌ Erro ao listar sessões do Instagram: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/instagram_sessions/{session_id}/deactivate")
async def deactivate_instagram_session(
    session_id: str,
    db: Session = Depends(get_db),
):
    """
    Desativa uma sessão específica do Instagram.
    """
    try:
        session = db.query(InstagramSession).filter(InstagramSession.id == session_id).first()
        if not session:
            raise HTTPException(status_code=404, detail="Sessao nao encontrada")

        if not session.is_active:
            return {
                "id": session.id,
                "instagram_username": session.instagram_username,
                "is_active": False,
                "message": "Sessao ja estava inativa.",
            }

        session.is_active = False
        db.commit()
        return {
            "id": session.id,
            "instagram_username": session.instagram_username,
            "is_active": False,
            "message": "Sessao desativada com sucesso.",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("❌ Erro ao desativar sessao %s: %s", session_id, e)
        raise HTTPException(status_code=500, detail=str(e))


# ==================== Background Tasks ====================

async def _scrape_profile_background(job_id: str, profile_url: str, options: dict | None = None):
    """
    Executa scraping em background.

    Args:
        job_id: ID do job
        profile_url: URL do perfil a raspar
        options: opções avançadas do fluxo
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

        opts = dict(options or {})
        flow = (opts.get("flow") or "default").lower().strip()
        test_mode = bool(opts.get("test_mode", False))
        test_duration_seconds = int(opts.get("test_duration_seconds", 120))
        default_max_posts = 3 if flow == "recent_likes" else 5
        max_posts = int(opts.get("max_posts", default_max_posts))
        recent_days = int(opts.get("recent_days", 1))
        if "recent_hours" in opts and "recent_days" not in opts:
            try:
                recent_days = max(1, int((int(opts.get("recent_hours", 24)) + 23) / 24))
            except Exception:
                recent_days = int(opts.get("recent_days", 1))
        max_like_users_per_post = int(opts.get("max_like_users_per_post", 30))
        session_username = str(opts.get("session_username") or "").strip() or None
        # Etapa de enriquecimento de perfis curtidores foi removida do /scrape.
        # Mesmo que venha true na request, forçamos false neste job.
        collect_like_user_profiles = False

        # Executar scraping de acordo com o fluxo.
        if test_mode:
            logger.info(
                "🧪 Test mode ativo para job %s. Simulando execução por %ss...",
                job_id,
                test_duration_seconds,
            )
            await asyncio.sleep(test_duration_seconds)
            fake_profile_url = _normalize_profile_url(profile_url)
            fake_username = _extract_instagram_username(fake_profile_url) or "dummy_profile"
            if flow == "recent_likes":
                result = {
                    "status": "success",
                    "flow": "recent_likes",
                    "profile": {
                        "username": fake_username,
                        "profile_url": fake_profile_url,
                    },
                    "posts": [
                        {
                            "post_url": f"{fake_profile_url}reel/DUMMY001/",
                            "caption": "Post dummy 1 - modo teste",
                            "like_count": 12,
                            "comment_count": 3,
                            "posted_at": "1h",
                            "is_recent_24h": True,
                            "likes_accessible": True,
                            "like_users": [
                                "https://www.instagram.com/dummy_user_1/",
                                "https://www.instagram.com/dummy_user_2/",
                            ],
                            "like_users_data": [],
                            "error": None,
                        },
                        {
                            "post_url": f"{fake_profile_url}reel/DUMMY002/",
                            "caption": "Post dummy 2 - modo teste",
                            "like_count": 0,
                            "comment_count": 0,
                            "posted_at": "2d",
                            "is_recent_24h": False,
                            "likes_accessible": False,
                            "like_users": [],
                            "like_users_data": [],
                            "error": "post_older_than_window",
                        },
                    ],
                    "summary": {
                        "total_posts": 2,
                        "recent_posts": 1,
                        "total_like_users": 2,
                        "scraped_at": datetime.utcnow().isoformat(),
                    },
                }
            else:
                result = {
                    "status": "success",
                    "flow": "default",
                    "profile": {
                        "username": fake_username,
                        "profile_url": fake_profile_url,
                        "bio": "Perfil dummy de teste",
                        "is_private": False,
                        "follower_count": 1234,
                        "verified": False,
                    },
                    "posts": [
                        {
                            "post_url": f"{fake_profile_url}p/DUMMYPOST001/",
                            "caption": "Post dummy default",
                            "like_count": 10,
                            "comment_count": 1,
                            "posted_at": datetime.utcnow().isoformat(),
                        }
                    ],
                    "interactions": [],
                    "summary": {
                        "total_posts": 1,
                        "total_interactions": 0,
                        "scraped_at": datetime.utcnow().isoformat(),
                    },
                }
        elif flow == "recent_likes":
            result = await instagram_scraper.scrape_recent_posts_like_users(
                profile_url=profile_url,
                max_posts=max_posts,
                recent_days=recent_days,
                max_like_users_per_post=max_like_users_per_post,
                collect_like_user_profiles=collect_like_user_profiles,
                db=db,
                session_username=session_username,
            )
        else:
            result = await instagram_scraper.scrape_profile(
                profile_url=profile_url,
                max_posts=max_posts,
                db=db,
                session_username=session_username,
            )

        # Atualizar job com resultados
        job.status = "completed"
        job.completed_at = datetime.utcnow()
        summary = result.get("summary", {}) if isinstance(result, dict) else {}
        total_posts = int(summary.get("total_posts", 0) or 0)
        if flow == "recent_likes":
            total_interactions = int(summary.get("total_like_users", 0) or 0)
        else:
            total_interactions = int(summary.get("total_interactions", 0) or 0)

        job.posts_scraped = total_posts
        job.interactions_scraped = total_interactions
        base_metadata = job.metadata_json if isinstance(job.metadata_json, dict) else {}
        metadata = dict(base_metadata)
        metadata["flow"] = flow
        metadata["options"] = dict(opts)
        metadata["result"] = result
        job.metadata_json = metadata
        flag_modified(job, "metadata_json")
        db.commit()

        logger.info(f"✅ Job concluído: {job_id}")

    except Exception as e:
        logger.exception("❌ Erro no scraping em background: %s", e)

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


async def _generic_scrape_background(
    job_id: str,
    target_url: str,
    prompt: str,
    test_mode: bool = False,
    test_duration_seconds: int = 120,
    session_username: str | None = None,
):
    """
    Executa generic scrape em background.
    """
    db = None
    try:
        db = next(get_db())
        job = db.query(ScrapingJob).filter(ScrapingJob.id == job_id).first()
        if not job:
            logger.error("Job generic_scrape nao encontrado: %s", job_id)
            return

        job.status = "running"
        job.started_at = datetime.utcnow()
        db.commit()

        storage_state = None
        normalized_session_username = _normalize_session_username(session_username)
        if not test_mode:
            session = _get_active_instagram_session(db, normalized_session_username)
            if normalized_session_username and not session:
                logger.warning(
                    "Sessao Instagram solicitada nao encontrada/ativa. username=%s",
                    normalized_session_username,
                )
                raise RuntimeError(
                    f"Sessao Instagram '@{normalized_session_username}' nao encontrada ou inativa."
                )
            if session and isinstance(session.storage_state, dict):
                is_valid = await browser_use_agent.is_instagram_session_valid(session.storage_state)
                if not is_valid:
                    logger.warning(
                        "Sessao Instagram invalida/expirada. id=%s username=%s",
                        session.id,
                        session.instagram_username,
                    )
                    session.is_active = False
                    db.commit()
                    if normalized_session_username:
                        raise RuntimeError(
                            f"Sessao Instagram '@{normalized_session_username}' expirada ou invalida."
                        )
                else:
                    logger.info(
                        "Usando sessao Instagram. id=%s username=%s",
                        session.id,
                        session.instagram_username,
                    )
                    session.last_used_at = datetime.utcnow()
                    db.commit()
                    storage_state = session.storage_state
            elif not normalized_session_username:
                logger.info("Nenhuma sessao Instagram ativa encontrada; seguindo sem autenticacao.")

        if test_mode:
            await asyncio.sleep(test_duration_seconds)
            result = {
                "status": "success",
                "url": target_url,
                "data": {
                    "title": "Dummy Generic Scrape Result",
                    "note": "Resultado ficticio de teste",
                },
                "raw_result": '{"title":"Dummy Generic Scrape Result","note":"Resultado ficticio de teste"}',
                "error": None,
            }
        else:
            result = await browser_use_agent.generic_scrape(
                url=target_url,
                prompt=prompt,
                storage_state=storage_state,
            )

        base_metadata = job.metadata_json if isinstance(job.metadata_json, dict) else {}
        metadata = dict(base_metadata)
        metadata["flow"] = "generic"
        metadata["result"] = result
        job.metadata_json = metadata
        flag_modified(job, "metadata_json")

        error_message = result.get("error")
        if not error_message and isinstance(result.get("data"), dict):
            error_message = result["data"].get("error")
        if result.get("status") == "failed" and not error_message:
            error_message = "generic_scrape_failed"

        if error_message:
            job.status = "failed"
            job.error_message = str(error_message)
        else:
            job.status = "completed"
            job.error_message = None

        job.completed_at = datetime.utcnow()
        job.posts_scraped = 0
        job.interactions_scraped = 0
        db.commit()
        logger.info("✅ Generic scrape job concluido: %s", job_id)
    except Exception as e:
        logger.exception("❌ Erro no generic scrape em background: %s", e)
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


async def _investing_scrape_background(
    job_id: str,
    target_url: str,
    prompt: str,
    force_login: bool = False,
    test_mode: bool = False,
    test_duration_seconds: int = 120,
):
    """
    Executa investing scrape em background garantindo login/sessao persistida.
    """
    db = None
    try:
        db = next(get_db())
        job = db.query(ScrapingJob).filter(ScrapingJob.id == job_id).first()
        if not job:
            logger.error("Job investing_scrape nao encontrado: %s", job_id)
            return

        job.status = "running"
        job.started_at = datetime.utcnow()
        db.commit()

        if test_mode:
            await asyncio.sleep(test_duration_seconds)
            result = {
                "status": "success",
                "url": target_url,
                "data": {
                    "title": "Dummy Investing Scrape Result",
                    "note": "Resultado ficticio de teste",
                },
                "raw_result": '{"title":"Dummy Investing Scrape Result","note":"Resultado ficticio de teste"}',
                "error": None,
            }
        else:
            storage_state = await browser_use_agent.ensure_investing_session(db, force_login=force_login)
            if not storage_state:
                raise RuntimeError("Nao foi possivel obter sessao autenticada do Investing.")

            result = await browser_use_agent.generic_scrape(
                url=target_url,
                prompt=prompt,
                storage_state=storage_state,
            )

        base_metadata = job.metadata_json if isinstance(job.metadata_json, dict) else {}
        metadata = dict(base_metadata)
        metadata["flow"] = "investing"
        metadata["result"] = result
        job.metadata_json = metadata
        flag_modified(job, "metadata_json")

        if result.get("error"):
            job.status = "failed"
            job.error_message = str(result.get("error"))
        else:
            job.status = "completed"
            job.error_message = None

        job.completed_at = datetime.utcnow()
        job.posts_scraped = 0
        job.interactions_scraped = 0
        db.commit()
        logger.info("✅ Investing scrape job concluido: %s", job_id)
    except Exception as e:
        logger.exception("❌ Erro no investing scrape em background: %s", e)
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
