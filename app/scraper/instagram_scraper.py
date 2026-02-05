"""
Scraper principal do Instagram.
Coordena o fluxo completo de raspagem: navegação, extração, processamento.
"""

import logging
import asyncio
import random
import re
import json
import html as html_lib
from urllib.parse import urlparse
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone, timedelta

from app.scraper.browserless_client import BrowserlessClient
from app.scraper.browser_use_agent import browser_use_agent
from app.scraper.ai_extractor import AIExtractor
from app.models import Profile, Post, Interaction, InteractionType
from app.database import SessionLocal
from sqlalchemy.orm import Session
from sqlalchemy import func, or_

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

    def _extract_profile_info_from_html(
        self,
        html_content: Optional[str],
        username_hint: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Extração determinística de dados de perfil a partir do HTML do Instagram.
        """
        if not html_content:
            return {}

        extracted: Dict[str, Any] = {}
        text = html_content

        def _bool_from_match(pattern: str) -> Optional[bool]:
            match = re.search(pattern, text)
            if not match:
                return None
            return match.group(1).lower() == "true"

        def _int_from_match(pattern: str) -> Optional[int]:
            match = re.search(pattern, text)
            if not match:
                return None
            try:
                return int(match.group(1))
            except ValueError:
                return None

        username_match = re.search(r'"username":"([^"]+)"', text)
        if username_match:
            extracted["username"] = username_match.group(1)
        elif username_hint:
            extracted["username"] = username_hint

        full_name_match = re.search(r'"full_name":"((?:\\.|[^"])*)"', text)
        if full_name_match:
            raw_full_name = full_name_match.group(1)
            try:
                extracted["full_name"] = json.loads(f'"{raw_full_name}"')
            except Exception:
                extracted["full_name"] = raw_full_name.replace('\\"', '"').replace("\\n", "\n")

        bio_match = re.search(r'"biography":"((?:\\.|[^"])*)"', text)
        if bio_match:
            raw_bio = bio_match.group(1)
            try:
                extracted["bio"] = json.loads(f'"{raw_bio}"')
            except Exception:
                extracted["bio"] = raw_bio.replace('\\"', '"').replace("\\n", "\n")

        extracted["is_private"] = _bool_from_match(r'"is_private":(true|false)')
        extracted["verified"] = _bool_from_match(r'"is_verified":(true|false)')
        extracted["follower_count"] = _int_from_match(r'"edge_followed_by":\{"count":(\d+)')
        extracted["following_count"] = _int_from_match(r'"edge_follow":\{"count":(\d+)')
        extracted["post_count"] = _int_from_match(r'"edge_owner_to_timeline_media":\{"count":(\d+)')

        # Fallback via meta description (útil quando o payload principal não vem completo)
        if any(extracted.get(k) is None for k in ("follower_count", "following_count", "post_count", "bio")):
            meta_match = re.search(
                r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']',
                text,
                flags=re.IGNORECASE,
            )
            if meta_match:
                og_desc = html_lib.unescape(meta_match.group(1))
                nums = [self._to_int_or_none(n) for n in re.findall(r"(\d[\d\.,]*)", og_desc)]
                nums = [n for n in nums if n is not None]
                if extracted.get("follower_count") is None and len(nums) >= 1:
                    extracted["follower_count"] = nums[0]
                if extracted.get("following_count") is None and len(nums) >= 2:
                    extracted["following_count"] = nums[1]
                if extracted.get("post_count") is None and len(nums) >= 3:
                    extracted["post_count"] = nums[2]

                if extracted.get("bio") is None:
                    bio_desc_match = re.search(r"on Instagram:\s*\"([^\"]+)\"", og_desc, flags=re.IGNORECASE)
                    if bio_desc_match:
                        extracted["bio"] = bio_desc_match.group(1).strip()

        if extracted.get("full_name") is None:
            og_title_match = re.search(
                r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
                text,
                flags=re.IGNORECASE,
            )
            if og_title_match:
                og_title = html_lib.unescape(og_title_match.group(1)).strip()
                full_name = re.sub(r"\s*\(@[^)]+\).*", "", og_title).strip()
                if full_name and full_name.lower() != "instagram":
                    extracted["full_name"] = full_name

        # limpeza
        if isinstance(extracted.get("bio"), str):
            extracted["bio"] = extracted["bio"].strip() or None
        if isinstance(extracted.get("full_name"), str):
            extracted["full_name"] = extracted["full_name"].strip() or None

        return extracted

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
        user_agent: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Fallback sem Browser Use:
        - extrai links /p/ do HTML do perfil
        - abre cada post diretamente e usa IA para extrair campos.
        """
        try:
            html = profile_html or await self.browserless.get_html(
                profile_url,
                cookies=cookies,
                user_agent=user_agent,
            )
            post_urls = self._extract_post_urls_from_html(html, max_posts=max_posts)
            if not post_urls:
                return []

            recovered: List[Dict[str, Any]] = []
            for post_url in post_urls[:max_posts]:
                screenshot_base64: Optional[str] = None
                post_html: Optional[str] = None
                try:
                    screenshot_base64 = await self.browserless.screenshot(
                        post_url,
                        cookies=cookies,
                        user_agent=user_agent,
                    )
                except Exception as exc:
                    logger.warning("⚠️ Falha ao capturar screenshot do post %s: %s", post_url, exc)
                try:
                    post_html = await self.browserless.get_html(
                        post_url,
                        cookies=cookies,
                        user_agent=user_agent,
                    )
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

    def _relative_time_to_hours(self, text: Optional[str]) -> Optional[float]:
        """
        Converte texto relativo (ex: "3 h", "2d", "1 sem") para horas.
        Retorna None quando nao consegue interpretar.
        """
        if text is None:
            return None

        cleaned = str(text).strip().lower()
        if not cleaned:
            return None

        cleaned = cleaned.replace("\u2022", " ").replace("\u00b7", " ")
        cleaned = re.sub(r"\b(editado|editada|edited)\b", "", cleaned)
        cleaned = re.sub(r"\bago\b", "", cleaned)
        cleaned = re.sub(r"\bh[a\u00e1]\b", "", cleaned)
        cleaned = cleaned.strip()

        if cleaned in {"now", "just now", "agora", "agora mesmo"}:
            return 0.0
        if cleaned in {"today", "hoje"}:
            return 0.0
        if cleaned in {"yesterday", "ontem"}:
            return 24.0

        patterns = [
            (r"(\d+(?:[.,]\d+)?)\s*(?:s|seg|segs|segundo|segundos|sec|secs|second|seconds)\b", 1 / 3600),
            (r"(\d+(?:[.,]\d+)?)\s*(?:m|min|mins|minuto|minutos)\b", 1 / 60),
            (r"(\d+(?:[.,]\d+)?)\s*(?:h|hr|hrs|hour|hours|hora|horas)\b", 1),
            (r"(\d+(?:[.,]\d+)?)\s*(?:d|day|days|dia|dias)\b", 24),
            (r"(\d+(?:[.,]\d+)?)\s*(?:w|wk|wks|week|weeks|sem|semana|semanas)\b", 24 * 7),
            (r"(\d+(?:[.,]\d+)?)\s*(?:mo|month|months|mes|m[e\u00ea]s|meses)\b", 24 * 30),
            (r"(\d+(?:[.,]\d+)?)\s*(?:y|yr|year|years|ano|anos)\b", 24 * 365),
        ]

        for pattern, hour_multiplier in patterns:
            match = re.search(pattern, cleaned)
            if not match:
                continue
            value = match.group(1).replace(",", ".")
            try:
                return float(value) * hour_multiplier
            except ValueError:
                return None

        return None

    def _is_recent_post(self, posted_at: Any, recent_days: int = 1) -> bool:
        """
        Determina se o post é recente baseado no texto/valor retornado pelo scraper.
        """
        if posted_at is None:
            return False

        now = datetime.now(timezone.utc)
        limit_hours = max(1, int(recent_days)) * 24

        if isinstance(posted_at, datetime):
            post_dt = posted_at if posted_at.tzinfo else posted_at.replace(tzinfo=timezone.utc)
            return (now - post_dt).total_seconds() <= limit_hours * 3600

        text = str(posted_at).strip().lower()
        if not text:
            return False

        if text in {"now", "just now", "agora", "agora mesmo"}:
            return True

        iso_candidate = text.replace("z", "+00:00")
        try:
            parsed = datetime.fromisoformat(iso_candidate)
            parsed = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            return (now - parsed).total_seconds() <= limit_hours * 3600
        except Exception:
            pass

        if text in {"today", "hoje"}:
            return True
        if text in {"yesterday", "ontem"}:
            return limit_hours >= 48

        hours = self._relative_time_to_hours(text)
        if hours is not None:
            return hours <= limit_hours

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
        user_agent: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Captura screenshot + HTML e aplica IA para extrair dados do perfil curtidor.
        """
        username = self._extract_username_from_url(user_url)
        try:
            screenshot = await self.browserless.screenshot(
                user_url,
                cookies=cookies,
                user_agent=user_agent,
            )
            html = await self.browserless.get_html(
                user_url,
                cookies=cookies,
                user_agent=user_agent,
            )
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
        session_username: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Raspa um perfil completo do Instagram.
        """
        try:
            logger.info("Iniciando scraping completo do perfil: %s", profile_url)

            if not profile_url.startswith("http"):
                profile_url = f"https://instagram.com/{profile_url}"
            if not profile_url.endswith("/"):
                profile_url = f"{profile_url}/"

            username = self._extract_username_from_url(profile_url)
            storage_state = (
                await browser_use_agent.ensure_instagram_session(
                    db,
                    instagram_username=session_username,
                )
                if db
                else None
            )
            if session_username and not storage_state:
                raise RuntimeError(
                    f"Sessao Instagram '@{session_username}' nao encontrada ou invalida."
                )
            cookies = browser_use_agent.get_cookies(storage_state)
            user_agent = browser_use_agent.get_user_agent(storage_state)

            profile_result = await self.scrape_profile_info(
                profile_url=profile_url,
                db=db,
                save_to_db=True,
                cache_ttl_days=0,
                session_username=session_username,
            )

            posts_data = await self._scrape_posts(
                profile_url=profile_url,
                max_posts=max_posts,
                cookies=cookies,
                storage_state=storage_state,
                user_agent=user_agent,
            )

            all_interactions: List[Dict[str, Any]] = []
            for post_data in posts_data:
                post_url = post_data.get("post_url")
                if not post_url:
                    continue
                interactions = await self._scrape_post_interactions(
                    post_url=post_url,
                    post_data=post_data,
                    cookies=cookies,
                    user_agent=user_agent,
                )
                for interaction in interactions:
                    interaction["_post_url"] = post_url
                all_interactions.extend(interactions)

            if db:
                profile_db = await self._save_profile(db, profile_url, profile_result)
                await self._save_posts_and_interactions(db, profile_db.id, posts_data, all_interactions)

            return {
                "status": "success",
                "flow": "default",
                "profile": {
                    "username": profile_result.get("username") or username,
                    "full_name": profile_result.get("full_name"),
                    "profile_url": profile_url,
                    "bio": profile_result.get("bio"),
                    "is_private": bool(profile_result.get("is_private", False)),
                    "follower_count": self._to_int_or_none(profile_result.get("follower_count")),
                    "verified": bool(profile_result.get("verified", False)),
                },
                "posts": posts_data,
                "interactions": all_interactions,
                "summary": {
                    "total_posts": len(posts_data),
                    "total_interactions": len(all_interactions),
                    "scraped_at": datetime.utcnow().isoformat(),
                },
            }
        except Exception as e:
            logger.exception("Erro ao raspar perfil completo %s: %s", profile_url, e)
            raise

    async def scrape_profile_info(
        self,
        profile_url: str,
        db: Optional[Session] = None,
        save_to_db: bool = True,
        cache_ttl_days: int = 0,
        session_username: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Raspa somente os dados do perfil (sem posts/interacoes).
        Reutiliza sessao autenticada do Instagram quando disponivel.
        """
        try:
            if not profile_url.startswith("http"):
                profile_url = f"https://instagram.com/{profile_url}"
            if not profile_url.endswith("/"):
                profile_url = f"{profile_url}/"

            username_fallback = self._extract_username_from_url(profile_url).strip().lstrip("@").lower()

            if db and cache_ttl_days > 0:
                ttl_cutoff = datetime.utcnow() - timedelta(days=cache_ttl_days)
                cached = db.query(Profile).filter(
                    (Profile.instagram_url == profile_url)
                    | (func.lower(Profile.instagram_username) == username_fallback)
                ).first()
                if cached and cached.last_scraped_at and cached.last_scraped_at >= ttl_cutoff:
                    logger.info(
                        "Perfil %s retornado do cache (TTL %s dias).",
                        cached.instagram_username,
                        cache_ttl_days,
                    )
                    return {
                        "username": cached.instagram_username,
                        "full_name": cached.full_name,
                        "profile_url": cached.instagram_url,
                        "bio": cached.bio,
                        "is_private": bool(cached.is_private),
                        "follower_count": cached.follower_count,
                        "following_count": cached.following_count,
                        "post_count": cached.post_count,
                        "verified": bool(cached.verified),
                        "confidence": 1.0,
                        "profile_id": cached.id,
                        "last_scraped_at": cached.last_scraped_at,
                        "extracted_at": datetime.utcnow(),
                    }

            storage_state = (
                await browser_use_agent.ensure_instagram_session(
                    db,
                    instagram_username=session_username,
                )
                if db
                else None
            )
            if session_username and not storage_state:
                raise RuntimeError(
                    f"Sessao Instagram '@{session_username}' nao encontrada ou invalida."
                )
            cookies = browser_use_agent.get_cookies(storage_state)
            user_agent = browser_use_agent.get_user_agent(storage_state)

            profile_info: Dict[str, Any] = {}
            browser_use_result: Dict[str, Any] = {}

            try:
                browser_use_result = await browser_use_agent.scrape_profile_basic_info(
                    profile_url=profile_url,
                    storage_state=storage_state,
                )
                if isinstance(browser_use_result, dict) and not browser_use_result.get("error"):
                    profile_info.update(browser_use_result)
            except Exception as exc:
                logger.warning("Browser Use nao conseguiu extrair perfil %s: %s", profile_url, exc)

            profile_html: Optional[str] = None
            profile_screenshot: Optional[str] = None
            html_error: Optional[str] = None
            screenshot_error: Optional[str] = None

            html_info: Dict[str, Any] = {}
            need_more_data = not any(
                profile_info.get(key) is not None
                for key in ("bio", "follower_count", "following_count", "post_count")
            )

            if need_more_data:
                try:
                    profile_html = await self.browserless.get_html(
                        profile_url,
                        cookies=cookies,
                        user_agent=user_agent,
                    )
                    html_info = self._extract_profile_info_from_html(profile_html, username_hint=username_fallback)
                    for key, value in html_info.items():
                        if profile_info.get(key) is None and value is not None:
                            profile_info[key] = value
                except Exception as exc:
                    html_error = str(exc)
                    logger.warning("Falha ao obter/parsing HTML do perfil %s: %s", profile_url, exc)

            still_poor = not any(
                profile_info.get(key) is not None
                for key in ("bio", "follower_count", "following_count", "post_count")
            )
            if still_poor:
                try:
                    profile_screenshot = await self.browserless.screenshot(
                        profile_url,
                        cookies=cookies,
                        user_agent=user_agent,
                    )
                except Exception as exc:
                    screenshot_error = str(exc)
                    logger.warning("Falha ao capturar screenshot do perfil %s: %s", profile_url, exc)

                if not profile_screenshot and not profile_html:
                    details = " ; ".join(
                        item
                        for item in [
                            screenshot_error,
                            html_error,
                            browser_use_result.get("error") if isinstance(browser_use_result, dict) else None,
                        ]
                        if item
                    )
                    raise RuntimeError(
                        f"Falha ao obter dados do perfil para {profile_url}. {details}".strip()
                    )

                ai_info = await self.ai_extractor.extract_profile_info(
                    screenshot_base64=profile_screenshot,
                    html_content=profile_html,
                )
                if isinstance(ai_info, dict):
                    for key, value in ai_info.items():
                        if profile_info.get(key) is None and value is not None:
                            profile_info[key] = value

            normalized = {
                "username": (profile_info.get("username") or username_fallback).strip().lstrip("@").lower(),
                "full_name": (str(profile_info.get("full_name")).strip() if profile_info.get("full_name") is not None else None),
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
            logger.exception("Erro ao extrair dados do perfil %s: %s", profile_url, e)
            raise

    async def scrape_recent_posts_like_users(
        self,
        profile_url: str,
        max_posts: int = 3,
        recent_days: int = 1,
        max_like_users_per_post: int = 30,
        collect_like_user_profiles: bool = True,
        db: Optional[Session] = None,
        session_username: Optional[str] = None,
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
            storage_state = (
                await browser_use_agent.ensure_instagram_session(
                    db,
                    instagram_username=session_username,
                )
                if db
                else None
            )
            if session_username and not storage_state:
                raise RuntimeError(
                    f"Sessao Instagram '@{session_username}' nao encontrada ou invalida."
                )
            cookies = browser_use_agent.get_cookies(storage_state)
            user_agent = browser_use_agent.get_user_agent(storage_state)

            posts_data = await self._scrape_posts(
                profile_url=profile_url,
                max_posts=max_posts,
                cookies=cookies,
                storage_state=storage_state,
                user_agent=user_agent,
            )

            extracted_posts: List[Dict[str, Any]] = []
            total_like_users = 0
            total_recent_posts = 0
            all_interactions: List[Dict[str, Any]] = []

            for post in posts_data[:max_posts]:
                post_url = post.get("post_url")
                if not post_url:
                    continue

                posted_at = post.get("posted_at")
                is_recent = self._is_recent_post(posted_at, recent_days=recent_days)
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
                    for user_url in post_payload["like_users"]:
                        all_interactions.append({
                            "type": "like",
                            "user_url": user_url,
                            "user_username": self._extract_username_from_url(user_url),
                            "_post_url": post_url,
                        })
                else:
                    post_payload["like_users"] = []

                total_like_users += len(post_payload["like_users"])
                if is_recent:
                    try:
                        comment_interactions = await self._scrape_post_interactions(
                            post_url=post_url,
                            post_data=post,
                            cookies=cookies,
                            user_agent=user_agent,
                            recent_days=recent_days,
                        )
                        comment_interactions = [
                            item for item in comment_interactions
                            if item.get("type") == "comment"
                        ]
                        for interaction in comment_interactions:
                            interaction["_post_url"] = post_url
                        all_interactions.extend(comment_interactions)
                    except Exception as exc:
                        logger.warning("Falha ao extrair comentarios do post %s: %s", post_url, exc)

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
                await self._save_posts_and_interactions(db, profile_db.id, extracted_posts, all_interactions)

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
        user_agent: Optional[str] = None,
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
                    user_agent=user_agent,
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
                user_agent=user_agent,
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
        user_agent: Optional[str] = None,
        recent_days: Optional[int] = None,
        max_comment_scrolls: int = 6,
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

            interactions: List[Dict[str, Any]] = []
            seen_comment_keys: set[str] = set()
            limit_hours = max(1, int(recent_days)) * 24 if recent_days is not None else None
            max_scrolls = max(1, int(max_comment_scrolls)) if recent_days is not None else 1

            scroll_script = """
(() => {
  const keywords = [
    "view more comments",
    "load more comments",
    "more comments",
    "ver mais comentarios",
    "ver comentarios anteriores",
    "carregar mais comentarios",
    "comentarios anteriores"
  ];
  const nodes = Array.from(document.querySelectorAll("button, a, span, div"));
  for (const node of nodes) {
    const raw = (node.innerText || "").trim();
    if (!raw) continue;
    const text = raw.toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "");
    if (keywords.some((k) => text.includes(k))) {
      node.click();
    }
  }
  const before = window.scrollY || 0;
  window.scrollBy(0, Math.floor(window.innerHeight * 0.9));
  return { before, after: window.scrollY || 0, height: document.body.scrollHeight || 0 };
})();
"""

            for attempt in range(max_scrolls):
                comments_screenshot = await self.browserless.screenshot(
                    post_url,
                    cookies=cookies,
                    user_agent=user_agent,
                )

                comments = await self.ai_extractor.extract_comments(
                    screenshot_base64=comments_screenshot,
                )

                has_within_window = False
                for comment in comments:
                    user_username = str(comment.get("user_username") or "").strip()
                    user_url = str(comment.get("user_url") or "").strip()
                    if not user_url and user_username:
                        user_url = f"https://www.instagram.com/{user_username.lstrip('@').strip()}/"

                    comment_posted_at = comment.get("comment_posted_at")
                    hours = self._relative_time_to_hours(comment_posted_at)
                    within_window = True
                    if limit_hours is not None and hours is not None and hours > limit_hours:
                        within_window = False
                    if within_window:
                        has_within_window = True
                    if limit_hours is not None and not within_window:
                        continue

                    comment_text = comment.get("comment_text")
                    comment_key = f"{user_url or user_username}|{comment_text}|{comment_posted_at}"
                    if comment_key in seen_comment_keys:
                        continue
                    seen_comment_keys.add(comment_key)

                    interactions.append({
                        "type": "comment",
                        "user_url": user_url or None,
                        "user_username": user_username or None,
                        "comment_text": comment_text,
                        "comment_likes": comment.get("comment_likes", 0),
                        "comment_replies": comment.get("comment_replies", 0),
                        "comment_posted_at": comment_posted_at,
                    })

                if recent_days is None:
                    break
                if not has_within_window:
                    break
                if attempt >= max_scrolls - 1:
                    break

                await self.browserless.execute_script(
                    post_url,
                    scroll_script,
                    cookies=cookies,
                    user_agent=user_agent,
                )
                await asyncio.sleep(self._get_random_delay(1.5, 3.5))

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
        Salva informacoes do perfil no banco de dados.
        """
        try:
            username = profile_info.get("username") or self._extract_username_from_url(profile_url)
            if not username:
                raise ValueError("Nao foi possivel determinar username do perfil para persistencia.")

            username = str(username).strip().lstrip("@").lower()
            normalized_profile_url = profile_url.strip()
            if not normalized_profile_url.endswith("/"):
                normalized_profile_url = f"{normalized_profile_url}/"

            existing = db.query(Profile).filter(
                (func.lower(Profile.instagram_username) == username)
                | (Profile.instagram_url == normalized_profile_url)
            ).first()

            full_name = profile_info.get("full_name")

            if existing:
                existing.instagram_username = username
                existing.instagram_url = normalized_profile_url
                existing.full_name = full_name
                existing.bio = profile_info.get("bio")
                existing.is_private = profile_info.get("is_private", False)
                existing.follower_count = profile_info.get("follower_count")
                existing.following_count = profile_info.get("following_count")
                existing.post_count = profile_info.get("post_count")
                existing.verified = profile_info.get("verified", False)
                existing.last_scraped_at = datetime.utcnow()
                db.commit()
                logger.info("Perfil atualizado: %s", username)
                return existing

            profile = Profile(
                instagram_username=username,
                full_name=full_name,
                instagram_url=normalized_profile_url,
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
            logger.info("Novo perfil salvo: %s", username)
            return profile

        except Exception as e:
            logger.error("Erro ao salvar perfil: %s", e)
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
                    interaction_post_url = interaction_data.get("_post_url") or post_url
                    if not interaction_post_url:
                        continue
                    if interaction_post_url != post_url:
                        continue

                    interaction_type_raw = interaction_data.get("type")
                    if isinstance(interaction_type_raw, InteractionType):
                        interaction_type = interaction_type_raw
                    else:
                        try:
                            interaction_type = InteractionType(str(interaction_type_raw).strip().lower())
                        except Exception:
                            continue

                    user_url = str(interaction_data.get("user_url") or "").strip()
                    if not user_url:
                        continue

                    user_username = str(
                        interaction_data.get("user_username")
                        or self._extract_username_from_url(user_url)
                        or user_url
                    ).strip()

                    existing_interaction = (
                        db.query(Interaction)
                        .filter(Interaction.user_url == user_url)
                        .filter(Interaction.interaction_type == interaction_type)
                        .filter(
                            or_(
                                Interaction.post_url == interaction_post_url,
                                (Interaction.post_url.is_(None)) & (Interaction.post_id == post_id),
                            )
                        )
                        .first()
                    )

                    if not existing_interaction:
                        interaction = Interaction(
                            post_id=post_id,
                            profile_id=profile_id,
                            post_url=interaction_post_url,
                            user_username=user_username,
                            user_url=user_url,
                            interaction_type=interaction_type,
                            comment_text=(interaction_data.get("comment_text") if interaction_type == InteractionType.COMMENT else None),
                            comment_likes=(interaction_data.get("comment_likes", 0) if interaction_type == InteractionType.COMMENT else None),
                            comment_replies=(interaction_data.get("comment_replies", 0) if interaction_type == InteractionType.COMMENT else None),
                            comment_posted_at=(interaction_data.get("comment_posted_at") if interaction_type == InteractionType.COMMENT else None),
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
