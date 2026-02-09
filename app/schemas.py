"""
Schemas Pydantic para validação de requisições e respostas da API.
"""

from pydantic import BaseModel, HttpUrl, Field, AliasChoices
from typing import Optional, List, Dict, Any
from datetime import datetime
from enum import Enum


class InteractionTypeSchema(str, Enum):
    """Tipos de interações."""
    LIKE = "like"
    COMMENT = "comment"
    SHARE = "share"
    SAVE = "save"


# ==================== Profile Schemas ====================

class ProfileBase(BaseModel):
    """Schema base para perfil."""
    instagram_username: str
    full_name: Optional[str] = None
    instagram_url: str
    bio: Optional[str] = None
    is_private: bool = False
    follower_count: Optional[int] = None
    following_count: Optional[int] = None
    post_count: Optional[int] = None
    verified: bool = False


class ProfileCreate(ProfileBase):
    """Schema para criação de perfil."""
    pass


class ProfileUpdate(BaseModel):
    """Schema para atualização de perfil."""
    bio: Optional[str] = None
    is_private: Optional[bool] = None
    follower_count: Optional[int] = None
    following_count: Optional[int] = None
    post_count: Optional[int] = None
    verified: Optional[bool] = None


class ProfileResponse(ProfileBase):
    """Schema para resposta de perfil."""
    id: str
    created_at: datetime
    updated_at: datetime
    last_scraped_at: Optional[datetime] = None
    post_count: Optional[int] = None

    class Config:
        from_attributes = True


class ProfileScrapeRequest(BaseModel):
    """Schema para scraping direto de um perfil por URL."""
    profile_url: str = Field(..., description="URL do perfil Instagram a ser extraido")
    session_username: Optional[str] = Field(
        default=None,
        description="Username da sessao Instagram a reutilizar (opcional)",
    )
    save_to_db: bool = Field(
        default=True,
        description="(Legado) Perfil sempre e atualizado na tabela profiles neste endpoint",
    )


class ProfileScrapeResponse(BaseModel):
    """Schema de resposta do scraping direto de perfil."""
    username: str
    full_name: Optional[str] = None
    profile_url: str
    bio: Optional[str] = None
    is_private: bool = False
    follower_count: Optional[int] = None
    following_count: Optional[int] = None
    post_count: Optional[int] = None
    verified: bool = False
    confidence: Optional[float] = None
    profile_id: Optional[str] = None
    last_scraped_at: Optional[datetime] = None
    extracted_at: datetime


# ==================== Generic Scrape Schemas ====================

class GenericScrapeRequest(BaseModel):
    """Schema para scraping generico de qualquer pagina web."""
    url: str = Field(..., description="URL da pagina a ser raspada")
    prompt: str = Field(..., description="Instrucoes de scraping e formato de retorno")
    session_username: Optional[str] = Field(
        default=None,
        description="Username da sessao Instagram a reutilizar (opcional)",
        validation_alias=AliasChoices(
            "session_username",
            "sessionUsername",
            "instagram_username",
            "instagramUsername",
        ),
    )
    test_mode: bool = Field(
        default=False,
        description="Se true, nao executa scraping real; simula um job assincrono",
    )
    test_duration_seconds: int = Field(
        default=120,
        ge=1,
        le=1800,
        description="Duracao da simulacao em segundos quando test_mode=true",
    )


class GenericScrapeResponse(BaseModel):
    """Resposta de scraping generico via Browser Use."""
    status: str = "success"
    url: str
    data: Optional[Any] = None
    raw_result: Optional[str] = None
    error: Optional[str] = None
    scraped_at: datetime


class GenericScrapeJobResultResponse(BaseModel):
    """Resultado completo de um job de generic scrape."""
    job_id: str
    status: str
    url: str
    prompt: str
    data: Optional[Any] = None
    raw_result: Optional[str] = None
    error_message: Optional[str] = None
    completed_at: Optional[datetime] = None


class InvestingScrapeRequest(BaseModel):
    """Schema para scraping no Investing com sessao autenticada."""
    url: str = Field(..., description="URL alvo dentro do Investing")
    prompt: str = Field(..., description="Instrucoes de scraping e formato de retorno")
    force_login: bool = Field(
        default=False,
        description="Se true, invalida a sessao salva e faz novo login antes do scraping",
    )
    test_mode: bool = Field(
        default=False,
        description="Se true, nao executa scraping real; simula um job assincrono",
    )
    test_duration_seconds: int = Field(
        default=120,
        ge=1,
        le=1800,
        description="Duracao da simulacao em segundos quando test_mode=true",
    )


class InvestingScrapeJobResultResponse(BaseModel):
    """Resultado completo de um job de investing scrape."""
    job_id: str
    status: str
    url: str
    prompt: str
    data: Optional[Any] = None
    raw_result: Optional[str] = None
    error_message: Optional[str] = None
    completed_at: Optional[datetime] = None


# ==================== Post Schemas ====================

class PostBase(BaseModel):
    """Schema base para post."""
    post_url: str
    caption: Optional[str] = None
    like_count: int = 0
    comment_count: int = 0
    share_count: int = 0
    save_count: int = 0
    posted_at: Optional[datetime] = None


