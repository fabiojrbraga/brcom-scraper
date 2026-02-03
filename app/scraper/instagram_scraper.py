"""
Scraper principal do Instagram.
Coordena o fluxo completo de raspagem: navegação, extração, processamento.
"""

import logging
import asyncio
import random
import re
import json
from urllib.parse import urlparse
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone, timedelta

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

    def _extract_post_urls_from_html(self, html: str, max_posts: int) -> List[str]:
        """
        Extrai links canônicos de posts/reels (/p/... e /reel/...) a partir do HTML do perfil.
        """
        if not html:
            return []

        matches = re.findall(r'href=["\'](/(?:p|reel)/[A-Za-z0-9_-]+/?)(?:\?[^"\']*)?["\']', html)
        found: List[str] = []
        for path in matches:
            normalized = path if path.startswith("/") else f"/{path}"
            if not normalized.endswith("/"):
                normalized = f"{normalized}/"
            url = f"https://www.instagram.com{normalized}"
            if url not in found:
                found.append(url)
            if len(found) >= max_posts:
                break
        return found

    def _to_int_or_none(self, value: Any) -> Optional[int]:
        if value is None:
            return None
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)

        text = str(value).strip().lower()
        if not text:
            return None

        match = re.search(r"(\d+(?:[.,]\d+)?)\s*([km]?)", text)
        if not match:
            return None

        number_text = match.group(1)
        suffix = match.group(2)

        if "," in number_text and "." not in number_text:
            parts = number_text.split(",")
            if len(parts[-1]) == 3:
                number_text = "".join(parts)
            else:
                number_text = ".".join(parts)
        elif "." in number_text and "," not in number_text:
            parts = number_text.split(".")
            if len(parts[-1]) == 3 and len(parts) > 1:
                number_text = "".join(parts)
        else:
            number_text = number_text.replace(",", "")

        try:
            number = float(number_text)
        except ValueError:
            return None

        if suffix == "k":
            number *= 1_000
        elif suffix == "m":
            number *= 1_000_000

        return int(number)

    def _normalize_post_item(self, item: Dict[str, Any], fallback_url: Optional[str] = None) -> Dict[str, Any]:
        post_url = item.get("post_url") or item.get("canonical_post_url") or fallback_url
        if isinstance(post_url, str):
            post_url = post_url.strip()
            if post_url.startswith("/p/"):
                post_url = f"https://www.instagram.com{post_url}"
            if post_url and ("/p/" in post_url or "/reel/" in post_url) and not post_url.endswith("/"):
                post_url = f"{post_url}/"
        else:
            post_url = fallback_url

        caption = item.get("caption")
        if caption is None:
            caption = item.get("full_caption_text")
        if caption is not None:
            caption = str(caption).strip() or None

        posted_at = item.get("posted_at")
        if isinstance(posted_at, datetime):
            posted_at = posted_at.isoformat()
        elif posted_at is not None:
            posted_at = str(posted_at).strip() or None

        like_count = self._to_int_or_none(item.get("like_count"))
        comment_count = self._to_int_or_none(item.get("comment_count"))

        return {
            "post_url": post_url,
            "caption": caption,
            "like_count": like_count if like_count is not None else 0,
            "comment_count": comment_count if comment_count is not None else 0,
            "posted_at": posted_at,
        }

    def _merge_posts_data(
        self,
        primary: List[Dict[str, Any]],
        fallback: List[Dict[str, Any]],
        max_posts: int,
    ) -> List[Dict[str, Any]]:
        merged: List[Dict[str, Any]] = []
        by_url: Dict[str, Dict[str, Any]] = {}

        def _url_key(url: Optional[str]) -> Optional[str]:
            if not url:
                return None
            try:
                parsed = urlparse(url)
                return f"{parsed.netloc}{parsed.path}".rstrip("/")
            except Exception:
                return url.rstrip("/")

        for src in primary:
            normalized = self._normalize_post_item(src)
            url_key = _url_key(normalized.get("post_url"))
            if url_key and url_key not in by_url:
                by_url[url_key] = normalized
                merged.append(normalized)
            elif not url_key:
                merged.append(normalized)

        for src in fallback:
            normalized = self._normalize_post_item(src)
            url_key = _url_key(normalized.get("post_url"))
            if url_key and url_key in by_url:
                target = by_url[url_key]
                if not target.get("caption") and normalized.get("caption"):
                    target["caption"] = normalized["caption"]
                if target.get("like_count", 0) == 0 and normalized.get("like_count", 0) > 0:
                    target["like_count"] = normalized["like_count"]
                if target.get("comment_count", 0) == 0 and normalized.get("comment_count", 0) > 0:
                    target["comment_count"] = normalized["comment_count"]
                if not target.get("posted_at") and normalized.get("posted_at"):
                    target["posted_at"] = normalized["posted_at"]
                continue
            if url_key and url_key not in by_url:
                by_url[url_key] = normalized
            merged.append(normalized)
            if len(merged) >= max_posts:
                break

        return merged[:max_posts]

    async def _fallback_scrape_posts_via_browserless(
        self,
        profile_url: str,
        max_posts: int,
        cookies: Optional[list[dict]] = None,
        profile_html: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Fallback sem Browser Use:
        - extrai links /p/ do HTML do perfil
        - abre cada post diretamente e usa IA para extrair campos.
        """
        try:
            html = profile_html or await self.browserless.get_html(profile_url, cookies=cookies)
            post_urls = self._extract_post_urls_from_html(html, max_posts=max_posts)
            if not post_urls:
                return []

            recovered: List[Dict[str, Any]] = []
            for post_url in post_urls[:max_posts]:
                screenshot_base64: Optional[str] = None
                post_html: Optional[str] = None
                try:
                    screenshot_base64 = await self.browserless.screenshot(post_url, cookies=cookies)
                except Exception as exc:
                    logger.warning("⚠️ Falha ao capturar screenshot do post %s: %s", post_url, exc)
                try:
                    post_html = await self.browserless.get_html(post_url, cookies=cookies)
                except Exception as exc:
                    logger.warning("⚠️ Falha ao obter HTML do post %s: %s", post_url, exc)

                ai_candidates: List[Dict[str, Any]] = []
                if screenshot_base64 or post_html:
                    try:
                        ai_candidates = await self.ai_extractor.extract_posts_info(
                            screenshot_base64=screenshot_base64,
                            html_content=post_html,
                        )
                    except Exception as exc:
                        logger.warning("⚠️ IA não conseguiu extrair o post %s: %s", post_url, exc)

                selected: Dict[str, Any] = {}
                for candidate in ai_candidates:
                    if not isinstance(candidate, dict):
                        continue
                    candidate_url = str(candidate.get("post_url") or "").rstrip("/")
                    if candidate_url and candidate_url == post_url.rstrip("/"):
                        selected = candidate
                        break
                if not selected and ai_candidates:
                    first_candidate = next((c for c in ai_candidates if isinstance(c, dict)), None)
                    if first_candidate:
                        selected = first_candidate

                recovered.append(self._normalize_post_item(selected, fallback_url=post_url))

            return recovered[:max_posts]
        except Exception as exc:
            logger.warning("⚠️ Fallback via Browserless falhou: %s", exc)
            return []

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

        if text in {"today", "hoje"}:
            return True
        if text in {"yesterday", "ontem"}:
            return recent_hours >= 24

        relative_patterns = [
            (r"(\d+)\s*(?:m|min|mins|minute|minutes|minuto|minutos)\b", 1 / 60),
            (r"(\d+)\s*(?:h|hr|hrs|hour|hours|hora|horas)\b", 1),
            (r"(\d+)\s*(?:d|day|days|dia|dias)\b", 24),
            (r"(\d+)\s*(?:w|week|weeks|semana|semanas)\b", 24 * 7),
        ]

        for pattern, hour_multiplier in relative_patterns:
            match = re.search(pattern, text)
            if not match:
                continue
            value = int(match.group(1))
            return (value * hour_multiplier) <= recent_hours

        return False

    def _coerce_posted_at_datetime(self, posted_at: Any) -> Optional[datetime]:
        """
        Converte posted_at para datetime quando possível.
        Valores relativos como '1d', '2w' retornam None.
        """
        if posted_at is None:
            return None
        if isinstance(posted_at, datetime):
            return posted_at

        text = str(posted_at).strip()
        if not text:
            return None

        iso_candidate = text.replace("Z", "+00:00").replace("z", "+00:00")
        try:
            parsed = datetime.fromisoformat(iso_candidate)
            return parsed if parsed.tzinfo is None else parsed.replace(tzinfo=None)
        except Exception:
            return None

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

    async def scrape_profile_info(
        self,
        profile_url: str,
        db: Optional[Session] = None,
        save_to_db: bool = True,
        cache_ttl_days: int = 0,
    ) -> Dict[str, Any]:
        """
        Extrai somente dados do perfil (sem posts/interações).
        """
        try:
            if not profile_url.startswith("http"):
                profile_url = f"https://instagram.com/{profile_url}"

            username_fallback = self._extract_username_from_url(profile_url)
            normalized_url = profile_url.rstrip("/") + "/"
            profile_url = normalized_url

            if db and cache_ttl_days > 0:
                existing = db.query(Profile).filter(
                    Profile.instagram_url == normalized_url
                ).first()
                if not existing and username_fallback:
                    existing = db.query(Profile).filter(
                        Profile.instagram_username == username_fallback
                    ).first()

                if existing and existing.last_scraped_at:
                    threshold = datetime.utcnow() - timedelta(days=cache_ttl_days)
                    if existing.last_scraped_at >= threshold:
                        return {
                            "username": existing.instagram_username,
                            "profile_url": existing.instagram_url,
                            "bio": existing.bio,
                            "is_private": bool(existing.is_private),
                            "follower_count": existing.follower_count,
                            "following_count": existing.following_count,
                            "post_count": existing.post_count,
                            "verified": bool(existing.verified),
                            "confidence": None,
                            "profile_id": existing.id,
                            "last_scraped_at": existing.last_scraped_at,
                            "extracted_at": datetime.utcnow(),
                        }

            storage_state = await browser_use_agent.ensure_instagram_session(db) if db else None
            cookies = browser_use_agent.get_cookies(storage_state)

            profile_screenshot: Optional[str] = None
            profile_html: Optional[str] = None
            screenshot_error: Optional[str] = None
            html_error: Optional[str] = None

            try:
                profile_screenshot = await self.browserless.screenshot(profile_url, cookies=cookies)
            except Exception as exc:
                screenshot_error = str(exc)
                logger.warning("⚠️ Falha ao capturar screenshot do perfil %s: %s", profile_url, exc)

            try:
                profile_html = await self.browserless.get_html(profile_url, cookies=cookies)
            except Exception as exc:
                html_error = str(exc)
                logger.warning("⚠️ Falha ao obter HTML do perfil %s: %s", profile_url, exc)

            if not profile_screenshot and not profile_html:
                details = " ; ".join(
                    item for item in [screenshot_error, html_error] if item
                )
                raise RuntimeError(
                    f"Falha ao obter dados do Browserless para {profile_url}. {details}".strip()
                )

            profile_info = await self.ai_extractor.extract_profile_info(
                screenshot_base64=profile_screenshot,
                html_content=profile_html,
            )

            normalized = {
                "username": profile_info.get("username") or username_fallback,
                "profile_url": profile_url,
                "bio": profile_info.get("bio"),
                "is_private": bool(profile_info.get("is_private", False)),
                "follower_count": self._to_int_or_none(profile_info.get("follower_count")),
                "following_count": self._to_int_or_none(profile_info.get("following_count")),
                "post_count": self._to_int_or_none(profile_info.get("post_count")),
                "verified": bool(profile_info.get("verified", False)),
                "confidence": profile_info.get("confidence"),
                "extracted_at": datetime.utcnow(),
            }

            if db and save_to_db:
                profile_db = await self._save_profile(db, profile_url, normalized)
                normalized["profile_id"] = profile_db.id
                normalized["last_scraped_at"] = profile_db.last_scraped_at
            else:
                normalized["profile_id"] = None
                normalized["last_scraped_at"] = None

            return normalized
        except Exception as e:
            logger.exception("❌ Erro ao extrair dados do perfil %s: %s", profile_url, e)
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
            if collect_like_user_profiles:
                logger.info(
                    "ℹ️ collect_like_user_profiles foi solicitado, mas está desativado no /scrape."
                )

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

                # Enriquecimento de perfis curtidores foi removido do /scrape.
                # Mantemos like_users_data vazio por compatibilidade de contrato.

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

            if db:
                profile_payload = {
                    "username": username,
                    "bio": None,
                    "is_private": False,
                    "follower_count": None,
                    "verified": False,
                }
                profile_db = await self._save_profile(db, profile_url, profile_payload)
                await self._save_posts_and_interactions(db, profile_db.id, extracted_posts, [])

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

            normalized_primary = [
                self._normalize_post_item(post)
                for post in posts_data
                if isinstance(post, dict)
            ]

            if len(normalized_primary) < max_posts:
                logger.warning(
                    "⚠️ Browser Use retornou %s/%s posts. Tentando fallback via Browserless...",
                    len(normalized_primary),
                    max_posts,
                )
                fallback_posts = await self._fallback_scrape_posts_via_browserless(
                    profile_url=profile_url,
                    max_posts=max_posts,
                    cookies=cookies,
                    profile_html=profile_html,
                )
                if fallback_posts:
                    logger.info("✅ Fallback recuperou %s posts.", len(fallback_posts))
                posts_data = self._merge_posts_data(normalized_primary, fallback_posts, max_posts=max_posts)
            else:
                posts_data = normalized_primary

            logger.info(f"✅ {len(posts_data)} posts extraídos via Browser Use")
            return posts_data[:max_posts]

        except Exception as e:
            logger.exception("❌ Erro ao raspar posts: %s", e)
            fallback_posts = await self._fallback_scrape_posts_via_browserless(
                profile_url=profile_url,
                max_posts=max_posts,
                cookies=cookies,
                profile_html=profile_html,
            )
            if fallback_posts:
                logger.info("✅ Fallback recuperou %s posts após exceção.", len(fallback_posts))
                return fallback_posts[:max_posts]
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
            username = profile_info.get("username") or self._extract_username_from_url(profile_url)
            if not username:
                raise ValueError("Nao foi possivel determinar username do perfil para persistencia.")

            # Verificar se perfil já existe
            existing = db.query(Profile).filter(
                Profile.instagram_username == username
            ).first()

            if existing:
                # Atualizar perfil existente
                existing.bio = profile_info.get("bio")
                existing.is_private = profile_info.get("is_private", False)
                existing.follower_count = profile_info.get("follower_count")
                existing.following_count = profile_info.get("following_count")
                existing.post_count = profile_info.get("post_count")
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
                    following_count=profile_info.get("following_count"),
                    post_count=profile_info.get("post_count"),
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
                        posted_at=self._coerce_posted_at_datetime(post_data.get("posted_at")),
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
