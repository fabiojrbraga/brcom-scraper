"""
Scraper principal do Instagram.
Coordena o fluxo completo de raspagem: navegação, extração, processamento.
"""

import logging
import asyncio
import random
import re
import json
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone

from app.scraper.browserless_client import BrowserlessClient
from app.scraper.browser_use_agent import browser_use_agent
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

    def _recover_posts_from_raw_result(self, raw_result: str) -> List[Dict[str, Any]]:
        """
        Tenta recuperar payload JSON com "posts" mesmo quando o agente retorna texto extra.
        """
        if not raw_result:
            return []
        decoder = json.JSONDecoder()
        for idx, char in enumerate(raw_result):
            if char != "{":
                continue
            try:
                obj, _ = decoder.raw_decode(raw_result[idx:])
            except Exception:
                continue
            if isinstance(obj, dict) and isinstance(obj.get("posts"), list):
                return obj.get("posts", [])
        return []

    def _is_recent_post(self, posted_at: Any, recent_hours: int = 24) -> bool:
        """
        Determina se o post é recente baseado no texto/valor retornado pelo scraper.
        """
        if posted_at is None:
            return False

        now = datetime.now(timezone.utc)

        if isinstance(posted_at, datetime):
            post_dt = posted_at if posted_at.tzinfo else posted_at.replace(tzinfo=timezone.utc)
            return (now - post_dt).total_seconds() <= recent_hours * 3600

        text = str(posted_at).strip().lower()
        if not text:
            return False

        if text in {"now", "just now", "agora", "agora mesmo"}:
            return True

        iso_candidate = text.replace("z", "+00:00")
        try:
            parsed = datetime.fromisoformat(iso_candidate)
            parsed = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            return (now - parsed).total_seconds() <= recent_hours * 3600
        except Exception:
            pass

        number_match = re.search(r"(\d+)", text)
        value = int(number_match.group(1)) if number_match else None

        minute_tokens = ("min", "minute", "minutes", "minuto", "minutos", "m")
        hour_tokens = ("hour", "hours", "hora", "horas", "h")
        day_tokens = ("day", "days", "dia", "dias", "d")
        week_tokens = ("week", "weeks", "semana", "semanas", "w")

        if any(token in text for token in minute_tokens):
            return True
        if any(token in text for token in hour_tokens):
            if value is None:
                return False
            return value <= recent_hours
        if any(token in text for token in day_tokens):
            return False
        if any(token in text for token in week_tokens):
            return False

        return False

    async def _extract_like_user_profile(
        self,
        user_url: str,
        cookies: Optional[list[dict]] = None,
    ) -> Dict[str, Any]:
        """
        Captura screenshot + HTML e aplica IA para extrair dados do perfil curtidor.
        """
        username = self._extract_username_from_url(user_url)
        try:
            screenshot = await self.browserless.screenshot(user_url, cookies=cookies)
            html = await self.browserless.get_html(user_url, cookies=cookies)
            extracted = await self.ai_extractor.extract_user_info(
                screenshot_base64=screenshot,
                html_content=html,
                username=username,
            )
            return {
                "user_url": user_url,
                "user_username": username,
                "bio": extracted.get("bio"),
                "is_private": extracted.get("is_private"),
                "follower_count": extracted.get("follower_count"),
                "verified": extracted.get("verified"),
                "confidence": extracted.get("confidence"),
            }
        except Exception as exc:
            logger.warning("⚠️ Falha ao enriquecer perfil curtidor %s: %s", user_url, exc)
            return {
                "user_url": user_url,
                "user_username": username,
                "error": str(exc),
            }

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

            storage_state = await browser_use_agent.ensure_instagram_session(db) if db else None
            cookies = browser_use_agent.get_cookies(storage_state)

            # FASE 1: Capturar informações do perfil
            logger.info(f"📸 Capturando informações do perfil: {username}")
            await asyncio.sleep(self._get_random_delay())

            profile_screenshot = await self.browserless.screenshot(profile_url, cookies=cookies)
            profile_html = await self.browserless.get_html(profile_url, cookies=cookies)

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

            # FASE 4: Raspar posts usando Browser Use
            logger.info(f"📝 Raspando posts do perfil com Browser Use...")
            posts_data = await self._scrape_posts(
                profile_url,
                max_posts=max_posts,
                profile_html=profile_html,
                cookies=cookies,
                storage_state=storage_state,
            )

            # FASE 5: Raspar comentários e interações
            logger.info(f"💬 Raspando comentários e interações...")
            interactions = []
            for post_data in posts_data[:max_posts]:
                post_interactions = await self._scrape_post_interactions(
                    post_data["post_url"],
                    post_data,
                    cookies=cookies,
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
            logger.exception("❌ Erro ao raspar perfil %s: %s", profile_url, e)
            raise

    async def scrape_recent_posts_like_users(
        self,
        profile_url: str,
        max_posts: int = 3,
        recent_hours: int = 24,
        max_like_users_per_post: int = 30,
        collect_like_user_profiles: bool = True,
        db: Optional[Session] = None,
    ) -> Dict[str, Any]:
        """
        Fluxo avançado:
        1) coleta os posts mais recentes do perfil;
        2) para posts dentro da janela recente, coleta usuários que curtiram;
        3) opcionalmente enriquece os perfis curtidores com IA.
        """
        try:
            logger.info("🚀 Iniciando fluxo recent_likes para %s", profile_url)

            if not profile_url.startswith("http"):
                profile_url = f"https://instagram.com/{profile_url}"

            username = self._extract_username_from_url(profile_url)
            storage_state = await browser_use_agent.ensure_instagram_session(db) if db else None
            cookies = browser_use_agent.get_cookies(storage_state)

            posts_data = await self._scrape_posts(
                profile_url=profile_url,
                max_posts=max_posts,
                cookies=cookies,
                storage_state=storage_state,
            )

            extracted_posts: List[Dict[str, Any]] = []
            total_like_users = 0
            total_recent_posts = 0

            for post in posts_data[:max_posts]:
                post_url = post.get("post_url")
                if not post_url:
                    continue

                posted_at = post.get("posted_at")
                is_recent = self._is_recent_post(posted_at, recent_hours=recent_hours)
                if is_recent:
                    total_recent_posts += 1

                post_payload: Dict[str, Any] = {
                    "post_url": post_url,
                    "caption": post.get("caption"),
                    "like_count": post.get("like_count", 0),
                    "comment_count": post.get("comment_count", 0),
                    "posted_at": posted_at,
                    "is_recent_24h": is_recent,
                    "likes_accessible": False,
                    "like_users": [],
                    "like_users_data": [],
                    "error": None,
                }

                if not is_recent:
                    post_payload["error"] = "post_older_than_window"
                    extracted_posts.append(post_payload)
                    continue

                like_users_result = await browser_use_agent.scrape_post_like_users(
                    post_url=post_url,
                    storage_state=storage_state,
                    max_users=max_like_users_per_post,
                )

                post_payload["likes_accessible"] = bool(like_users_result.get("likes_accessible"))
                post_payload["error"] = like_users_result.get("error")

                like_users = like_users_result.get("like_users") or []
                if isinstance(like_users, list):
                    dedup_users = []
                    for item in like_users:
                        if isinstance(item, str) and item not in dedup_users:
                            dedup_users.append(item)
                    post_payload["like_users"] = dedup_users
                else:
                    post_payload["like_users"] = []

                total_like_users += len(post_payload["like_users"])

                if collect_like_user_profiles and post_payload["like_users"]:
                    for user_url in post_payload["like_users"]:
                        user_data = await self._extract_like_user_profile(user_url=user_url, cookies=cookies)
                        post_payload["like_users_data"].append(user_data)

                extracted_posts.append(post_payload)

            result = {
                "status": "success",
                "flow": "recent_likes",
                "profile": {
                    "username": username,
                    "profile_url": profile_url,
                },
                "posts": extracted_posts,
                "summary": {
                    "total_posts": len(extracted_posts),
                    "recent_posts": total_recent_posts,
                    "total_like_users": total_like_users,
                    "scraped_at": datetime.utcnow().isoformat(),
                },
            }

            logger.info(
                "✅ Fluxo recent_likes concluído: posts=%s recentes=%s curtidores=%s",
                len(extracted_posts),
                total_recent_posts,
                total_like_users,
            )
            return result
        except Exception as exc:
            logger.exception("❌ Erro no fluxo recent_likes para %s: %s", profile_url, exc)
            raise

    async def _scrape_posts(
        self,
        profile_url: str,
        max_posts: int = 5,
        profile_html: Optional[str] = None,
        cookies: Optional[list[dict]] = None,
        storage_state: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Raspa posts de um perfil usando Browser Use Agent.

        Args:
            profile_url: URL do perfil
            max_posts: Número máximo de posts
            profile_html: HTML do perfil (não usado mais)
            cookies: Cookies da sessão (não usado mais)
            storage_state: Storage state da sessão autenticada

        Returns:
            Lista de posts extraídos
        """
        try:
            logger.info(f"🤖 Usando Browser Use para raspar {max_posts} posts...")

            # Usar Browser Use Agent para navegar e extrair posts
            result = await browser_use_agent.scrape_profile_posts(
                profile_url=profile_url,
                storage_state=storage_state,
                max_posts=max_posts,
            )

            posts_data = result.get("posts", [])

            if result.get("error"):
                logger.warning(f"⚠️ Browser Use retornou erro: {result['error']}")
                if result["error"] == "private_profile":
                    logger.info("🔒 Perfil privado detectado")
                elif result["error"] == "parse_failed":
                    logger.warning(f"⚠️ Falha ao parsear resposta: {result.get('raw_result', '')[:200]}")
                    recovered = self._recover_posts_from_raw_result(result.get("raw_result", ""))
                    if recovered:
                        logger.info("✅ Recuperados %s posts do raw_result.", len(recovered))
                        posts_data = recovered

            logger.info(f"✅ {len(posts_data)} posts extraídos via Browser Use")
            return posts_data[:max_posts]

        except Exception as e:
            logger.exception("❌ Erro ao raspar posts: %s", e)
            return []

    async def _scrape_post_interactions(
        self,
        post_url: str,
        post_data: Dict[str, Any],
        cookies: Optional[list[dict]] = None,
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
            comments_screenshot = await self.browserless.screenshot(post_url, cookies=cookies)

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