class PostCreate(PostBase):
    """Schema para criação de post."""
    profile_id: str


class PostResponse(PostBase):
    """Schema para resposta de post."""
    id: str
    profile_id: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ==================== Interaction Schemas ====================

class InteractionBase(BaseModel):
    """Schema base para interação."""
    user_username: str
    user_url: str
    user_bio: Optional[str] = None
    user_is_private: bool = False
    interaction_type: InteractionTypeSchema
    comment_text: Optional[str] = None
    comment_likes: Optional[int] = None
    comment_replies: Optional[int] = None
    comment_posted_at: Optional[str] = None


class InteractionCreate(InteractionBase):
    """Schema para criação de interação."""
    post_id: str
    profile_id: str


class InteractionResponse(InteractionBase):
    """Schema para resposta de interação."""
    id: str
    post_id: str
    profile_id: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ==================== Scraping Job Schemas ====================

class ScrapingJobCreate(BaseModel):
    """Schema para criar job de scraping."""
    profile_url: str = Field(..., description="URL do perfil Instagram a ser raspado")
    session_username: Optional[str] = Field(
        default=None,
        description="Username da sessao Instagram a reutilizar (opcional)",
    )
    flow: str = Field(default="default", description="Fluxo: default ou recent_likes")
    max_posts: int = Field(default=5, ge=1, le=20, description="Quantidade maxima de posts")
    recent_days: int = Field(
        default=1,
        ge=1,
        le=30,
        description="Janela de dias para considerar post recente",
        validation_alias=AliasChoices("recent_days", "recent_hours"),
    )
    max_like_users_per_post: int = Field(default=30, ge=1, le=200, description="Maximo de perfis curtidores por post")
    collect_like_user_profiles: bool = Field(
        default=False,
        description="(Deprecado no /scrape) Mantido por compatibilidade; atualmente ignorado neste endpoint",
    )
    test_mode: bool = Field(
        default=False,
        description="Se true, nao executa scraping real; simula job em running por alguns segundos e retorna resultado dummy",
    )
    test_duration_seconds: int = Field(
        default=120,
        ge=1,
        le=1800,
        description="Duracao da simulacao em segundos quando test_mode=true",
    )


class ScrapingJobResponse(BaseModel):
    """Schema para resposta de job de scraping."""
    id: str
    profile_url: str
    status: str
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error_message: Optional[str] = None
    posts_scraped: int = 0
    interactions_scraped: int = 0
    created_at: datetime

    class Config:
        from_attributes = True


# ==================== Scraping Result Schemas ====================

class ScrapingResultInteraction(BaseModel):
    """Resultado de uma interação extraída."""
    type: InteractionTypeSchema
    user_url: str
    user_username: str
    user_bio: Optional[str] = None
    is_private: bool = False
    comment_text: Optional[str] = None


class ScrapingResultPost(BaseModel):
    """Resultado de um post extraído."""
    post_url: str
    caption: Optional[str] = None
    like_count: int = 0
    comment_count: int = 0
    interactions: List[ScrapingResultInteraction] = []


class ScrapingLikeUserResult(BaseModel):
    """Perfil de usuário curtidor enriquecido."""
    user_url: str
    user_username: Optional[str] = None
    bio: Optional[str] = None
    is_private: Optional[bool] = None
    follower_count: Optional[int] = None
    verified: Optional[bool] = None
    confidence: Optional[float] = None
    error: Optional[str] = None


class ScrapingRecentPostResult(BaseModel):
    """Resultado detalhado para fluxo de posts recentes e curtidores."""
    post_url: str
    caption: Optional[str] = None
    like_count: int = 0
    comment_count: int = 0
    posted_at: Optional[str] = None
    is_recent_24h: bool = False
    likes_accessible: bool = False
    like_users: List[str] = []
    like_users_data: List[ScrapingLikeUserResult] = []
    error: Optional[str] = None


class ScrapingResultProfile(BaseModel):
    """Resultado completo de scraping de um perfil."""
    username: str
    full_name: Optional[str] = None
    profile_url: str
    bio: Optional[str] = None
    is_private: bool = False
    follower_count: Optional[int] = None
    posts: List[ScrapingResultPost] = []


class ScrapingCompleteResponse(BaseModel):
    """Resposta completa de um job de scraping."""
    job_id: str
    status: str
    flow: Optional[str] = None
    profile: Optional[ScrapingResultProfile] = None
    extracted_posts: List[ScrapingRecentPostResult] = []
    total_posts: int = 0
    total_interactions: int = 0
    raw_result: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    completed_at: Optional[datetime] = None


# ==================== Error Schemas ====================

class ErrorResponse(BaseModel):
    """Schema para resposta de erro."""
    detail: str
    status_code: int
    timestamp: datetime = Field(default_factory=datetime.utcnow)


# ==================== Pagination Schemas ====================

class PaginationParams(BaseModel):
    """Parâmetros de paginação."""
    skip: int = Field(0, ge=0)
    limit: int = Field(100, ge=1, le=1000)


class PaginatedResponse(BaseModel):
    """Resposta paginada genérica."""
    total: int
    skip: int
    limit: int
    items: List[dict]
