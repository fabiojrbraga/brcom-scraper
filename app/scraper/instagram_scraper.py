"""
Scraper principal do Instagram.
Coordena o fluxo completo de raspagem: navegação, extração, processamento.
"""

import logging
import asyncio
import random
from typing import Optional, Dict, Any, List
from datetime import datetime
from urllib.parse import urlparse

from app.scraper.browserless_client import BrowserlessClient
from app.scraper.ai_extractor import AIExtractor
from app.models import Profile, Post, Interaction, InteractionType
from app.database import SessionLocal
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class InstagramScraper:
    """
    Scraper principal do Instagram.
    Coordena a raspagem de dados usando Browserless + IA.
    """

    def __init__(self):
        self.browserless = BrowserlessClient()
        self.ai_extractor = AIExtractor()
        self.user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
        ]

    async def close(self):
        """Fecha conexões."""
        await self.browserless.close()

    def _get_random_delay(self, min_sec: float = 1, max_sec: float = 5) -> float:
        """Retorna delay aleatório para simular comportamento humano."""
        return random.uniform(min_sec, max_sec)

    def _extract_username_from_url(self, url: str) -> str:
        """Extrai username da URL do Instagram."""
        # URL pode ser: https://instagram.com/username ou https://www.instagram.com/username/
        parts = url.rstrip("/").split("/")
        return parts[-1]

    async def scrape_profile(
        self,
        profile_url: str,
        max_posts: int = 5,
        db: Optional[Session] = None,
    ) -> Dict[str, Any]:
        """
        Raspa um perfil completo do Instagram.

        Args:
            profile_url: URL do perfil (ex: https://instagram.com/username)
            max_posts: Número máximo de posts a analisar
            db: Sessão do banco de dados

        Returns:
            Dicionário com dados extraídos
        """
        try:
            logger.info(f"🚀 Iniciando scraping do perfil: {profile_url}")

            # Normalizar URL
            if not profile_url.startswith("http"):
                profile_url = f"https://instagram.com/{profile_url}"

            username = self._extract_username_from_url(profile_url)

            # FASE 1: Capturar informações do perfil
            logger.info(f"📸 Capturando informações do perfil: {username}")
            await asyncio.sleep(self._get_random_delay())

            profile_screenshot = await self.browserless.screenshot(profile_url)
            profile_html = await self.browserless.get_html(profile_url)

            # FASE 2: Extrair informações do perfil com IA
            logger.info(f"🧠 Extraindo informações do perfil com IA...")
            profile_info = await self.ai_extractor.extract_profile_info(
                screenshot_base64=profile_screenshot,
                html_content=profile_html,
            )

            # FASE 3: Salvar perfil no banco
            if db:
                profile_db = await self._save_profile(db, profile_url, profile_info)
            else:
                profile_db = None

            # FASE 4: Raspar posts
            logger.info(f"📝 Raspando posts do perfil...")
            posts_data = await self._scrape_posts(
                profile_url,
                max_posts=max_posts,
                profile_html=profile_html,
            )

            # FASE 5: Raspar comentários e interações
            logger.info(f"💬 Raspando comentários e interações...")
            interactions = []
            for post_data in posts_data[:max_posts]:
                post_interactions = await self._scrape_post_interactions(
                    post_data["post_url"],
                    post_data,
                )
                interactions.extend(post_interactions)

            # FASE 6: Salvar dados no banco
            if db and profile_db:
                await self._save_posts_and_interactions(
                    db,
                    profile_db.id,
                    posts_data,
                    interactions,
                )

            # Compilar resultado final
            result = {
                "status": "success",
                "profile": {
                    "username": profile_info.get("username"),
                    "profile_url": profile_url,
                    "bio": profile_info.get("bio"),
                    "is_private": profile_info.get("is_private", False),
                    "follower_count": profile_info.get("follower_count"),
                    "verified": profile_info.get("verified", False),
                },
                "posts": posts_data,
                "interactions": interactions,
                "summary": {
                    "total_posts": len(posts_data),
                    "total_interactions": len(interactions),
                    "scraped_at": datetime.utcnow().isoformat(),
                },
            }

            logger.info(f"✅ Scraping concluído: {username}")
            logger.info(f"   - Posts: {len(posts_data)}")
            logger.info(f"   - Interações: {len(interactions)}")

            return result

        except Exception as e:
            logger.error(f"❌ Erro ao raspar perfil {profile_url}: {e}")
            raise

    async def _scrape_posts(
        self,
        profile_url: str,
        max_posts: int = 5,
        profile_html: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Raspa posts de um perfil.

        Args:
            profile_url: URL do perfil
            max_posts: Número máximo de posts
            profile_html: HTML do perfil (para extração com IA)

        Returns:
            Lista de posts extraídos
        """
        try:
            # Simular scroll para carregar mais posts
            await asyncio.sleep(self._get_random_delay())

            # Capturar screenshot dos posts
            posts_screenshot = await self.browserless.screenshot(profile_url)

            # Extrair posts com IA
            posts_data = await self.ai_extractor.extract_posts_info(
                screenshot_base64=posts_screenshot,
                html_content=profile_html,
            )

            logger.info(f"✅ {len(posts_data)} posts extraídos")
            return posts_data[:max_posts]

        except Exception as e:
            logger.error(f"❌ Erro ao raspar posts: {e}")
            return []

    async def _scrape_post_interactions(
        self,
        post_url: str,
        post_data: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """
        Raspa comentários e interações de um post.

        Args:
            post_url: URL do post
            post_data: Dados do post

        Returns:
            Lista de interações extraídas
        """
        try:
            logger.info(f"📍 Raspando interações do post: {post_url}")

            await asyncio.sleep(self._get_random_delay(2, 5))

            # Capturar screenshot dos comentários
            comments_screenshot = await self.browserless.screenshot(post_url)

            # Extrair comentários com IA
            comments = await self.ai_extractor.extract_comments(
                screenshot_base64=comments_screenshot,
            )

            # Processar comentários em interações
            interactions = []
            for comment in comments:
                interaction = {
                    "type": "comment",
                    "user_url": comment.get("user_url"),
                    "user_username": comment.get("user_username"),
                    "comment_text": comment.get("comment_text"),
                    "comment_likes": comment.get("comment_likes", 0),
                    "comment_replies": comment.get("comment_replies", 0),
                }
                interactions.append(interaction)

            # Adicionar likes como interação (se houver contagem)
            if post_data.get("like_count", 0) > 0:
                interactions.append({
                    "type": "like",
                    "count": post_data.get("like_count"),
                })

            logger.info(f"✅ {len(interactions)} interações extraídas do post")
            return interactions

        except Exception as e:
            logger.error(f"❌ Erro ao raspar interações do post: {e}")
            return []

    async def _save_profile(
        self,
        db: Session,
        profile_url: str,
        profile_info: Dict[str, Any],
    ) -> Profile:
        """
        Salva informações do perfil no banco de dados.

        Args:
            db: Sessão do banco
            profile_url: URL do perfil
            profile_info: Informações extraídas

        Returns:
            Objeto Profile salvo
        """
        try:
            username = profile_info.get("username")

            # Verificar se perfil já existe
            existing = db.query(Profile).filter(
                Profile.instagram_username == username
            ).first()

            if existing:
                # Atualizar perfil existente
                existing.bio = profile_info.get("bio")
                existing.is_private = profile_info.get("is_private", False)
                existing.follower_count = profile_info.get("follower_count")
                existing.verified = profile_info.get("verified", False)
                existing.last_scraped_at = datetime.utcnow()
                db.commit()
                logger.info(f"✅ Perfil atualizado: {username}")
                return existing
            else:
                # Criar novo perfil
                profile = Profile(
                    instagram_username=username,
                    instagram_url=profile_url,
                    bio=profile_info.get("bio"),
                    is_private=profile_info.get("is_private", False),
                    follower_count=profile_info.get("follower_count"),
                    verified=profile_info.get("verified", False),
                    last_scraped_at=datetime.utcnow(),
                )
                db.add(profile)
                db.commit()
                db.refresh(profile)
                logger.info(f"✅ Novo perfil salvo: {username}")
                return profile

        except Exception as e:
            logger.error(f"❌ Erro ao salvar perfil: {e}")
            db.rollback()
            raise

    async def _save_posts_and_interactions(
        self,
        db: Session,
        profile_id: str,
        posts_data: List[Dict[str, Any]],
        interactions: List[Dict[str, Any]],
    ) -> None:
        """
        Salva posts e interações no banco de dados.

        Args:
            db: Sessão do banco
            profile_id: ID do perfil
            posts_data: Lista de posts
            interactions: Lista de interações
        """
        try:
            for post_data in posts_data:
                post_url = post_data.get("post_url")

                # Verificar se post já existe
                existing_post = db.query(Post).filter(
                    Post.post_url == post_url
                ).first()

                if not existing_post:
                    post = Post(
                        profile_id=profile_id,
                        post_url=post_url,
                        caption=post_data.get("caption"),
                        like_count=post_data.get("like_count", 0),
                        comment_count=post_data.get("comment_count", 0),
                        posted_at=post_data.get("posted_at"),
                    )
                    db.add(post)
                    db.flush()
                    post_id = post.id
                else:
                    post_id = existing_post.id

                # Salvar interações do post
                for interaction_data in interactions:
                    if interaction_data.get("type") == "comment":
                        user_url = interaction_data.get("user_url")

                        # Verificar se interação já existe
                        existing_interaction = db.query(Interaction).filter(
                            Interaction.post_id == post_id,
                            Interaction.user_url == user_url,
                            Interaction.interaction_type == InteractionType.COMMENT,
                        ).first()

                        if not existing_interaction:
                            interaction = Interaction(
                                post_id=post_id,
                                profile_id=profile_id,
                                user_username=interaction_data.get("user_username"),
                                user_url=user_url,
                                interaction_type=InteractionType.COMMENT,
                                comment_text=interaction_data.get("comment_text"),
                                comment_likes=interaction_data.get("comment_likes", 0),
                                comment_replies=interaction_data.get("comment_replies", 0),
                            )
                            db.add(interaction)

            db.commit()
            logger.info(f"✅ Posts e interações salvos no banco")

        except Exception as e:
            logger.error(f"❌ Erro ao salvar posts e interações: {e}")
            db.rollback()
            raise


# Instância global do scraper
instagram_scraper = InstagramScraper()
