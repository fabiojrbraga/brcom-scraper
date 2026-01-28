"""
Schemas Pydantic para validação de requisições e respostas da API.
"""

from pydantic import BaseModel, HttpUrl, Field
from typing import Optional, List
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


class ScrapingResultProfile(BaseModel):
    """Resultado completo de scraping de um perfil."""
    username: str
    profile_url: str
    bio: Optional[str] = None
    is_private: bool = False
    follower_count: Optional[int] = None
    posts: List[ScrapingResultPost] = []


class ScrapingCompleteResponse(BaseModel):
    """Resposta completa de um job de scraping."""
    job_id: str
    status: str
    profile: Optional[ScrapingResultProfile] = None
    total_posts: int = 0
    total_interactions: int = 0
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
