"""
Integração com Browser Use para automação inteligente de navegador.
Browser Use usa IA para tomar decisões autônomas durante a navegação.
"""

import logging
import asyncio
import inspect
import json
import tempfile
from pathlib import Path
from typing import Optional, Dict, Any, List, Union
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
from datetime import datetime
from uuid import uuid4

from browser_use import Agent, BrowserSession, ChatOpenAI
import httpx
import websockets
from config import settings
from app.models import InstagramSession, InvestingSession
from sqlalchemy.orm import Session
from sqlalchemy import func

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
        self.ws_compression_mode = self._normalize_ws_compression_mode(
            getattr(settings, "browser_use_ws_compression", "auto")
        )
        # Respect LOG_LEVEL from .env for browser_use logs.
        level = getattr(logging, settings.log_level, logging.INFO)
        for name in ("browser_use", "browser_use.Agent", "browser_use.BrowserSession", "browser_use.tools"):
            log = logging.getLogger(name)
            log.setLevel(level)
            log.propagate = True
        self._patch_websocket_compression(self.ws_compression_mode)
        logger.info("Browser Use WebSocket compression mode: %s", self.ws_compression_mode)

    _ws_patched = False
    _ws_patch_mode = "auto"
    _ws_original_connect = None

    @classmethod
    def _normalize_ws_compression_mode(cls, mode: Optional[str]) -> str:
        normalized = (mode or "auto").strip().lower()
        if normalized not in {"auto", "none", "deflate"}:
            return "auto"
        return normalized

    @classmethod
    def _patch_websocket_compression(cls, mode: Optional[str] = None) -> None:
        """
        Ajusta websocket compression globalmente para compatibilidade com CDP/browserless.
        """
        normalized_mode = cls._normalize_ws_compression_mode(mode)

        if cls._ws_original_connect is None:
            cls._ws_original_connect = websockets.connect

        original_connect = cls._ws_original_connect
        if normalized_mode == "auto":
            if cls._ws_patched and original_connect is not None:
                websockets.connect = original_connect  # type: ignore[assignment]
                try:
                    import websockets.client as ws_client  # type: ignore
                    ws_client.connect = original_connect  # type: ignore[assignment]
                except Exception:
                    pass
                try:
                    import websockets.asyncio.client as ws_async_client  # type: ignore
                    ws_async_client.connect = original_connect  # type: ignore[assignment]
                except Exception:
                    pass
                try:
                    import cdp_use.client as cdp_client_module  # type: ignore
                    cdp_ws = getattr(cdp_client_module, "websockets", None)
                    if cdp_ws and hasattr(cdp_ws, "connect"):
                        cdp_ws.connect = original_connect  # type: ignore[assignment]
                except Exception:
                    pass
            cls._ws_patched = False
            cls._ws_patch_mode = "auto"
            return

        if cls._ws_patched and cls._ws_patch_mode == normalized_mode:
            return

        compression_value = None if normalized_mode == "none" else "deflate"

        def _connect(*args, **kwargs):
            kwargs["compression"] = compression_value
            return original_connect(*args, **kwargs)

        websockets.connect = _connect  # type: ignore[assignment]
        try:
            import websockets.client as ws_client  # type: ignore
            ws_client.connect = _connect  # type: ignore[assignment]
        except Exception:
            pass
        try:
            import websockets.asyncio.client as ws_async_client  # type: ignore
            ws_async_client.connect = _connect  # type: ignore[assignment]
        except Exception:
            pass
        try:
            import cdp_use.client as cdp_client_module  # type: ignore
            cdp_ws = getattr(cdp_client_module, "websockets", None)
            if cdp_ws and hasattr(cdp_ws, "connect"):
                cdp_ws.connect = _connect  # type: ignore[assignment]
        except Exception:
            pass
        cls._ws_patched = True
        cls._ws_patch_mode = normalized_mode

    def _get_ws_connect_kwargs(self) -> Optional[Dict[str, Any]]:
        mode = self._normalize_ws_compression_mode(self.ws_compression_mode)
        if mode == "auto":
            return None
        if mode == "none":
            return {"compression": None}
        return {"compression": "deflate"}

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
        session_paths = ("/session", "/chromium/session")
        payload = {
            "ttl": settings.browserless_session_ttl_ms,
            "stealth": settings.browserless_session_stealth,
            "headless": settings.browserless_session_headless,
        }

        async with httpx.AsyncClient(timeout=30) as client:
            last_error = None
            for path in session_paths:
                url = f"{host}{path}?token={self.browserless_token}"
                resp = await client.post(url, json=payload)
                if resp.status_code == 404:
                    last_error = resp
                    continue
                if resp.status_code >= 400:
                    raise RuntimeError(f"Erro ao criar sessao Browserless: {resp.status_code} {resp.text}")
                return resp.json()

            if last_error is not None:
                logger.warning(
                    "API de sessao do Browserless indisponivel (%s %s). Usando CDP padrao.",
                    last_error.status_code,
                    last_error.text,
                )
                return {}
            return {}

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

    def _patch_event_bus_for_stop(self, browser_session: BrowserSession):
        event_bus = getattr(browser_session, "event_bus", None)
        if event_bus is None:
            return None
        original_publish = getattr(event_bus, "publish", None)
        original_emit = getattr(event_bus, "emit", None)

        def _should_block(event) -> bool:
            name = getattr(event, "name", "") or getattr(event, "event_name", "")
            return event.__class__.__name__ == "BrowserStopEvent" or name in (
                "BrowserStopEvent",
                "browser_stop",
                "browser_stop_event",
            )

        if callable(original_publish):
            def _publish_wrapper(event, *args, **kwargs):
                if _should_block(event):
                    return None
                return original_publish(event, *args, **kwargs)

            try:
                event_bus.publish = _publish_wrapper  # type: ignore[assignment]
            except Exception:
                pass

        if callable(original_emit):
            def _emit_wrapper(event, *args, **kwargs):
                if _should_block(event):
                    return None
                return original_emit(event, *args, **kwargs)

            try:
                event_bus.emit = _emit_wrapper  # type: ignore[assignment]
            except Exception:
                pass

        def _restore():
            if callable(original_publish):
                try:
                    event_bus.publish = original_publish  # type: ignore[assignment]
                except Exception:
                    pass
            if callable(original_emit):
                try:
                    event_bus.emit = original_emit  # type: ignore[assignment]
                except Exception:
                    pass

        return _restore

    def _create_browser_session(
        self,
        cdp_url: str,
        storage_state: Optional[Union[Dict[str, Any], str, Path]] = None,
    ) -> BrowserSession:
        """
        Cria BrowserSession com fallback de argumentos para diferentes versoes do browser-use.
        """
        clean_storage_state = self._sanitize_storage_state(storage_state)
        ws_connect_kwargs = self._get_ws_connect_kwargs()
        session = None
        base_kwargs = dict(cdp_url=cdp_url, storage_state=clean_storage_state)
        ctor_attempts = []
        if ws_connect_kwargs is not None:
            ctor_attempts.append({**base_kwargs, "ws_connect_kwargs": ws_connect_kwargs, "keep_alive": True})
        ctor_attempts.append({**base_kwargs, "keep_alive": True})
        if ws_connect_kwargs is not None:
            ctor_attempts.append({**base_kwargs, "ws_connect_kwargs": ws_connect_kwargs})
        ctor_attempts.append({**base_kwargs})
        for kwargs in ctor_attempts:
            try:
                session = BrowserSession(**kwargs)
                break
            except TypeError:
                continue
        if session is None:
            session = BrowserSession(cdp_url=cdp_url, storage_state=clean_storage_state)

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
        if hasattr(session, "auto_close"):
            try:
                session.auto_close = False
            except Exception:
                pass
        return session

    def _create_agent(self, task: str, llm: ChatOpenAI, browser_session: BrowserSession) -> Agent:
        possible_kwargs = {
            "task": task,
            "llm": llm,
            "browser_session": browser_session,
            "auto_close": False,
            "close_browser": False,
            "keep_browser_open": True,
            "keep_browser_session": True,
        }
        try:
            sig = inspect.signature(Agent.__init__)
            allowed = {k: v for k, v in possible_kwargs.items() if k in sig.parameters}
        except Exception:
            allowed = possible_kwargs
        return Agent(**allowed)

    def _get_latest_session(
        self,
        db: Session,
        instagram_username: Optional[str] = None,
    ) -> Optional[InstagramSession]:
        query = db.query(InstagramSession).filter(InstagramSession.is_active.is_(True))
        normalized_username = (instagram_username or "").strip().lstrip("@").lower()
        if normalized_username:
            query = query.filter(func.lower(InstagramSession.instagram_username) == normalized_username)
        return query.order_by(InstagramSession.updated_at.desc()).first()

    def _get_latest_investing_session(self, db: Session) -> Optional[InvestingSession]:
        return (
            db.query(InvestingSession)
            .filter(InvestingSession.is_active.is_(True))
            .order_by(InvestingSession.updated_at.desc())
            .first()
        )

    def _touch_session(self, db: Session, session: InstagramSession) -> None:
        session.last_used_at = datetime.utcnow()
        db.commit()

    def _touch_investing_session(self, db: Session, session: InvestingSession) -> None:
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

    def _get_browserless_reconnect_url(self, storage_state: Optional[Dict[str, Any]]) -> Optional[str]:
        if not storage_state:
            return None
        reconnect_url = storage_state.get("_browserless_reconnect")
        return reconnect_url if isinstance(reconnect_url, str) and reconnect_url else None

    def _sanitize_storage_state(
        self,
        storage_state: Optional[Union[Dict[str, Any], str, Path]],
    ) -> Optional[Union[Dict[str, Any], str]]:
        """
        Playwright aceita apenas cookies/origins no storage_state.
        """
        if not storage_state:
            return None
        if isinstance(storage_state, (str, Path)):
            return str(storage_state)
        if not isinstance(storage_state, dict):
            return None
        cookies = self._extract_cookies(storage_state)
        origins = storage_state.get("origins")
        if not isinstance(origins, list):
            origins = []
        if not cookies and not origins:
            return None
        return {
            "cookies": cookies,
            "origins": origins,
        }

    def _write_storage_state_temp_file(self, storage_state: Optional[Dict[str, Any]]) -> Optional[str]:
        clean_state = self._sanitize_storage_state(storage_state)
        if not isinstance(clean_state, dict):
            return None

        temp_dir = Path(tempfile.gettempdir()) / "instagram-scraper"
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_file = temp_dir / f"browser_use_storage_{uuid4().hex}.json"
        temp_file.write_text(json.dumps(clean_state), encoding="utf-8")
        return str(temp_file)

    def _cleanup_storage_state_temp_file(self, path: Optional[str]) -> None:
        if not path:
            return
        try:
            Path(path).unlink(missing_ok=True)
        except Exception as exc:
            logger.debug("Nao foi possivel remover storage_state temporario %s: %s", path, exc)

    def _ensure_ws_token(self, ws_url: str) -> str:
        if "token=" in ws_url:
            return ws_url
        separator = "&" if "?" in ws_url else "?"
        return f"{ws_url}{separator}token={self.browserless_token}"

    def _contains_protocol_error(self, text: str) -> bool:
        if not text:
            return False
        lowered = text.lower()
        markers = (
            "protocol error",
            "reserved bits must be 0",
            "connectionclosederror",
            "client is stopping",
            "sent 1002",
        )
        return any(marker in lowered for marker in markers)

    def _extract_json_object_with_key(self, text: str, key: str) -> Optional[Dict[str, Any]]:
        if not text:
            return None
        decoder = json.JSONDecoder()
        for idx, char in enumerate(text):
            if char != "{":
                continue
            try:
                obj, _ = decoder.raw_decode(text[idx:])
            except Exception:
                continue
            if isinstance(obj, dict) and key in obj:
                return obj
        return None

    def _extract_first_json_value(self, text: str) -> Optional[Any]:
        if not text:
            return None
        decoder = json.JSONDecoder()
        for idx, char in enumerate(text):
            if char not in ("{", "["):
                continue
            try:
                obj, _ = decoder.raw_decode(text[idx:])
            except Exception:
                continue
            return obj
        return None

    async def _send_cdp_command(
        self,
        browser_session: BrowserSession,
        method: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        params = params or {}
        candidates = []
        for attr in ("cdp_client_root", "_cdp_client_root", "cdp_client", "_cdp_client"):
            client = getattr(browser_session, attr, None)
            if client:
                candidates.append(client)
        cdp_session = getattr(browser_session, "cdp_session", None)
        if cdp_session is not None:
            for attr in ("cdp_client", "_cdp_client"):
                client = getattr(cdp_session, attr, None)
                if client:
                    candidates.append(client)

        for client in candidates:
            send = getattr(client, "send", None)
            if callable(send):
                try:
                    return await self._maybe_await(send(method, params))
                except Exception:
                    pass
            send_raw = getattr(client, "send_raw", None)
            if callable(send_raw):
                try:
                    payload = {"method": method, "params": params}
                    return await self._maybe_await(send_raw(payload))
                except Exception:
                    pass
        return None

    async def _prepare_browserless_reconnect(
        self,
        browser_session: BrowserSession,
    ) -> Optional[str]:
        timeout_ms = getattr(settings, "browserless_reconnect_timeout_ms", 60000)
        response = await self._send_cdp_command(
            browser_session,
            "Browserless.reconnect",
            {"timeout": timeout_ms},
        )
        if not isinstance(response, dict):
            return None
        reconnect_url = response.get("browserWSEndpoint") or response.get("wsEndpoint")
        if not reconnect_url:
            return None
        return self._ensure_ws_token(reconnect_url)

    async def _refresh_session_via_reconnect(
        self,
        db: Session,
        reconnect_url: str,
        existing: InstagramSession,
    ) -> Optional[Dict[str, Any]]:
        cdp_url = self._ensure_ws_token(reconnect_url)
        browser_session = self._create_browser_session(cdp_url)
        try:
            storage_state = await self._export_storage_state_with_retry(browser_session)
            if storage_state and self._extract_cookies(storage_state):
                existing.storage_state = storage_state
                existing.last_used_at = datetime.utcnow()
                db.commit()
                logger.info("Sessao do Instagram reutilizada via reconnect.")
                return storage_state
        except Exception as exc:
            logger.warning("Falha ao reutilizar sessao via reconnect: %s", exc)
        finally:
            await self._detach_browser_session(browser_session)
        return None

    async def _export_storage_state_from_reconnect(self, reconnect_url: str) -> Optional[Dict[str, Any]]:
        cdp_url = self._ensure_ws_token(reconnect_url)
        browser_session = self._create_browser_session(cdp_url)
        try:
            storage_state = await self._export_storage_state_with_retry(browser_session)
            if storage_state and self._extract_cookies(storage_state):
                return storage_state
        except Exception as exc:
            logger.warning("Falha ao exportar storage state via reconnect: %s", exc)
        finally:
            await self._detach_browser_session(browser_session)
        return None

    def get_cookies(self, storage_state: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Retorna lista de cookies a partir de um storage_state."""
        if not storage_state:
            return []
        return self._extract_cookies(storage_state)

    def get_user_agent(self, storage_state: Optional[Dict[str, Any]]) -> Optional[str]:
        """Retorna user-agent persistido junto da sessao (quando disponivel)."""
        if not isinstance(storage_state, dict):
            return None
        meta = storage_state.get("_meta")
        if isinstance(meta, dict):
            ua = meta.get("user_agent")
            if isinstance(ua, str) and ua.strip():
                return ua.strip()
        legacy_ua = storage_state.get("_user_agent")
        if isinstance(legacy_ua, str) and legacy_ua.strip():
            return legacy_ua.strip()
        return None

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

    def _has_valid_auth_cookie(self, storage_state: Dict[str, Any]) -> bool:
        """
        Verifica se existe cookie de autenticação aparentemente válido.
        """
        now_ts = datetime.utcnow().timestamp()
        for cookie in self._extract_cookies(storage_state):
            if str(cookie.get("name", "")).lower() != "sessionid":
                continue
            expires = cookie.get("expires")
            if expires in (None, -1, "-1"):
                return True
            try:
                expires_ts = float(expires)
            except (TypeError, ValueError):
                return True
            if expires_ts <= 0:
                return True
            if expires_ts > now_ts:
                return True
        return False

    async def _is_session_valid(self, storage_state: Dict[str, Any]) -> bool:
        """
        Verifica se o storage_state ainda representa uma sessao autenticada.
        """
        cookies = self._extract_cookies(storage_state)
        if not cookies:
            return False

        # Modo padrão: reutilização otimista baseada no cookie de sessão.
        if not settings.instagram_session_strict_validation:
            if self._has_valid_auth_cookie(storage_state):
                return True

        jar = self._build_cookie_jar(cookies)
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
        }
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                resp = await client.get("https://www.instagram.com/accounts/edit/", cookies=jar, headers=headers)
        except Exception:
            return self._has_valid_auth_cookie(storage_state)

        if resp.url and "login" in str(resp.url):
            return False
        text = (resp.text or "").lower()
        if "login" in text and ("password" in text or "senha" in text):
            return False

        return resp.status_code == 200

    async def _is_investing_session_valid(self, storage_state: Dict[str, Any]) -> bool:
        """
        Validacao simples de sessao do Investing.
        """
        cookies = self._extract_cookies(storage_state)
        if not cookies:
            return False

        if not settings.investing_session_strict_validation:
            return True

        jar = self._build_cookie_jar(cookies)
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
        }
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                resp = await client.get("https://br.investing.com/", cookies=jar, headers=headers)
        except Exception:
            return True

        return resp.status_code == 200

    def _should_retry_login_error(self, exc: Exception) -> bool:
        message = str(exc).lower()
        retry_markers = (
            "root cdp client not initialized",
            "failed to establish cdp connection",
            "connectionclosederror",
            "protocol error",
            "reserved bits must be 0",
            "sent 1002",
            "client is stopping",
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

    async def ensure_instagram_session(
        self,
        db: Session,
        instagram_username: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Garante uma sess??o autenticada do Instagram salva no banco.
        Retorna storage_state quando dispon??vel.
        """
        if db is None:
            logger.warning("?????? Sess??o de banco n??o fornecida; login n??o ser?? persistido.")
            return None

        normalized_username = (instagram_username or "").strip().lstrip("@").lower()
        existing = self._get_latest_session(db, instagram_username=normalized_username or None)
        if existing and existing.storage_state:
            if not settings.instagram_session_strict_validation:
                self._touch_session(db, existing)
                logger.info("Sessao do Instagram reutilizada do banco (validacao estrita desativada).")
                return existing.storage_state

            if await self._is_session_valid(existing.storage_state):
                self._touch_session(db, existing)
                logger.info("Sessao do Instagram reutilizada do banco.")
                return existing.storage_state

            reconnect_url = self._get_browserless_reconnect_url(existing.storage_state)
            if reconnect_url:
                refreshed = await self._refresh_session_via_reconnect(db, reconnect_url, existing)
                if refreshed:
                    return refreshed

            if settings.browserless_session_enabled:
                session_info = self._get_browserless_session_info(existing.storage_state)
                stop_url = session_info.get("stop")
                if stop_url:
                    await self._stop_browserless_session(stop_url)

            existing.is_active = False
            db.commit()
            logger.info("Sessao do Instagram expirada; realizando novo login.")

        configured_username = (settings.instagram_username or "").strip().lstrip("@").lower()
        if normalized_username and configured_username and normalized_username != configured_username:
            logger.warning(
                "Sessao solicitada para @%s, mas INSTAGRAM_USERNAME configurado e @%s. "
                "Login automatico sera ignorado para evitar usar conta errada.",
                normalized_username,
                configured_username,
            )
            return None

        if not settings.instagram_username or not settings.instagram_password:
            logger.warning(
                "Nenhuma sessao ativa valida no banco e INSTAGRAM_USERNAME/PASSWORD nao configurados; "
                "login automatico nao sera feito."
            )
            return None

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
        7) Se aparecer mensagem de login invalido (senha incorreta/usuario invalido), responda com "LOGIN_INVALID" e pare.
        8) Se houver challenge/2FA, pare e reporte erro.

        Importante:
        - Use apenas a aba atual (nao abrir nova aba).
        - Aguarde o DOM carregar; se ficar vazio, aguarde alguns segundos e recarregue uma vez.
        - Nao clique em "Forgot password?"; se nao encontrar um botao claro de login, pressione Enter no campo de senha.

        Ao final, confirme sucesso com um texto curto: "LOGIN_OK".
        """

        agent = self._create_agent(
            task=login_task,
            llm=llm,
            browser_session=browser_session,
        )
        login_ok = False
        original_stop = getattr(browser_session, "stop", None)
        stop_patched = False
        if callable(original_stop):
            async def _noop_stop(*_args, **_kwargs):
                return None
            try:
                browser_session.stop = _noop_stop  # type: ignore[assignment]
                stop_patched = True
            except Exception:
                pass
        restore_event_bus = self._patch_event_bus_for_stop(browser_session)
        try:
            history = await agent.run()
            if not history.is_done() or not history.is_successful():
                raise RuntimeError("Login nao foi concluido com sucesso.")
            final_text = (history.final_result() or "").strip().upper()
            if "LOGIN_INVALID" in final_text:
                raise RuntimeError("Login invalido detectado pelo agente.")

            logger.info("Exportando storage state do navegador...")
            if stop_patched and callable(original_stop):
                try:
                    browser_session.stop = original_stop  # type: ignore[assignment]
                except Exception:
                    pass
            reconnect_url = None
            try:
                reconnect_url = await self._prepare_browserless_reconnect(browser_session)
                storage_state = await self._export_storage_state_with_retry(browser_session)
            except Exception as exc:
                storage_state = None
                if reconnect_url:
                    storage_state = await self._export_storage_state_from_reconnect(reconnect_url)
                fallback_state = getattr(browser_session, "storage_state", None)
                if isinstance(fallback_state, dict) and self._extract_cookies(fallback_state):
                    storage_state = fallback_state
                    logger.warning("Storage state export falhou, usando fallback em memoria: %s", exc)
                else:
                    logger.exception("Falha ao exportar storage state: %s", exc)
                    raise

            if reconnect_url and storage_state is not None:
                storage_state["_browserless_reconnect"] = reconnect_url

            if not storage_state or not self._extract_cookies(storage_state):
                raise RuntimeError("Storage state nao possui cookies do Instagram.")

            if session_info:
                storage_state["_browserless_session"] = session_info

            configured_username = (settings.instagram_username or "").strip().lstrip("@").lower()
            deactivate_query = db.query(InstagramSession).filter(InstagramSession.is_active.is_(True))
            if configured_username:
                deactivate_query = deactivate_query.filter(
                    func.lower(InstagramSession.instagram_username) == configured_username
                )
            else:
                deactivate_query = deactivate_query.filter(InstagramSession.instagram_username.is_(None))
            deactivate_query.update({InstagramSession.is_active: False}, synchronize_session=False)

            session = InstagramSession(
                instagram_username=configured_username or None,
                storage_state=storage_state,
                last_used_at=datetime.utcnow(),
                is_active=True,
            )
            db.add(session)
            db.commit()
            db.refresh(session)

            login_ok = True
            logger.info("Sessao do Instagram salva no banco.")
            return storage_state

        finally:
            if callable(restore_event_bus):
                restore_event_bus()
            if login_ok and settings.browserless_session_enabled:
                await self._detach_browser_session(browser_session)
            else:
                await self._safe_stop_session(browser_session)
            if not login_ok and stop_url:
                await self._stop_browserless_session(stop_url)

    async def ensure_investing_session(
        self,
        db: Session,
        force_login: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """
        Garante uma sessao autenticada do Investing salva no banco.
        """
        if db is None:
            logger.warning("Sessao de banco nao fornecida; sessao do Investing nao sera persistida.")
            return None

        if not settings.investing_username or not settings.investing_password:
            logger.warning("INVESTING_USERNAME/PASSWORD nao configurados; login nao sera feito.")
            return None

        existing = self._get_latest_investing_session(db)
        if existing and force_login:
            existing.is_active = False
            db.commit()
            existing = None
            logger.info("Sessao Investing invalida por force_login=true.")

        if existing and existing.storage_state:
            if not settings.investing_session_strict_validation:
                self._touch_investing_session(db, existing)
                logger.info("Sessao do Investing reutilizada do banco (validacao estrita desativada).")
                return existing.storage_state

            if await self._is_investing_session_valid(existing.storage_state):
                self._touch_investing_session(db, existing)
                logger.info("Sessao do Investing reutilizada do banco.")
                return existing.storage_state

            existing.is_active = False
            db.commit()
            logger.info("Sessao do Investing expirada; realizando novo login.")

        last_error = None
        for attempt in range(1, settings.browser_use_max_retries + 1):
            try:
                return await self._login_and_save_investing_session(db)
            except Exception as exc:
                last_error = exc
                if attempt >= settings.browser_use_max_retries or not self._should_retry_login_error(exc):
                    break
                delay = settings.browser_use_retry_backoff * attempt
                logger.warning(
                    "Login Investing falhou (tentativa %s/%s): %s. Retentando em %ss...",
                    attempt,
                    settings.browser_use_max_retries,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)

        if last_error:
            raise last_error
        return None

    async def _login_and_save_investing_session(self, db: Session) -> Dict[str, Any]:
        logger.info("Iniciando login no Investing via Browser Use...")

        cdp_url = await self._resolve_browserless_cdp_url()
        browser_session = self._create_browser_session(cdp_url)
        llm = ChatOpenAI(model=self.model, api_key=self.api_key)

        login_task = f"""
        Voce esta em um navegador controlado por IA.
        Acesse https://br.investing.com/.

        Passos:
        1) Se houver modal de cookies, aceite.
        2) Clique em entrar/login/sign in.
        3) Preencha email/usuario com: {settings.investing_username}
        4) Preencha senha com: {settings.investing_password}
        5) Envie o formulario de login.
        6) Aguarde ate confirmar que o usuario esta autenticado.
        7) Se credenciais invalidas, responda "LOGIN_INVALID".

        Regras:
        - Use apenas a aba atual.
        - Nao abra nova aba.
        - Se houver captcha/challenge nao resolvido, responda "LOGIN_BLOCKED".
        - Ao final, responda apenas "LOGIN_OK" quando autenticado.
        """

        agent = self._create_agent(
            task=login_task,
            llm=llm,
            browser_session=browser_session,
        )
        login_ok = False
        restore_event_bus = self._patch_event_bus_for_stop(browser_session)
        try:
            history = await agent.run()
            if not history.is_done() or not history.is_successful():
                raise RuntimeError("Login Investing nao foi concluido com sucesso.")

            final_text = (history.final_result() or "").strip().upper()
            if "LOGIN_INVALID" in final_text:
                raise RuntimeError("Login invalido detectado pelo agente no Investing.")
            if "LOGIN_BLOCKED" in final_text:
                raise RuntimeError("Login bloqueado por challenge/captcha no Investing.")

            storage_state = await self._export_storage_state_with_retry(browser_session)
            if not storage_state or not self._extract_cookies(storage_state):
                raise RuntimeError("Storage state do Investing nao possui cookies.")

            (
                db.query(InvestingSession)
                .filter(InvestingSession.is_active.is_(True))
                .update({InvestingSession.is_active: False}, synchronize_session=False)
            )

            session = InvestingSession(
                investing_username=settings.investing_username,
                storage_state=storage_state,
                last_used_at=datetime.utcnow(),
                is_active=True,
            )
            db.add(session)
            db.commit()
            db.refresh(session)

            login_ok = True
            logger.info("Sessao do Investing salva no banco.")
            return storage_state
        finally:
            if callable(restore_event_bus):
                restore_event_bus()
            if login_ok:
                await self._detach_browser_session(browser_session)
            else:
                await self._safe_stop_session(browser_session)


    async def scrape_profile_posts(
        self,
        profile_url: str,
        storage_state: Optional[Dict[str, Any]],
        max_posts: int = 5,
    ) -> Dict[str, Any]:
        """
        Usa Browser Use para navegar no perfil e extrair posts de forma inteligente.

        Args:
            profile_url: URL do perfil Instagram
            storage_state: Estado de sessão autenticada (cookies)
            max_posts: Número máximo de posts a raspar

        Returns:
            Dicionário com posts extraídos
        """
        max_retries = getattr(settings, 'browser_use_max_retries', 3)
        retry_delay = 5  # segundos
        reconnect_url = self._get_browserless_reconnect_url(storage_state)
        session_info = self._get_browserless_session_info(storage_state)
        session_connect_url = session_info.get("connect") if isinstance(session_info.get("connect"), str) else None
        clean_storage_state = self._sanitize_storage_state(storage_state)
        storage_state_file = self._write_storage_state_temp_file(storage_state)
        storage_state_for_session: Optional[Union[Dict[str, Any], str]]
        storage_state_for_session = storage_state_file or clean_storage_state
        logger.info(
            "Browser Use recebeu storage_state com %s cookies.",
            len(self._extract_cookies(storage_state or {})),
        )
        if storage_state_file:
            logger.info("Storage state persistido em arquivo temporario para compatibilidade com browser-use 0.11.x.")

        try:
            for attempt in range(1, max_retries + 1):
                browser_session = None
                restore_event_bus = None

                try:
                    logger.info(f"🤖 Browser Use: Raspando posts de {profile_url} (tentativa {attempt}/{max_retries})")

                    if not self.api_key:
                        raise ValueError("OPENAI_API_KEY is required for Browser Use.")

                    use_reconnect = bool(reconnect_url and attempt == 1)
                    use_session_connect = bool((not reconnect_url) and session_connect_url and attempt == 1)
                    if use_reconnect:
                        cdp_url = self._ensure_ws_token(reconnect_url)
                        logger.info("Tentando reaproveitar navegador autenticado via reconnect.")
                    elif use_session_connect:
                        cdp_url = self._ensure_ws_token(session_connect_url)
                        logger.info("Tentando reaproveitar sessao Browserless existente.")
                    else:
                        cdp_url = await self._resolve_browserless_cdp_url()
                        logger.info("Usando CDP padrao com storage_state.")

                    task = f"""
                    Você é um raspador de dados do Instagram. Extraia os primeiros {max_posts} posts do perfil.

                    PERFIL:
                    - URL: {profile_url}

                    ESTRATÉGIA (obrigatória):
                    1) Abra o perfil e aguarde carregar.
                    2) Faça scroll suave 2-3 vezes para carregar o grid.
                    3) Colete os primeiros {max_posts} links CANÔNICOS de posts a partir de anchors com href contendo "/p/" ou "/reel/".
                       - Não clique em ícones SVG, overlays de "Clip" ou elementos decorativos.
                       - Se precisar clicar, clique no link/anchor do post (href /p/... ou /reel/...), não no ícone.
                    4) Para cada URL coletada:
                       a) Navegue para a URL do post na MESMA aba (new_tab: false).
                       b) Aguarde carregar.
                       c) Extraia:
                          - caption completa (ou null)
                          - like_count (inteiro ou null)
                          - comment_count (inteiro ou null)
                          - posted_at (texto visível ou null)
                    5) Retorne JSON final com todos os posts coletados.

                    FORMATO DE SAÍDA (JSON puro, sem texto extra):
                    {{
                      "posts": [
                        {{
                          "post_url": "https://instagram.com/p/CODIGO/ ou https://instagram.com/reel/CODIGO/",
                          "caption": "texto da caption",
                          "like_count": 123,
                          "comment_count": 45,
                          "posted_at": "2 dias atrás" ou null
                        }}
                      ],
                      "total_found": {max_posts}
                    }}

                    REGRAS:
                    - Se o perfil for privado: {{"posts": [], "total_found": 0, "error": "private_profile"}}
                    - Use apenas a aba atual; não abra nova aba/janela.
                    - Se não conseguir um campo, retorne null naquele campo.
                    - Se não conseguir abrir um post, pule para o próximo.
                    - Não invente dados.
                    """

                    browser_session = self._create_browser_session(cdp_url, storage_state=storage_state_for_session)
                    llm = ChatOpenAI(model=self.model, api_key=self.api_key)
                    agent = self._create_agent(
                        task=task,
                        llm=llm,
                        browser_session=browser_session,
                    )

                    restore_event_bus = self._patch_event_bus_for_stop(browser_session)
                    history = await agent.run()

                    if not history.is_done():
                        logger.warning("⚠️ Browser Use não completou a tarefa")
                        # Não fazer return aqui, deixar o except capturar

                    final_result = history.final_result() or ""

                    if (not history.is_successful()) and self._contains_protocol_error(final_result) and attempt < max_retries:
                        wait_time = retry_delay * attempt
                        logger.warning(
                            "Sessao CDP instavel detectada (tentativa %s/%s). Retentando em %ss...",
                            attempt,
                            max_retries,
                            wait_time,
                        )
                        await asyncio.sleep(wait_time)
                        continue

                    data = self._extract_json_object_with_key(final_result, "posts")
                    if data is not None:
                        if data.get("error") == "login_required" and attempt < max_retries:
                            wait_time = retry_delay * attempt
                            logger.warning(
                                "Agente retornou login_required (tentativa %s/%s). Retentando em %ss...",
                                attempt,
                                max_retries,
                                wait_time,
                            )
                            await asyncio.sleep(wait_time)
                            continue
                        logger.info(f"✅ Browser Use extraiu {len(data.get('posts', []))} posts")
                        return data  # Sucesso!

                    # Fallback: retornar resultado bruto
                    logger.warning("⚠️ Não foi possível extrair JSON estruturado")
                    if self._contains_protocol_error(final_result) and attempt < max_retries:
                        wait_time = retry_delay * attempt
                        logger.warning(
                            "Falha de protocolo detectada no resultado final (%s/%s). Retentando em %ss...",
                            attempt,
                            max_retries,
                            wait_time,
                        )
                        await asyncio.sleep(wait_time)
                        continue
                    return {
                        "posts": [],
                        "total_found": 0,
                        "raw_result": final_result,
                        "error": "parse_failed"
                    }

                except Exception as e:
                    error_msg = str(e)
                    is_retryable = any(marker in error_msg.lower() for marker in [
                        "http 500",
                        "connection",
                        "timeout",
                        "websocket",
                        "failed to establish",
                        "protocol error",
                        "reserved bits",
                        "client is stopping",
                    ])

                    if is_retryable and attempt < max_retries:
                        wait_time = retry_delay * attempt
                        logger.warning(
                            f"⚠️ Tentativa {attempt}/{max_retries} falhou: {error_msg[:100]}. "
                            f"Aguardando {wait_time}s antes de tentar novamente..."
                        )
                        await asyncio.sleep(wait_time)
                        # Continue para próxima iteração
                    else:
                        # Não é retryável ou última tentativa
                        logger.error(f"❌ Erro no Browser Use Agent (tentativa {attempt}/{max_retries}): {e}")
                        return {"posts": [], "total_found": 0, "error": str(e)}

                finally:
                    # Sempre limpar recursos
                    if callable(restore_event_bus):
                        restore_event_bus()
                    if browser_session:
                        await self._detach_browser_session(browser_session)

            # Se saiu do loop sem retornar, todas as tentativas falharam
            return {"posts": [], "total_found": 0, "error": "all_retries_failed"}
        finally:
            self._cleanup_storage_state_temp_file(storage_state_file)

    async def scrape_post_like_users(
        self,
        post_url: str,
        storage_state: Optional[Dict[str, Any]],
        max_users: int = 30,
    ) -> Dict[str, Any]:
        """
        Abre um post e tenta extrair os perfis que curtiram.

        Returns:
            {
              "post_url": str,
              "likes_accessible": bool,
              "like_users": [url, ...],
              "error": Optional[str]
            }
        """
        max_retries = getattr(settings, "browser_use_max_retries", 3)
        retry_delay = 5
        reconnect_url = self._get_browserless_reconnect_url(storage_state)
        session_info = self._get_browserless_session_info(storage_state)
        session_connect_url = session_info.get("connect") if isinstance(session_info.get("connect"), str) else None
        clean_storage_state = self._sanitize_storage_state(storage_state)
        storage_state_file = self._write_storage_state_temp_file(storage_state)
        storage_state_for_session: Optional[Union[Dict[str, Any], str]]
        storage_state_for_session = storage_state_file or clean_storage_state

        try:
            for attempt in range(1, max_retries + 1):
                browser_session = None
                restore_event_bus = None
                try:
                    logger.info(
                        "🤖 Browser Use: Coletando curtidores de %s (tentativa %s/%s)",
                        post_url,
                        attempt,
                        max_retries,
                    )

                    if not self.api_key:
                        raise ValueError("OPENAI_API_KEY is required for Browser Use.")

                    use_reconnect = bool(reconnect_url and attempt == 1)
                    use_session_connect = bool((not reconnect_url) and session_connect_url and attempt == 1)
                    if use_reconnect:
                        cdp_url = self._ensure_ws_token(reconnect_url)
                    elif use_session_connect:
                        cdp_url = self._ensure_ws_token(session_connect_url)
                    else:
                        cdp_url = await self._resolve_browserless_cdp_url()

                    task = f"""
                    Você está em um navegador autenticado no Instagram.
                    Sua tarefa é extrair os links dos perfis que curtiram um post.

                    PASSOS:
                    1) Acesse o post: {post_url}
                    2) Aguarde a página carregar.
                    3) Se houver modal de cookies, aceite.
                    4) Localize e clique no link/botão de curtidas para abrir a lista de usuários.
                    5) Se a lista abrir, role o modal/lista até coletar até {max_users} links únicos de perfis.
                    6) Retorne os links no formato https://www.instagram.com/usuario/

                    FORMATO DE SAÍDA (JSON):
                    {{
                      "post_url": "{post_url}",
                      "likes_accessible": true,
                      "like_users": ["https://www.instagram.com/usuario1/"],
                      "total_collected": 1
                    }}

                    REGRAS:
                    - Se não for possível abrir a lista de curtidas, retorne:
                      {{
                        "post_url": "{post_url}",
                        "likes_accessible": false,
                        "like_users": [],
                        "error": "likes_unavailable"
                      }}
                    - Não abra nova aba.
                    - Não invente links.
                    """

                    browser_session = self._create_browser_session(cdp_url, storage_state=storage_state_for_session)
                    llm = ChatOpenAI(model=self.model, api_key=self.api_key)
                    agent = self._create_agent(
                        task=task,
                        llm=llm,
                        browser_session=browser_session,
                    )

                    restore_event_bus = self._patch_event_bus_for_stop(browser_session)
                    history = await agent.run()
                    final_result = history.final_result() or ""

                    if (not history.is_successful()) and self._contains_protocol_error(final_result) and attempt < max_retries:
                        wait_time = retry_delay * attempt
                        logger.warning(
                            "Sessao CDP instavel ao coletar curtidores (%s/%s). Retentando em %ss...",
                            attempt,
                            max_retries,
                            wait_time,
                        )
                        await asyncio.sleep(wait_time)
                        continue

                    data = self._extract_json_object_with_key(final_result, "likes_accessible")
                    if data is None:
                        logger.warning("⚠️ Falha ao extrair JSON de curtidores: %s", final_result[:180])
                        if self._contains_protocol_error(final_result) and attempt < max_retries:
                            wait_time = retry_delay * attempt
                            logger.warning(
                                "Falha de protocolo detectada na coleta de curtidores (%s/%s). Retentando em %ss...",
                                attempt,
                                max_retries,
                                wait_time,
                            )
                            await asyncio.sleep(wait_time)
                            continue
                        return {
                            "post_url": post_url,
                            "likes_accessible": False,
                            "like_users": [],
                            "error": "parse_failed",
                            "raw_result": final_result,
                        }

                    if data.get("error") == "login_required" and attempt < max_retries:
                        wait_time = retry_delay * attempt
                        logger.warning(
                            "Agente retornou login_required ao coletar curtidores (%s/%s). Retentando em %ss...",
                            attempt,
                            max_retries,
                            wait_time,
                        )
                        await asyncio.sleep(wait_time)
                        continue

                    unique_users: list[str] = []
                    for value in data.get("like_users", []) or []:
                        if not isinstance(value, str):
                            continue
                        if "instagram.com" not in value:
                            continue
                        normalized = value.strip()
                        if normalized and normalized not in unique_users:
                            unique_users.append(normalized)
                        if len(unique_users) >= max_users:
                            break

                    return {
                        "post_url": data.get("post_url") or post_url,
                        "likes_accessible": bool(data.get("likes_accessible")),
                        "like_users": unique_users,
                        "total_collected": len(unique_users),
                        "error": data.get("error"),
                    }

                except Exception as exc:
                    error_msg = str(exc).lower()
                    is_retryable = any(
                        marker in error_msg
                        for marker in (
                            "http 500",
                            "connection",
                            "timeout",
                            "websocket",
                            "failed to establish",
                            "protocol error",
                            "reserved bits",
                            "client is stopping",
                        )
                    )
                    if is_retryable and attempt < max_retries:
                        wait_time = retry_delay * attempt
                        logger.warning(
                            "⚠️ Tentativa %s/%s falhou ao coletar curtidores: %s. Retentando em %ss...",
                            attempt,
                            max_retries,
                            str(exc)[:120],
                            wait_time,
                        )
                        await asyncio.sleep(wait_time)
                        continue
                    return {
                        "post_url": post_url,
                        "likes_accessible": False,
                        "like_users": [],
                        "error": str(exc),
                    }
                finally:
                    if callable(restore_event_bus):
                        restore_event_bus()
                    if browser_session:
                        await self._detach_browser_session(browser_session)

            return {
                "post_url": post_url,
                "likes_accessible": False,
                "like_users": [],
                "error": "all_retries_failed",
            }
        finally:
            self._cleanup_storage_state_temp_file(storage_state_file)

    async def scrape_profile_basic_info(
        self,
        profile_url: str,
        storage_state: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Extrai dados básicos de um perfil Instagram usando o mesmo fluxo autenticado do Browser Use.
        """
        max_retries = getattr(settings, "browser_use_max_retries", 3)
        retry_delay = 5
        reconnect_url = self._get_browserless_reconnect_url(storage_state)
        session_info = self._get_browserless_session_info(storage_state)
        session_connect_url = session_info.get("connect") if isinstance(session_info.get("connect"), str) else None
        clean_storage_state = self._sanitize_storage_state(storage_state)
        storage_state_file = self._write_storage_state_temp_file(storage_state)
        storage_state_for_session: Optional[Union[Dict[str, Any], str]]
        storage_state_for_session = storage_state_file or clean_storage_state

        try:
            for attempt in range(1, max_retries + 1):
                browser_session = None
                restore_event_bus = None
                try:
                    logger.info(
                        "🤖 Browser Use: Extraindo dados do perfil %s (tentativa %s/%s)",
                        profile_url,
                        attempt,
                        max_retries,
                    )

                    use_reconnect = bool(reconnect_url and attempt == 1)
                    use_session_connect = bool((not reconnect_url) and session_connect_url and attempt == 1)
                    if use_reconnect:
                        cdp_url = self._ensure_ws_token(reconnect_url)
                    elif use_session_connect:
                        cdp_url = self._ensure_ws_token(session_connect_url)
                    else:
                        cdp_url = await self._resolve_browserless_cdp_url()

                    task = f"""
                    Você está em um navegador autenticado no Instagram.
                    Extraia os dados do perfil em JSON puro.

                    PERFIL:
                    - URL: {profile_url}

                    PASSOS:
                    1) Navegue para a URL do perfil na aba atual.
                    2) Aguarde a página carregar.
                    3) Se houver modal de cookies, aceite.
                    4) Extraia os campos visíveis do perfil.

                    FORMATO (JSON puro):
                    {{
                      "username": "string ou null",
                      "full_name": "string ou null",
                      "bio": "string ou null",
                      "is_private": true/false,
                      "follower_count": número inteiro ou null,
                      "following_count": número inteiro ou null,
                      "post_count": número inteiro ou null,
                      "verified": true/false
                    }}

                    REGRAS:
                    - Não abra nova aba.
                    - Não invente dados.
                    - Se não conseguir um campo, retorne null.
                    """

                    browser_session = self._create_browser_session(cdp_url, storage_state=storage_state_for_session)
                    llm = ChatOpenAI(model=self.model, api_key=self.api_key)
                    agent = self._create_agent(
                        task=task,
                        llm=llm,
                        browser_session=browser_session,
                    )

                    restore_event_bus = self._patch_event_bus_for_stop(browser_session)
                    history = await agent.run()
                    final_result = history.final_result() or ""

                    if (not history.is_successful()) and self._contains_protocol_error(final_result) and attempt < max_retries:
                        wait_time = retry_delay * attempt
                        logger.warning(
                            "Sessao CDP instavel ao extrair perfil (%s/%s). Retentando em %ss...",
                            attempt,
                            max_retries,
                            wait_time,
                        )
                        await asyncio.sleep(wait_time)
                        continue

                    data = self._extract_json_object_with_key(final_result, "username")
                    if data is None:
                        if self._contains_protocol_error(final_result) and attempt < max_retries:
                            wait_time = retry_delay * attempt
                            logger.warning(
                                "Falha de protocolo ao extrair perfil (%s/%s). Retentando em %ss...",
                                attempt,
                                max_retries,
                                wait_time,
                            )
                            await asyncio.sleep(wait_time)
                            continue
                        return {
                            "error": "parse_failed",
                            "raw_result": final_result,
                        }

                    return data

                except Exception as exc:
                    error_msg = str(exc).lower()
                    is_retryable = any(
                        marker in error_msg
                        for marker in (
                            "http 500",
                            "connection",
                            "timeout",
                            "websocket",
                            "failed to establish",
                            "protocol error",
                            "reserved bits",
                            "client is stopping",
                        )
                    )
                    if is_retryable and attempt < max_retries:
                        wait_time = retry_delay * attempt
                        logger.warning(
                            "⚠️ Tentativa %s/%s falhou ao extrair perfil: %s. Retentando em %ss...",
                            attempt,
                            max_retries,
                            str(exc)[:120],
                            wait_time,
                        )
                        await asyncio.sleep(wait_time)
                        continue
                    return {"error": str(exc)}
                finally:
                    if callable(restore_event_bus):
                        restore_event_bus()
                    if browser_session:
                        await self._detach_browser_session(browser_session)

            return {"error": "all_retries_failed"}
        finally:
            self._cleanup_storage_state_temp_file(storage_state_file)

    async def generic_scrape(
        self,
        url: str,
        prompt: str,
        storage_state: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Scraping generico de qualquer site usando Browser Use + Browserless.
        """
        max_retries = getattr(settings, "browser_use_max_retries", 3)
        retry_delay = 3
        clean_storage_state = self._sanitize_storage_state(storage_state)
        storage_state_file = self._write_storage_state_temp_file(storage_state)
        storage_state_for_session: Optional[Union[Dict[str, Any], str]]
        storage_state_for_session = storage_state_file or clean_storage_state

        try:
            for attempt in range(1, max_retries + 1):
                browser_session = None
                restore_event_bus = None
                try:
                    cdp_url = await self._resolve_browserless_cdp_url()
                    browser_session = self._create_browser_session(cdp_url, storage_state=storage_state_for_session)
                    llm = ChatOpenAI(model=self.model, api_key=self.api_key)

                    task = f"""
                    Voce e um agente de scraping generico.

                    URL alvo:
                    - {url}

                    Instrucoes do usuario (seguir literalmente):
                    {prompt}

                    Regras:
                    - Use apenas a aba atual.
                    - Nao invente dados.
                    - Se algo falhar, retorne um JSON com campo "error".
                    - Retorne no final APENAS o formato pedido pelo usuario.
                    """

                    agent = self._create_agent(
                        task=task,
                        llm=llm,
                        browser_session=browser_session,
                    )

                    restore_event_bus = self._patch_event_bus_for_stop(browser_session)
                    history = await agent.run()
                    final_result = history.final_result() or ""

                    if (not history.is_successful()) and self._contains_protocol_error(final_result) and attempt < max_retries:
                        await asyncio.sleep(retry_delay * attempt)
                        continue

                    parsed = self._extract_first_json_value(final_result)
                    return {
                        "status": "success",
                        "url": url,
                        "data": parsed,
                        "raw_result": final_result,
                        "error": None,
                    }
                except Exception as exc:
                    if attempt < max_retries:
                        await asyncio.sleep(retry_delay * attempt)
                        continue
                    return {
                        "status": "failed",
                        "url": url,
                        "data": None,
                        "raw_result": None,
                        "error": str(exc),
                    }
                finally:
                    if callable(restore_event_bus):
                        restore_event_bus()
                    if browser_session:
                        await self._detach_browser_session(browser_session)
        finally:
            self._cleanup_storage_state_temp_file(storage_state_file)

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
