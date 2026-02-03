"""
Modelos SQLAlchemy para persistência de dados do Instagram.
"""

from sqlalchemy import Column, String, Integer, Text, Boolean, DateTime, ForeignKey, JSON, Enum
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
from datetime import datetime
import uuid
import enum

Base = declarative_base()


class InteractionType(str, enum.Enum):
    """Tipos de interações possíveis."""
    LIKE = "like"
    COMMENT = "comment"
    SHARE = "share"
    SAVE = "save"


class Profile(Base):
    """Modelo para perfis do Instagram."""
    __tablename__ = "profiles"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    instagram_username = Column(String(255), unique=True, nullable=False, index=True)
    full_name = Column(String(255), nullable=True, index=True)
    instagram_url = Column(String(500), nullable=False)
    bio = Column(Text, nullable=True)
    is_private = Column(Boolean, default=False)
    follower_count = Column(Integer, nullable=True)
    following_count = Column(Integer, nullable=True)
    post_count = Column(Integer, nullable=True)
    profile_picture_url = Column(String(500), nullable=True)
    verified = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_scraped_at = Column(DateTime, nullable=True)
    metadata_json = Column("metadata", JSON, nullable=True)

    # Relacionamentos
    posts = relationship("Post", back_populates="profile", cascade="all, delete-orphan")
    interactions = relationship("Interaction", back_populates="profile", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Profile(username={self.instagram_username}, private={self.is_private})>"



class Post(Base):
    """Modelo para posts do Instagram."""
    __tablename__ = "posts"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    profile_id = Column(String(36), ForeignKey("profiles.id"), nullable=False, index=True)
    post_url = Column(String(500), unique=True, nullable=False)
    post_id = Column(String(255), nullable=True)  # ID nativo do Instagram
    caption = Column(Text, nullable=True)
    like_count = Column(Integer, default=0)
    comment_count = Column(Integer, default=0)
    share_count = Column(Integer, default=0)
    save_count = Column(Integer, default=0)
    posted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    metadata_json = Column("metadata", JSON, nullable=True)

    # Relacionamentos
    profile = relationship("Profile", back_populates="posts")
    interactions = relationship("Interaction", back_populates="post", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Post(url={self.post_url}, likes={self.like_count})>"


class Interaction(Base):
    """Modelo para interações (likes, comentários, etc) no Instagram."""
    __tablename__ = "interactions"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    post_id = Column(String(36), ForeignKey("posts.id"), nullable=False, index=True)
    profile_id = Column(String(36), ForeignKey("profiles.id"), nullable=False, index=True)
    user_username = Column(String(255), nullable=False, index=True)
    user_url = Column(String(500), nullable=False)
    user_bio = Column(Text, nullable=True)
    user_is_private = Column(Boolean, default=False)
    user_follower_count = Column(Integer, nullable=True)
    interaction_type = Column(Enum(InteractionType), nullable=False)
    comment_text = Column(Text, nullable=True)
    comment_likes = Column(Integer, default=0, nullable=True)
    comment_replies = Column(Integer, default=0, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    metadata_json = Column("metadata", JSON, nullable=True)

    # Relacionamentos
    post = relationship("Post", back_populates="interactions")
    profile = relationship("Profile", back_populates="interactions")

    def __repr__(self):
        return f"<Interaction(user={self.user_username}, type={self.interaction_type})>"


class ScrapingJob(Base):
    """Modelo para rastrear jobs de scraping."""
    __tablename__ = "scraping_jobs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    profile_url = Column(String(500), nullable=False)
    status = Column(String(50), default="pending")  # pending, running, completed, failed
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    error_message = Column(Text, nullable=True)
    posts_scraped = Column(Integer, default=0)
    interactions_scraped = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    metadata_json = Column("metadata", JSON, nullable=True)

    def __repr__(self):
        return f"<ScrapingJob(url={self.profile_url}, status={self.status})>"


class InstagramSession(Base):
    """SessÃµes autenticadas do Instagram para reutilizaÃ§Ã£o."""
    __tablename__ = "instagram_sessions"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    instagram_username = Column(String(255), nullable=True, index=True)
    storage_state = Column(JSON, nullable=False)
    is_active = Column(Boolean, default=True)
    last_used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<InstagramSession(username={self.instagram_username}, active={self.is_active})>"
