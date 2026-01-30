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
import websockets
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
        # Respect LOG_LEVEL from .env for browser_use logs.
        level = getattr(logging, settings.log_level, logging.INFO)
        for name in ("browser_use", "browser_use.Agent", "browser_use.BrowserSession", "browser_use.tools"):
            log = logging.getLogger(name)
            log.setLevel(level)
            log.propagate = True
        self._patch_websocket_compression()

    _ws_patched = False

    @classmethod
    def _patch_websocket_compression(cls) -> None:
        """
        Força compression=None no websockets.connect para evitar erro 1002 (RSV bits).
        """
        if cls._ws_patched:
            return
        original_connect = websockets.connect

        async def _connect(*args, **kwargs):
            kwargs.setdefault("compression", None)
            return await original_connect(*args, **kwargs)

        websockets.connect = _connect  # type: ignore[assignment]
        cls._ws_patched = True

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

    def _build_browserless_http_url(self) -> str:
        host = (self.browserless_host or "").rstrip("/")
        if not host.startswith("http"):
            host = f"http://{host}"
        return host

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

    async def _create_browserless_session(self) -> Dict[str, Any]:
        if not settings.browserless_session_enabled:
            return {}

        host = self._build_browserless_http_url()
        url = f"{host}/session?token={self.browserless_token}"
        payload = {
            "ttl": settings.browserless_session_ttl_ms,
            "stealth": settings.browserless_session_stealth,
            "headless": settings.browserless_session_headless,
        }

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code >= 400:
                raise RuntimeError(f"Erro ao criar sessao Browserless: {resp.status_code} {resp.text}")
            return resp.json()

    async def _stop_browserless_session(self, stop_url: str) -> None:
        if not stop_url:
            return
        url = stop_url
        if "token=" not in url and self.browserless_token:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}token={self.browserless_token}"
        url = f"{url}&force=true" if "force=" not in url else url

        host = self._build_browserless_http_url()
        if url.startswith("/"):
            url = f"{host}{url}"

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                await client.delete(url)
        except Exception as exc:
            logger.warning("Falha ao encerrar sessao Browserless: %s", exc)

    async def _maybe_await(self, value):
        if asyncio.iscoroutine(value):
            return await value
        return value

    async def _safe_stop_session(self, session: BrowserSession) -> None:
        stop_fn = getattr(session, "stop", None)
        if stop_fn is None:
            return
        try:
            result = stop_fn()
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:
            logger.warning("Erro ao encerrar sessao do browser: %s", exc)

    async def _detach_browser_session(self, session: BrowserSession) -> None:
        disconnect_fn = getattr(session, "disconnect", None)
        if callable(disconnect_fn):
            try:
                result = disconnect_fn()
                if asyncio.iscoroutine(result):
                    await result
                return
            except Exception as exc:
                logger.warning("Erro ao desconectar sessao do browser: %s", exc)
        await self._safe_stop_session(session)

    def _create_browser_session(self, cdp_url: str, storage_state: Optional[Dict[str, Any]] = None) -> BrowserSession:
        """
        Cria BrowserSession tentando desativar compress??o do WebSocket quando suportado.
        """
        try:
            session = BrowserSession(
                cdp_url=cdp_url,
                storage_state=storage_state,
                ws_connect_kwargs={"compression": None},
            )
        except TypeError:
            session = BrowserSession(cdp_url=cdp_url, storage_state=storage_state)

        keep_alive_setters = (
            getattr(session, "set_keep_alive", None),
            getattr(session, "set_keepalive", None),
        )
        for setter in keep_alive_setters:
            if callable(setter):
                try:
                    setter(True)
                except Exception:
                    pass
        if hasattr(session, "keep_alive"):
            try:
                session.keep_alive = True
            except Exception:
                pass
        return session

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

    def _get_browserless_session_info(self, storage_state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if not storage_state:
            return {}
        info = storage_state.get("_browserless_session")
        return info if isinstance(info, dict) else {}

    def get_cookies(self, storage_state: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Retorna lista de cookies a partir de um storage_state."""
        if not storage_state:
            return []
        return self._extract_cookies(storage_state)

    def _build_cookie_jar(self, cookies: List[Dict[str, Any]]) -> httpx.Cookies:
        jar = httpx.Cookies()
        for cookie in cookies:
            name = cookie.get("name")
            value = cookie.get("value")
            if not name or value is None:
                continue
            domain = (cookie.get("domain") or "instagram.com").lstrip(".")
            path = cookie.get("path") or "/"
            jar.set(name, value, domain=domain, path=path)
        return jar

    async def _is_session_valid(self, storage_state: Dict[str, Any]) -> bool:
        """
        Verifica se o storage_state ainda representa uma sessao autenticada.
        """
        cookies = self._extract_cookies(storage_state)
        if not cookies:
            return False

        jar = self._build_cookie_jar(cookies)
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
        }
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                resp = await client.get("https://www.instagram.com/accounts/edit/", cookies=jar, headers=headers)
        except Exception:
            return False

        if resp.url and "login" in str(resp.url):
            return False
        text = (resp.text or "").lower()
        if "login" in text and ("password" in text or "senha" in text):
            return False

        return resp.status_code == 200

    def _should_retry_login_error(self, exc: Exception) -> bool:
        message = str(exc).lower()
        retry_markers = (
            "root cdp client not initialized",
            "failed to establish cdp connection",
            "connectionclosederror",
            "protocol error",
            "websocket",
            "navigation failed",
        )
        return any(marker in message for marker in retry_markers)

    async def _export_storage_state_with_retry(
        self,
        browser_session: BrowserSession,
        attempts: int = 2,
    ) -> Dict[str, Any]:
        last_error: Optional[BaseException] = None
        for attempt in range(1, max(1, attempts) + 1):
            try:
                return await self._maybe_await(browser_session.export_storage_state())
            except Exception as exc:
                last_error = exc
                if attempt == attempts or not self._should_retry_login_error(exc):
                    raise
                await asyncio.sleep(1)
        if last_error:
            raise last_error
        return {}

    async def ensure_instagram_session(self, db: Session) -> Optional[Dict[str, Any]]:
        """
        Garante uma sess??o autenticada do Instagram salva no banco.
        Retorna storage_state quando dispon??vel.
        """
        if db is None:
            logger.warning("?????? Sess??o de banco n??o fornecida; login n??o ser?? persistido.")
            return None

        if not settings.instagram_username or not settings.instagram_password:
            logger.warning("?????? INSTAGRAM_USERNAME/PASSWORD n??o configurados; login n??o ser?? feito.")
            return None
        existing = self._get_latest_session(db)
        if existing and existing.storage_state:
            if await self._is_session_valid(existing.storage_state):
                self._touch_session(db, existing)
                logger.info("Sessao do Instagram reutilizada do banco.")
                return existing.storage_state

            if settings.browserless_session_enabled:
                session_info = self._get_browserless_session_info(existing.storage_state)
                stop_url = session_info.get("stop")
                if stop_url:
                    await self._stop_browserless_session(stop_url)

            existing.is_active = False
            db.commit()
            logger.info("Sessao do Instagram expirada; realizando novo login.")

        last_error = None
        for attempt in range(1, settings.browser_use_max_retries + 1):
            try:
                return await self._login_and_save_session(db)
            except Exception as exc:
                last_error = exc
                if attempt >= settings.browser_use_max_retries or not self._should_retry_login_error(exc):
                    break
                delay = settings.browser_use_retry_backoff * attempt
                logger.warning(
                    "Login falhou (tentativa %s/%s): %s. Retentando em %ss...",
                    attempt,
                    settings.browser_use_max_retries,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)

        if last_error:
            raise last_error
        return None

    async def _login_and_save_session(self, db: Session) -> Dict[str, Any]:
        logger.info("Iniciando login no Instagram via Browser Use...")

        session_info: Dict[str, Any] = {}
        connect_url: Optional[str] = None
        stop_url: Optional[str] = None
        if settings.browserless_session_enabled:
            session_info = await self._create_browserless_session()
            connect_url = session_info.get("connect")
            stop_url = session_info.get("stop")

        cdp_url = connect_url or await self._resolve_browserless_cdp_url()
        browser_session = self._create_browser_session(cdp_url)
        llm = ChatOpenAI(model=self.model, api_key=self.api_key)

        login_task = f"""
        Voce esta em um navegador controlado por IA.
        Acesse https://www.instagram.com/accounts/login/.

        Passos:
        1) Se aparecer um modal de cookies, clique em "Allow all cookies" (ou equivalente).
        2) Preencha o campo de usuario com: {settings.instagram_username}
        3) Preencha o campo de senha com: {settings.instagram_password}
        4) Clique em "Log in"/"Entrar".
        5) Se aparecer a tela "Save your login info?", clique em "Save info".
        6) Aguarde o feed inicial carregar e confirme que o login foi bem sucedido.
        7) Se houver challenge/2FA, pare e reporte erro.

        Importante:
        - Use apenas a aba atual (nao abrir nova aba).
        - Aguarde o DOM carregar; se ficar vazio, aguarde alguns segundos e recarregue uma vez.
        - Nao clique em "Forgot password?"; se nao encontrar um botao claro de login, pressione Enter no campo de senha.

        Ao final, confirme sucesso com um texto curto: "LOGIN_OK".
        """

        agent = Agent(
            task=login_task,
            llm=llm,
            browser_session=browser_session,
        )
        login_ok = False
        try:
            history = await agent.run()
            if not history.is_done() or not history.is_successful():
                raise RuntimeError("Login nao foi concluido com sucesso.")

            logger.info("Exportando storage state do navegador...")
            try:
                storage_state = await self._export_storage_state_with_retry(browser_session)
            except Exception as exc:
                fallback_state = getattr(browser_session, "storage_state", None)
                if isinstance(fallback_state, dict) and self._extract_cookies(fallback_state):
                    storage_state = fallback_state
                    logger.warning("Storage state export falhou, usando fallback em memoria: %s", exc)
                else:
                    logger.exception("Falha ao exportar storage state: %s", exc)
                    raise

            if not storage_state or not self._extract_cookies(storage_state):
                raise RuntimeError("Storage state nao possui cookies do Instagram.")

            if session_info:
                storage_state["_browserless_session"] = session_info

            session = InstagramSession(
                instagram_username=settings.instagram_username,
                storage_state=storage_state,
                last_used_at=datetime.utcnow(),
            )
            db.add(session)
            db.commit()
            db.refresh(session)

            login_ok = True
            logger.info("Sessao do Instagram salva no banco.")
            return storage_state

        finally:
            if login_ok and settings.browserless_session_enabled:
                await self._detach_browser_session(browser_session)
            else:
                await self._safe_stop_session(browser_session)
            if not login_ok and stop_url:
                await self._stop_browserless_session(stop_url)


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

            browser_session = self._create_browser_session(cdp_url)
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
