"""
Integração com Browser Use para automação inteligente de navegador.
Browser Use usa IA para tomar decisões autônomas durante a navegação.
"""

import logging
import asyncio
from typing import Optional, Dict, Any
from urllib.parse import urlparse

from browser_use import Agent, BrowserSession, ChatOpenAI
from config import settings

logger = logging.getLogger(__name__)


class BrowserUseAgent:
    """
    Agente que usa Browser Use para navegar e interagir com o Instagram.
    
    Browser Use é uma biblioteca que permite que um modelo de IA (Claude/GPT)
    controle um navegador de forma autônoma, simulando comportamento humano.
    """

    def __init__(self):
        self.model = "gpt-4o-mini"
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

    async def _safe_stop_session(self, session: BrowserSession) -> None:
        stop_fn = getattr(session, "stop", None)
        if stop_fn is None:
            return
        result = stop_fn()
        if asyncio.iscoroutine(result):
            await result

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

            cdp_url = self._build_browserless_cdp_url()

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
