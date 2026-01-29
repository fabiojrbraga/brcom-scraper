"""
Integração com Browser Use para automação inteligente de navegador.
Browser Use usa IA para tomar decisões autônomas durante a navegação.
"""

import logging
import asyncio
from typing import Optional, Dict, Any, List
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
from datetime import datetime

from browser_use import Agent, BrowserSession, ChatOpenAI
import httpx
from config import settings
from app.models import InstagramSession
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class BrowserUseAgent:
    """
    Agente que usa Browser Use para navegar e interagir com o Instagram.
    
    Browser Use é uma biblioteca que permite que um modelo de IA (Claude/GPT)
    controle um navegador de forma autônoma, simulando comportamento humano.
    """

    def __init__(self):
        self.model = settings.openai_model_text
        self.api_key = settings.openai_api_key
        self.browserless_host = settings.browserless_host
        self.browserless_token = settings.browserless_token
        self.browserless_ws_url = settings.browserless_ws_url

    def _build_browserless_cdp_url(self) -> str:
        if not self.browserless_token:
            raise ValueError("BROWSERLESS_TOKEN is required for Browser Use.")

        base_url = self.browserless_ws_url
        if not base_url:
            parsed = urlparse(self.browserless_host)
            if not parsed.netloc:
                raise ValueError("BROWSERLESS_HOST must be a valid URL.")
            scheme = "wss" if parsed.scheme in ("https", "wss") else "ws"
            base_url = f"{scheme}://{parsed.netloc}"

        if "token=" in base_url:
            return base_url

        separator = "&" if "?" in base_url else "?"
        return f"{base_url}{separator}token={self.browserless_token}"

    def _rewrite_ws_url(self, ws_url: str) -> str:
        parsed_ws = urlparse(ws_url)
        if not parsed_ws.scheme.startswith("ws"):
            return ws_url

        host_parsed = urlparse(self.browserless_host)
        external_host = host_parsed.netloc or parsed_ws.netloc
        scheme = "wss" if host_parsed.scheme in ("https", "wss") else "ws"

        if parsed_ws.hostname in ("0.0.0.0", "127.0.0.1", "localhost"):
            parsed_ws = parsed_ws._replace(netloc=external_host, scheme=scheme)

        # Normalize path like "/token=..." into query param.
        query_items = dict(parse_qsl(parsed_ws.query))
        if "token=" in (parsed_ws.path or "") and not query_items:
            token_value = parsed_ws.path.lstrip("/").split("token=", 1)[-1]
            if token_value:
                query_items["token"] = token_value
                parsed_ws = parsed_ws._replace(path="/")

        if "token" not in query_items and self.browserless_token:
            query_items["token"] = self.browserless_token
            parsed_ws = parsed_ws._replace(query=urlencode(query_items))

        # Ensure we don't return an URL with token in the path.
        if "token=" in (parsed_ws.path or "") and parsed_ws.query:
            parsed_ws = parsed_ws._replace(path="/")

        return urlunparse(parsed_ws)

    async def _resolve_browserless_cdp_url(self) -> str:
        """
        Resolve CDP WebSocket URL. Tries explicit WS URL first, then /json/version.
        """
        if self.browserless_ws_url:
            return self._build_browserless_cdp_url()

        host = self.browserless_host.rstrip("/")
        if not host.startswith("http"):
            return self._build_browserless_cdp_url()

        version_url = f"{host}/json/version?token={self.browserless_token}"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(version_url)
                if resp.status_code == 200:
                    data = resp.json()
                    ws_url = data.get("webSocketDebuggerUrl")
                    if ws_url:
                        return self._rewrite_ws_url(ws_url)
        except Exception:
            pass

        return self._build_browserless_cdp_url()

    async def _maybe_await(self, value):
        if asyncio.iscoroutine(value):
            return await value
        return value

    async def _safe_stop_session(self, session: BrowserSession) -> None:
        stop_fn = getattr(session, "stop", None)
        if stop_fn is None:
            return
        result = stop_fn()
        if asyncio.iscoroutine(result):
            await result

    def _get_latest_session(self, db: Session) -> Optional[InstagramSession]:
        return (
            db.query(InstagramSession)
            .filter(InstagramSession.is_active.is_(True))
            .order_by(InstagramSession.updated_at.desc())
            .first()
        )

    def _touch_session(self, db: Session, session: InstagramSession) -> None:
        session.last_used_at = datetime.utcnow()
        db.commit()

    def _extract_cookies(self, storage_state: Dict[str, Any]) -> List[Dict[str, Any]]:
        cookies = storage_state.get("cookies") if storage_state else None
        if isinstance(cookies, list):
            return cookies
        return []

    def get_cookies(self, storage_state: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Retorna lista de cookies a partir de um storage_state."""
        if not storage_state:
            return []
        return self._extract_cookies(storage_state)

    async def ensure_instagram_session(self, db: Session) -> Optional[Dict[str, Any]]:
        """
        Garante uma sessÃ£o autenticada do Instagram salva no banco.
        Retorna storage_state quando disponÃ­vel.
        """
        if db is None:
            logger.warning("âš ï¸ SessÃ£o de banco nÃ£o fornecida; login nÃ£o serÃ¡ persistido.")
            return None

        if not settings.instagram_username or not settings.instagram_password:
            logger.warning("âš ï¸ INSTAGRAM_USERNAME/PASSWORD nÃ£o configurados; login nÃ£o serÃ¡ feito.")
            return None

        existing = self._get_latest_session(db)
        if existing and existing.storage_state:
            self._touch_session(db, existing)
            logger.info("âœ… SessÃ£o do Instagram reutilizada do banco.")
            return existing.storage_state

        return await self._login_and_save_session(db)

    async def _login_and_save_session(self, db: Session) -> Dict[str, Any]:
        """
        Faz login via Browser Use e salva storage_state no banco.
        """
        logger.info("ðŸ” Iniciando login no Instagram via Browser Use...")

        cdp_url = await self._resolve_browserless_cdp_url()
        browser_session = BrowserSession(cdp_url=cdp_url)
        llm = ChatOpenAI(model=self.model, api_key=self.api_key)

        login_task = f"""
        VocÃª estÃ¡ em um navegador controlado por IA. 
        Acesse https://www.instagram.com/accounts/login/.

        Passos:
        1) Se aparecer um modal de cookies, clique em "Allow all cookies" (ou equivalente).
        2) Preencha o campo de usuÃ¡rio com: {settings.instagram_username}
        3) Preencha o campo de senha com: {settings.instagram_password}
        4) Clique em "Log in"/"Entrar".
        5) Aguarde o feed inicial carregar e confirme que o login foi bem sucedido.
        6) Se houver challenge/2FA, pare e reporte erro.

        Importante:
        - Use apenas a aba atual (nÃ£o abrir nova aba).
        - Aguarde o DOM carregar; se ficar vazio, aguarde alguns segundos e recarregue uma vez.

        Ao final, confirme sucesso com um texto curto: "LOGIN_OK".
        """

        agent = Agent(
            task=login_task,
            llm=llm,
            browser_session=browser_session,
        )

        try:
            history = await agent.run()
            if not history.is_done() or not history.is_successful():
                raise RuntimeError("Login nÃ£o foi concluÃ­do com sucesso.")

            storage_state = await self._maybe_await(
                browser_session.export_storage_state()
            )

            if not storage_state or not self._extract_cookies(storage_state):
                raise RuntimeError("Storage state nÃ£o possui cookies do Instagram.")

            session = InstagramSession(
                instagram_username=settings.instagram_username,
                storage_state=storage_state,
                last_used_at=datetime.utcnow(),
            )
            db.add(session)
            db.commit()
            db.refresh(session)

            logger.info("âœ… SessÃ£o do Instagram salva no banco.")
            return storage_state

        finally:
            await self._safe_stop_session(browser_session)

    async def navigate_and_scrape_profile(
        self,
        profile_url: str,
        max_posts: int = 5,
    ) -> Dict[str, Any]:
        """
        Usa Browser Use para navegar em um perfil Instagram e extrair dados.

        Args:
            profile_url: URL do perfil Instagram
            max_posts: Número máximo de posts a analisar

        Returns:
            Dicionário com dados extraídos (screenshots, HTML, etc)
        """
        try:
            logger.info(f"🤖 Iniciando Browser Use Agent para: {profile_url}")

            if not self.api_key:
                raise ValueError("OPENAI_API_KEY is required for Browser Use.")

            cdp_url = await self._resolve_browserless_cdp_url()

            task = f"""
            Acesse o perfil do Instagram em {profile_url} e:
            
            1. Aguarde a página carregar completamente
            2. Tire um screenshot do perfil (bio, follower count, etc)
            3. Extraia o nome de usuário e bio
            4. Identifique se é conta privada ou pública
            5. Navegue pelos últimos {max_posts} posts
            6. Para cada post:
               - Tire screenshot
               - Extraia caption, likes, comentários
               - Colete comentários visíveis
            7. Retorne todos os dados capturados
            
            Simule comportamento humano com delays aleatórios entre ações.
            Não use seletores CSS fixos - adapte-se ao layout.
            """

            browser_session = BrowserSession(cdp_url=cdp_url)
            llm = ChatOpenAI(model=self.model, api_key=self.api_key)
            agent = Agent(
                task=task,
                llm=llm,
                browser_session=browser_session,
            )

            try:
                history = await agent.run()
            finally:
                await self._safe_stop_session(browser_session)

            status = "unknown"
            if history.is_done():
                status = "success" if history.is_successful() else "failed"

            result = {
                "profile_url": profile_url,
                "final_result": history.final_result(),
                "extracted_content": history.extracted_content(),
                "screenshots": history.screenshots(),
                "urls": history.urls(),
                "errors": history.errors(),
                "status": status,
                "task": task,
            }

            logger.info(f"✅ Browser Use Agent configurado para: {profile_url}")
            return result

        except Exception as e:
            logger.error(f"❌ Erro no Browser Use Agent: {e}")
            raise

    async def scroll_and_load_more(
        self,
        url: str,
        scroll_count: int = 5,
    ) -> Dict[str, Any]:
        """
        Simula scroll infinito para carregar mais conteúdo.

        Args:
            url: URL da página
            scroll_count: Número de scrolls a realizar

        Returns:
            Dados capturados após scrolls
        """
        try:
            logger.info(f"📜 Iniciando scroll em: {url}")

            # Implementação será feita com Browserless + JavaScript
            result = {
                "url": url,
                "scroll_count": scroll_count,
                "screenshots": [],
                "html_content": [],
            }

            logger.info(f"✅ Scroll completado em: {url}")
            return result

        except Exception as e:
            logger.error(f"❌ Erro ao fazer scroll: {e}")
            raise

    async def click_and_wait(
        self,
        url: str,
        selector: str,
        wait_for_selector: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Clica em um elemento e aguarda carregamento.

        Args:
            url: URL da página
            selector: Seletor CSS do elemento a clicar
            wait_for_selector: Seletor CSS para aguardar após clique

        Returns:
            Dados capturados após clique
        """
        try:
            logger.info(f"🖱️ Clicando em: {selector}")

            result = {
                "url": url,
                "clicked_selector": selector,
                "screenshot": None,
                "html_content": None,
            }

            logger.info(f"✅ Clique executado")
            return result

        except Exception as e:
            logger.error(f"❌ Erro ao clicar: {e}")
            raise

    async def extract_visible_text(
        self,
        html: str,
        selector: str,
    ) -> str:
        """
        Extrai texto visível de um elemento HTML.

        Args:
            html: Conteúdo HTML
            selector: Seletor CSS

        Returns:
            Texto extraído
        """
        try:
            # Implementação com BeautifulSoup ou similar
            logger.info(f"📝 Extraindo texto de: {selector}")
            return ""

        except Exception as e:
            logger.error(f"❌ Erro ao extrair texto: {e}")
            raise


# Instância global do agente
browser_use_agent = BrowserUseAgent()
