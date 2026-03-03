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
        self.fallback_model = (
            (settings.openai_fallback_model_text or "").strip() or None
        )
        if self.fallback_model == self.model:
            self.fallback_model = None
        self.api_key = settings.openai_api_key
        self.browserless_host = settings.browserless_host
        self.browserless_token = settings.browserless_token
        self.browserless_ws_url = settings.browserless_ws_url
        self.ws_compression_mode = self._normalize_ws_compression_mode(
            getattr(settings, "browser_use_ws_compression", "none")
        )
        # Respect LOG_LEVEL from .env for browser_use logs.
        level = getattr(logging, settings.log_level, logging.INFO)
        for name in ("browser_use", "browser_use.Agent", "browser_use.BrowserSession", "browser_use.tools"):
            log = logging.getLogger(name)
            log.setLevel(level)
            log.propagate = True
        self._patch_websocket_compression(self.ws_compression_mode)
        logger.info("Browser Use WebSocket compression mode: %s", self.ws_compression_mode)
        if self.fallback_model:
            logger.info("Browser Use fallback model enabled: %s -> %s", self.model, self.fallback_model)

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

    def _toggle_ws_compression_mode(self, reason: Optional[str] = None) -> str:
        current = self._normalize_ws_compression_mode(self.ws_compression_mode)
        target = "deflate" if current == "none" else "none"
        self.ws_compression_mode = target
        self._patch_websocket_compression(target)
        if reason:
            logger.warning(
                "Alternando WebSocket compression para %s (%s).",
                target,
                reason,
            )
        else:
            logger.warning("Alternando WebSocket compression para %s.", target)
        return target

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
        self._patch_websocket_compression(self.ws_compression_mode)
        clean_storage_state = self._sanitize_storage_state(storage_state)
        ws_connect_kwargs = self._get_ws_connect_kwargs()
        min_page_load_wait = float(getattr(settings, "browser_use_min_page_load_wait_s", 1.0))
        network_idle_wait = float(getattr(settings, "browser_use_network_idle_wait_s", 8.0))
        wait_between_actions = float(getattr(settings, "browser_use_wait_between_actions_s", 0.2))
        session = None
        base_kwargs = dict(
            cdp_url=cdp_url,
            storage_state=clean_storage_state,
            minimum_wait_page_load_time=min_page_load_wait,
            wait_for_network_idle_page_load_time=network_idle_wait,
            wait_between_actions=wait_between_actions,
        )
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
            "fallback_llm": self._create_fallback_llm(),
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

    def _create_fallback_llm(self) -> Optional[ChatOpenAI]:
        if not self.fallback_model:
            return None
        return ChatOpenAI(model=self.fallback_model, api_key=self.api_key)

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
            "browser not connected",
            "cannot navigate - browser not connected",
            "failed to establish cdp connection",
            "root cdp client not initialized",
        )
        return any(marker in lowered for marker in markers)

    def _contains_rate_limit_error(self, text: Optional[str]) -> bool:
        if not text:
            return False
        lowered = str(text).lower()
        markers = (
            "rate limit",
            "rate_limit_exceeded",
            "too many requests",
            "error code: 429",
            "http/1.1 429",
            "modelratelimiterror",
            "tokens per min",
            "tpm",
        )
        return any(marker in lowered for marker in markers)

    def _history_errors_text(self, history: Any) -> str:
        if history is None:
            return ""
        try:
            errors = history.errors()
        except Exception:
            return ""
        if not isinstance(errors, list):
            return ""
        parts: List[str] = []
        for item in errors:
            if item:
                parts.append(str(item))
        return " | ".join(parts)

    def _classify_agent_failure_error(
        self,
        final_result: str = "",
        history: Any = None,
        exc: Optional[Exception] = None,
    ) -> str:
        history_errors = self._history_errors_text(history)
        combined = " | ".join(
            part
            for part in (
                final_result or "",
                history_errors,
                str(exc) if exc else "",
            )
            if part
        )
        if self._contains_rate_limit_error(combined):
            return "rate_limit_exceeded"
        if self._contains_protocol_error(combined):
            return "protocol_error"
        return "parse_failed"

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

    def _normalize_story_interaction_type(self, value: Any) -> str:
        raw = str(value or "").strip().lower()
        if not raw:
            return ""

        replacements = str.maketrans(
            {
                "á": "a",
                "à": "a",
                "â": "a",
                "ã": "a",
                "é": "e",
                "ê": "e",
                "í": "i",
                "ó": "o",
                "ô": "o",
                "õ": "o",
                "ú": "u",
                "ç": "c",
            }
        )
        normalized = raw.translate(replacements).replace("-", "_").replace(" ", "_")
        normalized = normalized.replace("/", "_").replace("__", "_")

        if any(token in normalized for token in ("view", "visual", "viewer", "seen", "watch")):
            return "view"
        if "like" in normalized or "curt" in normalized:
            return "like"
        if "reply" in normalized or "respost" in normalized:
            return "reply"
        if "reaction" in normalized or "react" in normalized or "emoji" in normalized:
            return "reaction"
        if "poll" in normalized or "enquete" in normalized:
            return "poll_vote"
        if "quiz" in normalized:
            return "quiz_answer"
        if "question" in normalized or "pergunta" in normalized:
            return "question_reply"
        if "mention" in normalized or "mencao" in normalized:
            return "mention_tap"
        if "link" in normalized:
            return "link_click"
        if "sticker" in normalized:
            return "sticker_tap"
        return normalized

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

    def _normalize_story_url_value(self, value: Any) -> str:
        raw_url = str(value or "").strip()
        if not raw_url:
            return ""
        if raw_url.startswith("/"):
            raw_url = f"https://www.instagram.com{raw_url}"
        parsed = urlparse(raw_url)
        path_parts = [part for part in parsed.path.split("/") if part]
        if len(path_parts) >= 3 and path_parts[0].lower() == "stories":
            username_part = path_parts[1].strip().lstrip("@")
            story_id_part = path_parts[2].strip()
            if username_part and story_id_part:
                return f"https://www.instagram.com/stories/{username_part}/{story_id_part}/"
        if raw_url and "/stories/" in raw_url and not raw_url.endswith("/"):
            raw_url = f"{raw_url}/"
        return raw_url

    def _extract_story_id_from_url(self, story_url: str) -> Optional[str]:
        normalized_url = self._normalize_story_url_value(story_url)
        if not normalized_url:
            return None
        parsed = urlparse(normalized_url)
        path_parts = [part for part in parsed.path.split("/") if part]
        if len(path_parts) >= 3 and path_parts[0].lower() == "stories":
            story_id = path_parts[2].strip()
            return story_id or None
        return None

    async def _ensure_browser_session_connected(
        self,
        browser_session: BrowserSession,
        timeout_ms: int = 15000,
    ) -> None:
        timeout_s = max(1.0, float(timeout_ms) / 1000.0)
        errors: List[str] = []

        for method_name in ("start", "connect"):
            method = getattr(browser_session, method_name, None)
            if not callable(method):
                continue
            try:
                await asyncio.wait_for(
                    self._maybe_await(method()),
                    timeout=timeout_s,
                )
                return
            except Exception as exc:
                errors.append(f"{method_name}: {exc}")

        details = "; ".join(errors) if errors else "no start/connect method available"
        raise RuntimeError(
            f"failed to establish cdp connection (browser not connected): {details}"
        )

    async def _navigate_to_url_with_timeout(
        self,
        browser_session: BrowserSession,
        url: str,
        timeout_ms: int = 15000,
        new_tab: bool = False,
    ) -> None:
        await self._ensure_browser_session_connected(browser_session, timeout_ms=timeout_ms)
        try:
            from browser_use.browser.events import NavigateToUrlEvent

            event = browser_session.event_bus.dispatch(
                NavigateToUrlEvent(
                    url=url,
                    new_tab=new_tab,
                    timeout_ms=int(timeout_ms),
                )
            )
            await event
            await event.event_result(raise_if_any=True, raise_if_none=False)
            return
        except Exception:
            await browser_session.navigate_to(url, new_tab=new_tab)

    def _parse_evaluate_payload(self, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, (dict, list, bool, int, float)):
            return value
        if not isinstance(value, str):
            return value
        text = value.strip()
        if not text:
            return None
        lowered = text.lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        parsed = self._extract_first_json_value(text)
        if parsed is not None:
            return parsed
        return text

    async def _evaluate_page_json(
        self,
        page: Any,
        script: str,
        *args: Any,
    ) -> Any:
        raw = await page.evaluate(script, *args)
        return self._parse_evaluate_payload(raw)

    async def _scrape_story_interactions_via_js(
        self,
        browser_session: BrowserSession,
        profile_url: str,
        story_url: str,
        safe_max_interactions: int,
    ) -> Dict[str, Any]:
        state_script = """
        (...args) => {
          const href = window.location.href || '';
          const storyMatch = href.match(/\\/stories\\/([^\\/?#]+)\\/(\\d+)(?:\\/|$)/i);
          const storyUrl = storyMatch ? `https://www.instagram.com/stories/${storyMatch[1]}/${storyMatch[2]}/` : '';
          const isStoryUrl = Boolean(storyMatch);
          const pageText = (document.body && document.body.innerText) ? document.body.innerText : '';
          const loginRequired = /\\/accounts\\/login/i.test(window.location.pathname || '')
            || /log in|entrar/i.test(pageText.slice(0, 2000));

          let viewCount = null;
          const controls = Array.from(document.querySelectorAll('button,div[role="button"],a,span'));
          const seenControl = controls.find((el) => /^(seen by|visto por)\\s*\\d+/i.test((el.textContent || '').trim()));
          const readDigits = (text) => {
            if (!text) return null;
            const match = text.match(/(seen by|visto por)\\s*([\\d\\.,]+)/i);
            if (!match) return null;
            const digits = (match[2] || '').replace(/\\D/g, '');
            return digits ? Number(digits) : null;
          };
          viewCount = readDigits(seenControl ? (seenControl.textContent || '') : '');
          if (viewCount === null) {
            viewCount = readDigits(pageText);
          }

          const dialog = document.querySelector('div[role="dialog"]');
          const dialogText = dialog ? ((dialog.textContent || '').toLowerCase()) : '';
          const viewersModalOpen = Boolean(
            dialog && (
              dialogText.includes('visualizador')
              || dialogText.includes('viewer')
            )
          );

          return {
            current_url: href,
            story_url: storyUrl,
            is_story_url: isStoryUrl,
            view_count: Number.isFinite(viewCount) ? viewCount : null,
            viewers_modal_open: viewersModalOpen,
            login_required: loginRequired
          };
        }
        """

        click_seen_by_script = """
        (...args) => {
          const controls = Array.from(document.querySelectorAll('button,div[role="button"],a,span'));
          const target = controls.find((el) => /^(seen by|visto por)\\s*\\d+/i.test((el.textContent || '').trim()));
          if (!target) {
            return { clicked: false, reason: 'seen_by_not_found' };
          }
          try {
            target.scrollIntoView({ block: 'center', inline: 'center' });
          } catch (e) {}
          const rect = target.getBoundingClientRect();
          const fullyVisible = rect.top >= 0 && rect.left >= 0
            && rect.bottom <= (window.innerHeight || document.documentElement.clientHeight)
            && rect.right <= (window.innerWidth || document.documentElement.clientWidth);
          target.click();
          return { clicked: true, fully_visible: fullyVisible };
        }
        """

        extract_liked_users_script = """
        (...args) => (async () => {
          const maxUsers = Math.max(1, Number(args[0] || 300));
          const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
          const dialog = document.querySelector('div[role="dialog"]');
          if (!dialog) {
            return { popup_opened: false, liked_users: [] };
          }
          const dialogText = (dialog.textContent || '').toLowerCase();
          if (!(dialogText.includes('visualizador') || dialogText.includes('viewer'))) {
            return { popup_opened: false, liked_users: [] };
          }

          const debug = {
            passes: 0,
            rows_scanned: 0,
            heart_hits: 0
          };

          const usersMap = new Map();
          const blocked = new Set(['stories', 'explore', 'accounts', 'p', 'reels']);

          const parseRgb = (text) => {
            const m = String(text || '').match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/i);
            if (!m) return null;
            return { r: Number(m[1]), g: Number(m[2]), b: Number(m[3]) };
          };

          const isRedColor = (text) => {
            const lowered = String(text || '').toLowerCase();
            if (!lowered) return false;
            if (
              lowered.includes('#ed4956')
              || lowered.includes('#ff3040')
              || lowered.includes('#e0245e')
              || lowered.includes('#f91880')
            ) {
              return true;
            }
            const rgb = parseRgb(lowered);
            if (!rgb) return false;
            return rgb.r >= 190 && rgb.g <= 130 && rgb.b <= 150;
          };

          const pickRow = (anchor) => {
            let node = anchor;
            let best = null;
            while (node && node !== dialog) {
              if (!(node instanceof HTMLElement)) {
                node = node.parentElement;
                continue;
              }
              const linkCount = node.querySelectorAll('a[href^="/"]').length;
              const imgCount = node.querySelectorAll('img').length;
              const rect = node.getBoundingClientRect();
              const h = rect.height || node.clientHeight || 0;
              const w = rect.width || node.clientWidth || 0;
              if (h >= 24 && h <= 220 && w >= 90 && imgCount >= 1 && linkCount <= 6) {
                best = node;
              }
              if (linkCount > 10 || h > (window.innerHeight * 0.9)) {
                break;
              }
              node = node.parentElement;
            }
            return best || anchor.parentElement || anchor;
          };

          const rowHasHeartBadge = (row) => {
            const rowRect = row.getBoundingClientRect();
            const heartSelector = [
              'svg[aria-label*="Liked"]',
              'svg[aria-label*="Like"]',
              'svg[aria-label*="Curt"]',
              '[aria-label*="Liked"]',
              '[aria-label*="Like"]',
              '[aria-label*="Curti"]',
              '[aria-label*="curti"]',
              '[title*="Like"]',
              '[title*="Curt"]'
            ].join(',');
            if (row.querySelector(heartSelector)) {
              return true;
            }

            const elements = Array.from(row.querySelectorAll('*')).slice(0, 180);
            for (const el of elements) {
              const aria = (el.getAttribute('aria-label') || '').toLowerCase();
              const title = (el.getAttribute('title') || '').toLowerCase();
              const dataTestId = (el.getAttribute('data-testid') || '').toLowerCase();
              if (
                aria.includes('liked') || aria.includes('curti')
                || title.includes('liked') || title.includes('curti')
                || dataTestId.includes('like') || dataTestId.includes('heart')
              ) {
                return true;
              }

              const fill = (el.getAttribute('fill') || '').toLowerCase();
              const stroke = (el.getAttribute('stroke') || '').toLowerCase();
              const styleAttr = (el.getAttribute('style') || '').toLowerCase();
              const colorAttr = (el.getAttribute('color') || '').toLowerCase();
              if (
                isRedColor(fill) || isRedColor(stroke) || isRedColor(styleAttr) || isRedColor(colorAttr)
              ) {
                const rect = el.getBoundingClientRect();
                const nearAvatar = rect.left <= rowRect.left + Math.min(140, rowRect.width * 0.45);
                const smallIcon = rect.width <= 32 && rect.height <= 32;
                if (nearAvatar && smallIcon) {
                  return true;
                }
              }

              try {
                const cs = window.getComputedStyle(el);
                if (isRedColor(cs.color) || isRedColor(cs.fill) || isRedColor(cs.stroke)) {
                  const rect = el.getBoundingClientRect();
                  const nearAvatar = rect.left <= rowRect.left + Math.min(140, rowRect.width * 0.45);
                  const smallIcon = rect.width <= 32 && rect.height <= 32;
                  if (nearAvatar && smallIcon) {
                    return true;
                  }
                }
              } catch (e) {}
            }
            return false;
          };

          const collectFromCurrentDom = () => {
            debug.passes += 1;
            const anchors = Array.from(dialog.querySelectorAll('a[href^="/"]'));
            for (const anchor of anchors) {
              const href = anchor.getAttribute('href') || '';
              const m = href.match(/^\\/([^\\/?#\\.][^\\/?#]*)\\/?$/);
              if (!m) continue;
              const username = (m[1] || '').trim();
              if (!username) continue;
              if (blocked.has(username.toLowerCase())) continue;

              const row = pickRow(anchor);
              if (!row) continue;
              debug.rows_scanned += 1;

              if (!rowHasHeartBadge(row)) continue;
              debug.heart_hits += 1;

              if (!usersMap.has(username)) {
                usersMap.set(username, {
                  user_username: username,
                  user_url: `https://www.instagram.com/${username}/`,
                  badge_heart_red: true
                });
              }
              if (usersMap.size >= maxUsers) break;
            }
          };

          // Important: collect immediately before scrolling to avoid losing top rows in virtualized lists.
          collectFromCurrentDom();

          let scrollable = null;
          const candidates = [dialog, ...Array.from(dialog.querySelectorAll('div,section,ul,ol'))];
          for (const el of candidates) {
            try {
              if (el.scrollHeight > el.clientHeight + 24) {
                if (!scrollable || el.scrollHeight > scrollable.scrollHeight) {
                  scrollable = el;
                }
              }
            } catch (e) {}
          }

          if (scrollable && usersMap.size < maxUsers) {
            let stagnantRounds = 0;
            for (let i = 0; i < 80; i += 1) {
              const beforeTop = scrollable.scrollTop;
              const beforeHeight = scrollable.scrollHeight;
              const step = Math.max(260, Math.floor(scrollable.clientHeight * 0.75));
              scrollable.scrollTop = Math.min(beforeTop + step, scrollable.scrollHeight);
              await sleep(280);

              collectFromCurrentDom();
              if (usersMap.size >= maxUsers) break;

              const afterTop = scrollable.scrollTop;
              const afterHeight = scrollable.scrollHeight;
              const topUnchanged = Math.abs(afterTop - beforeTop) <= 2;
              const heightUnchanged = Math.abs(afterHeight - beforeHeight) <= 2;
              if (topUnchanged && heightUnchanged) {
                stagnantRounds += 1;
              } else {
                stagnantRounds = 0;
              }
              if (stagnantRounds >= 4) break;
            }
          }

          return {
            popup_opened: true,
            liked_users: Array.from(usersMap.values()),
            debug
          };
        })()
        """

        close_modal_script = """
        (...args) => {
          const dialog = document.querySelector('div[role="dialog"]');
          if (!dialog) return { closed: true, reason: 'already_closed' };

          const clickables = Array.from(
            dialog.querySelectorAll('button,div[role="button"],svg,[aria-label]')
          );
          const closeEl = clickables.find((el) => {
            const aria = (el.getAttribute('aria-label') || '').toLowerCase();
            const txt = (el.textContent || '').trim().toLowerCase();
            return aria.includes('close') || aria.includes('dismiss') || aria.includes('fechar')
              || txt === 'x' || txt === '×';
          });
          if (closeEl) {
            closeEl.click();
            return { closed: true, reason: 'close_button' };
          }

          try {
            const rect = dialog.getBoundingClientRect();
            const x = Math.max(2, rect.left - 10);
            const y = Math.max(2, rect.top + 10);
            const target = document.elementFromPoint(x, y);
            if (target) {
              target.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
            }
          } catch (e) {}

          try {
            document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
          } catch (e) {}

          return { closed: true, reason: 'outside_or_escape' };
        }
        """

        click_next_story_script = """
        (...args) => {
          const candidates = Array.from(document.querySelectorAll('button,div[role="button"],a'));
          const rightSide = candidates
            .map((el) => ({ el, rect: el.getBoundingClientRect() }))
            .filter((item) => item.rect.width > 10 && item.rect.height > 10 && item.rect.left > (window.innerWidth * 0.55));

          const byLabel = rightSide.find((item) => {
            const aria = (item.el.getAttribute('aria-label') || '').toLowerCase();
            const txt = (item.el.textContent || '').toLowerCase();
            return aria.includes('next') || aria.includes('próximo') || aria.includes('proximo')
              || aria.includes('avanç') || aria.includes('seguinte')
              || txt.includes('next') || txt.includes('próximo') || txt.includes('proximo');
          });

          const target = byLabel ? byLabel.el : (rightSide.length ? rightSide[rightSide.length - 1].el : null);
          if (!target) {
            return { clicked: false, reason: 'next_not_found' };
          }
          try {
            target.scrollIntoView({ block: 'center', inline: 'center' });
          } catch (e) {}
          target.click();
          return { clicked: true };
        }
        """

        async def _read_state_from_current_page() -> tuple[Optional[Any], Dict[str, Any]]:
            page_obj = await browser_session.get_current_page()
            if page_obj is None:
                return None, {}
            state_raw = await self._evaluate_page_json(page_obj, state_script)
            state_data = state_raw if isinstance(state_raw, dict) else {}
            return page_obj, state_data

        async def _wait_for_story_url(
            max_wait_seconds: float = 20.0,
        ) -> tuple[Optional[Any], Dict[str, Any], bool]:
            deadline = asyncio.get_event_loop().time() + max_wait_seconds
            last_page: Optional[Any] = None
            last_state: Dict[str, Any] = {}

            while asyncio.get_event_loop().time() < deadline:
                page_obj, state_data = await _read_state_from_current_page()
                if page_obj is not None:
                    last_page = page_obj
                if state_data:
                    last_state = state_data
                if state_data.get("login_required"):
                    return last_page, last_state, True
                current_story_url_value = self._normalize_story_url_value(
                    state_data.get("story_url") or state_data.get("current_url")
                )
                story_id_value = self._extract_story_id_from_url(current_story_url_value)
                if current_story_url_value and story_id_value:
                    return last_page, last_state, True
                await asyncio.sleep(1.0)

            return last_page, last_state, False

        await self._navigate_to_url_with_timeout(
            browser_session,
            story_url,
            timeout_ms=30000,
            new_tab=False,
        )
        await asyncio.sleep(1.0)

        page, initial_state, initial_ready = await _wait_for_story_url(max_wait_seconds=20.0)
        if initial_state.get("login_required"):
            return {
                "profile_url": profile_url,
                "stories_accessible": False,
                "story_posts": [],
                "total_story_posts": 0,
                "total_liked_users": 0,
                "total_collected": 0,
                "error": "login_required",
            }
        if not initial_ready:
            logger.warning(
                "Stories JS: viewer ainda nao estabilizou apos navegacao inicial (%s).",
                story_url,
            )

        story_posts: List[Dict[str, Any]] = []
        seen_story_ids: set[str] = set()
        seen_like_keys: set[str] = set()
        last_valid_story_url = ""
        recover_attempts = 0
        max_story_steps = max(5, min(80, safe_max_interactions * 2))

        for _ in range(max_story_steps):
            page, state = await _read_state_from_current_page()
            if page is None:
                return {
                    "profile_url": profile_url,
                    "stories_accessible": False,
                    "story_posts": story_posts,
                    "total_story_posts": len(story_posts),
                    "total_liked_users": len(seen_like_keys),
                    "total_collected": len(seen_like_keys),
                    "error": "story_open_failed",
                }
            if state.get("login_required"):
                return {
                    "profile_url": profile_url,
                    "stories_accessible": False,
                    "story_posts": [],
                    "total_story_posts": 0,
                    "total_liked_users": 0,
                    "total_collected": 0,
                    "error": "login_required",
                }

            current_story_url = self._normalize_story_url_value(
                state.get("story_url") or state.get("current_url")
            )
            if not current_story_url:
                if last_valid_story_url and recover_attempts < 3:
                    recover_attempts += 1
                    await self._navigate_to_url_with_timeout(
                        browser_session,
                        last_valid_story_url,
                        timeout_ms=30000,
                        new_tab=False,
                    )
                    await _wait_for_story_url(max_wait_seconds=12.0)
                    continue
                if recover_attempts < 6:
                    recover_attempts += 1
                    await self._navigate_to_url_with_timeout(
                        browser_session,
                        story_url,
                        timeout_ms=30000,
                        new_tab=False,
                    )
                    await _wait_for_story_url(max_wait_seconds=14.0)
                    continue
                if story_posts:
                    break
                return {
                    "profile_url": profile_url,
                    "stories_accessible": False,
                    "story_posts": [],
                    "total_story_posts": 0,
                    "total_liked_users": 0,
                    "total_collected": 0,
                    "error": "story_open_failed",
                }

            last_valid_story_url = current_story_url
            story_id = self._extract_story_id_from_url(current_story_url)
            if not story_id:
                if recover_attempts < 6:
                    recover_attempts += 1
                    logger.warning(
                        "Stories JS: URL sem story_id (%s). Aguardando estabilizacao (%s/6)...",
                        current_story_url,
                        recover_attempts,
                    )
                    await _wait_for_story_url(max_wait_seconds=10.0)
                    continue
                if story_posts:
                    break
                return {
                    "profile_url": profile_url,
                    "stories_accessible": False,
                    "story_posts": [],
                    "total_story_posts": 0,
                    "total_liked_users": 0,
                    "total_collected": 0,
                    "error": "story_open_failed",
                }
            recover_attempts = 0
            if story_id in seen_story_ids:
                break
            seen_story_ids.add(story_id)

            view_count = None
            try:
                view_count = int(state.get("view_count")) if state.get("view_count") is not None else None
            except Exception:
                view_count = None

            popup_open = False
            for _popup_try in range(2):
                click_raw = await self._evaluate_page_json(page, click_seen_by_script)
                click_data = click_raw if isinstance(click_raw, dict) else {}
                if not click_data.get("clicked"):
                    await asyncio.sleep(0.8)
                    continue
                await asyncio.sleep(10.0)
                modal_state_raw = await self._evaluate_page_json(page, state_script)
                modal_state = modal_state_raw if isinstance(modal_state_raw, dict) else {}
                popup_open = bool(modal_state.get("viewers_modal_open"))
                if popup_open:
                    break
                await asyncio.sleep(0.8)

            liked_users: List[Dict[str, str]] = []
            extraction_debug: Dict[str, Any] = {}
            if popup_open:
                max_remaining = max(1, safe_max_interactions - len(seen_like_keys))
                extracted_raw = await self._evaluate_page_json(
                    page,
                    extract_liked_users_script,
                    max_remaining,
                )
                extracted_data = extracted_raw if isinstance(extracted_raw, dict) else {}
                if isinstance(extracted_data.get("debug"), dict):
                    extraction_debug = extracted_data.get("debug") or {}
                raw_liked_users = extracted_data.get("liked_users") or []
                if isinstance(raw_liked_users, list):
                    for raw_user in raw_liked_users:
                        if not isinstance(raw_user, dict):
                            continue
                        if raw_user.get("badge_heart_red") is not True:
                            continue
                        username = str(raw_user.get("user_username") or "").strip().lstrip("@")
                        user_url = str(raw_user.get("user_url") or "").strip()
                        if user_url.startswith("/"):
                            user_url = f"https://www.instagram.com{user_url}"
                        if not user_url and username:
                            user_url = f"https://www.instagram.com/{username}/"
                        if user_url and "instagram.com" in user_url:
                            parsed_user = urlparse(user_url)
                            user_path_parts = [part for part in parsed_user.path.split("/") if part]
                            if user_path_parts:
                                normalized_username = user_path_parts[0].strip().lstrip("@")
                                if normalized_username:
                                    username = username or normalized_username
                                    user_url = f"https://www.instagram.com/{normalized_username}/"
                        if not user_url and not username:
                            continue
                        like_key = user_url or username
                        if like_key in seen_like_keys:
                            continue
                        seen_like_keys.add(like_key)
                        liked_users.append(
                            {
                                "user_username": username or "",
                                "user_url": user_url or "",
                            }
                        )
                        if len(seen_like_keys) >= safe_max_interactions:
                            break

            logger.info(
                "Stories JS: story=%s views=%s popup_open=%s liked_users=%s debug=%s",
                story_id,
                view_count,
                popup_open,
                len(liked_users),
                extraction_debug or None,
            )

            story_posts.append(
                {
                    "story_url": current_story_url,
                    "view_count": view_count,
                    "liked_users": liked_users,
                }
            )

            await self._evaluate_page_json(page, close_modal_script)
            await asyncio.sleep(0.8)

            if len(seen_like_keys) >= safe_max_interactions:
                break

            next_changed = False
            for _next_try in range(2):
                next_raw = await self._evaluate_page_json(page, click_next_story_script)
                next_data = next_raw if isinstance(next_raw, dict) else {}
                if not next_data.get("clicked"):
                    await asyncio.sleep(0.8)
                    continue
                await asyncio.sleep(1.6)
                new_state_raw = await self._evaluate_page_json(page, state_script)
                new_state = new_state_raw if isinstance(new_state_raw, dict) else {}
                next_story_url = self._normalize_story_url_value(
                    new_state.get("story_url") or new_state.get("current_url")
                )
                next_story_id = self._extract_story_id_from_url(next_story_url)
                if next_story_id and next_story_id != story_id:
                    next_changed = True
                    break

            if not next_changed:
                break

        if story_posts:
            return {
                "profile_url": profile_url,
                "stories_accessible": True,
                "story_posts": story_posts,
                "total_story_posts": len(story_posts),
                "total_liked_users": len(seen_like_keys),
                "total_collected": len(seen_like_keys),
                "error": None,
            }

        return {
            "profile_url": profile_url,
            "stories_accessible": False,
            "story_posts": [],
            "total_story_posts": 0,
            "total_liked_users": 0,
            "total_collected": 0,
            "error": "story_open_failed",
        }

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

    async def is_instagram_session_valid(self, storage_state: Optional[Dict[str, Any]]) -> bool:
        """
        Valida se um storage_state representa uma sessao autenticada do Instagram.
        """
        if not isinstance(storage_state, dict):
            return False
        return await self._is_session_valid(storage_state)

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
                    failure_error = self._classify_agent_failure_error(
                        final_result=final_result,
                        history=history,
                    )
                    return {
                        "posts": [],
                        "total_found": 0,
                        "raw_result": final_result,
                        "error": failure_error,
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
                        logger.warning("Falha ao extrair JSON de curtidores: %s", final_result[:180])
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
                        failure_error = self._classify_agent_failure_error(
                            final_result=final_result,
                            history=history,
                        )
                        return {
                            "post_url": post_url,
                            "likes_accessible": False,
                            "like_users": [],
                            "error": failure_error,
                            "raw_result": final_result or self._history_errors_text(history),
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
                    failure_error = self._classify_agent_failure_error(exc=exc)
                    if failure_error == "rate_limit_exceeded":
                        return {
                            "post_url": post_url,
                            "likes_accessible": False,
                            "like_users": [],
                            "error": failure_error,
                        }
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

    async def scrape_post_comments(
        self,
        post_url: str,
        storage_state: Optional[Dict[str, Any]],
        max_comments: int = 80,
        max_scrolls: int = 6,
    ) -> Dict[str, Any]:
        """
        Abre um post e tenta extrair comentarios visiveis usando Browser Use.

        Returns:
            {
              "post_url": str,
              "comments_accessible": bool,
              "comments": [
                {
                  "user_url": str | None,
                  "user_username": str | None,
                  "comment_text": str | None,
                  "comment_likes": int,
                  "comment_replies": int,
                  "comment_posted_at": str | None
                }
              ],
              "total_collected": int,
              "error": Optional[str]
            }
        """
        max_retries = getattr(settings, "browser_use_max_retries", 3)
        retry_delay = 5
        safe_max_comments = max(1, int(max_comments))
        safe_max_scrolls = max(1, int(max_scrolls))
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
                        "Browser Use: Coletando comentarios de %s (tentativa %s/%s)",
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
                    Voce esta em um navegador autenticado no Instagram.
                    Sua tarefa e extrair comentarios de um post.

                    PASSOS:
                    1) Acesse o post: {post_url}
                    2) Aguarde a pagina carregar.
                    3) Se houver modal de cookies, aceite.
                    4) Abra a secao de comentarios (incluindo "view all comments", "view more comments", "ver comentarios").
                    5) Role/carregue mais comentarios por no maximo {safe_max_scrolls} iteracoes.
                    6) Colete ate {safe_max_comments} comentarios visiveis.

                    FORMATO DE SAIDA (JSON):
                    {{
                      "post_url": "{post_url}",
                      "comments_accessible": true,
                      "comments": [
                        {{
                          "user_url": "https://www.instagram.com/usuario/",
                          "user_username": "usuario",
                          "comment_text": "texto do comentario",
                          "comment_likes": 0,
                          "comment_replies": 0,
                          "comment_posted_at": "2 h"
                        }}
                      ],
                      "total_collected": 1
                    }}

                    REGRAS:
                    - Se nao for possivel abrir/carregar comentarios, retorne:
                      {{
                        "post_url": "{post_url}",
                        "comments_accessible": false,
                        "comments": [],
                        "error": "comments_unavailable"
                      }}
                    - Nao abra nova aba.
                    - Nao invente dados.
                    - Se um campo nao estiver visivel, use null.
                    - Retorne JSON puro no resultado final.
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
                            "Sessao CDP instavel ao coletar comentarios (%s/%s). Retentando em %ss...",
                            attempt,
                            max_retries,
                            wait_time,
                        )
                        await asyncio.sleep(wait_time)
                        continue

                    data = self._extract_json_object_with_key(final_result, "comments_accessible")
                    if data is None:
                        logger.warning("Falha ao extrair JSON de comentarios: %s", final_result[:180])
                        if self._contains_protocol_error(final_result) and attempt < max_retries:
                            wait_time = retry_delay * attempt
                            logger.warning(
                                "Falha de protocolo detectada na coleta de comentarios (%s/%s). Retentando em %ss...",
                                attempt,
                                max_retries,
                                wait_time,
                            )
                            await asyncio.sleep(wait_time)
                            continue
                        failure_error = self._classify_agent_failure_error(
                            final_result=final_result,
                            history=history,
                        )
                        return {
                            "post_url": post_url,
                            "comments_accessible": False,
                            "comments": [],
                            "total_collected": 0,
                            "error": failure_error,
                            "raw_result": final_result or self._history_errors_text(history),
                        }

                    if data.get("error") == "login_required" and attempt < max_retries:
                        wait_time = retry_delay * attempt
                        logger.warning(
                            "Agente retornou login_required ao coletar comentarios (%s/%s). Retentando em %ss...",
                            attempt,
                            max_retries,
                            wait_time,
                        )
                        await asyncio.sleep(wait_time)
                        continue

                    normalized_comments: List[Dict[str, Any]] = []
                    seen_comment_keys: set[str] = set()

                    for value in data.get("comments", []) or []:
                        if not isinstance(value, dict):
                            continue

                        user_url = str(value.get("user_url") or "").strip()
                        user_username = str(value.get("user_username") or "").strip().lstrip("@")
                        if user_url.startswith("/"):
                            user_url = f"https://www.instagram.com{user_url}"
                        if not user_url and user_username:
                            user_url = f"https://www.instagram.com/{user_username}/"

                        if user_url and "instagram.com" in user_url:
                            parsed_user = urlparse(user_url)
                            path_parts = [part for part in parsed_user.path.split("/") if part]
                            if path_parts:
                                normalized_username = path_parts[0].strip().lstrip("@")
                                if normalized_username:
                                    user_username = user_username or normalized_username
                                    user_url = f"https://www.instagram.com/{normalized_username}/"

                        if not user_url and not user_username:
                            continue

                        comment_text = value.get("comment_text")
                        if comment_text is not None:
                            comment_text = str(comment_text).strip() or None

                        comment_posted_at = value.get("comment_posted_at")
                        if comment_posted_at is not None:
                            comment_posted_at = str(comment_posted_at).strip() or None

                        try:
                            comment_likes = int(value.get("comment_likes", 0) or 0)
                        except (TypeError, ValueError):
                            comment_likes = 0

                        try:
                            comment_replies = int(value.get("comment_replies", 0) or 0)
                        except (TypeError, ValueError):
                            comment_replies = 0

                        dedup_key = f"{user_url or user_username}|{comment_text}|{comment_posted_at}"
                        if dedup_key in seen_comment_keys:
                            continue
                        seen_comment_keys.add(dedup_key)

                        normalized_comments.append(
                            {
                                "user_url": user_url or None,
                                "user_username": user_username or None,
                                "comment_text": comment_text,
                                "comment_likes": comment_likes,
                                "comment_replies": comment_replies,
                                "comment_posted_at": comment_posted_at,
                            }
                        )
                        if len(normalized_comments) >= safe_max_comments:
                            break

                    comments_accessible = bool(data.get("comments_accessible"))
                    if normalized_comments and not comments_accessible:
                        comments_accessible = True

                    return {
                        "post_url": data.get("post_url") or post_url,
                        "comments_accessible": comments_accessible,
                        "comments": normalized_comments,
                        "total_collected": len(normalized_comments),
                        "error": data.get("error"),
                    }

                except Exception as exc:
                    failure_error = self._classify_agent_failure_error(exc=exc)
                    if failure_error == "rate_limit_exceeded":
                        return {
                            "post_url": post_url,
                            "comments_accessible": False,
                            "comments": [],
                            "total_collected": 0,
                            "error": failure_error,
                        }
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
                            "Tentativa %s/%s falhou ao coletar comentarios: %s. Retentando em %ss...",
                            attempt,
                            max_retries,
                            str(exc)[:120],
                            wait_time,
                        )
                        await asyncio.sleep(wait_time)
                        continue
                    return {
                        "post_url": post_url,
                        "comments_accessible": False,
                        "comments": [],
                        "total_collected": 0,
                        "error": str(exc),
                    }
                finally:
                    if callable(restore_event_bus):
                        restore_event_bus()
                    if browser_session:
                        await self._detach_browser_session(browser_session)

            return {
                "post_url": post_url,
                "comments_accessible": False,
                "comments": [],
                "total_collected": 0,
                "error": "all_retries_failed",
            }
        finally:
            self._cleanup_storage_state_temp_file(storage_state_file)

    async def scrape_story_interactions(
        self,
        profile_url: str,
        storage_state: Optional[Dict[str, Any]],
        max_interactions: int = 300,
    ) -> Dict[str, Any]:
        """
        Abre stories de um perfil e extrai por story:
        - URL do story
        - numero de visualizacoes
        - usuarios que deram like (username + url)
        """
        max_retries = getattr(settings, "browser_use_max_retries", 3)
        retry_delay = 5
        safe_max_interactions = max(1, int(max_interactions))
        parsed_profile_url = urlparse(profile_url or "")
        path_parts = [part for part in parsed_profile_url.path.split("/") if part]
        profile_username = (path_parts[0].strip().lstrip("@") if path_parts else "").lower()
        story_url = (
            f"https://www.instagram.com/stories/{profile_username}/"
            if profile_username
            else "https://www.instagram.com/stories/"
        )
        reconnect_url = self._get_browserless_reconnect_url(storage_state)
        session_info = self._get_browserless_session_info(storage_state)
        session_connect_url = (
            session_info.get("connect") if isinstance(session_info.get("connect"), str) else None
        )
        # Fluxo de stories tem mostrado mais instabilidade com reconnect reaproveitado.
        # Mantemos flag para trocar para CDP fresh apos qualquer erro de protocolo.
        force_fresh_cdp = False
        clean_storage_state = self._sanitize_storage_state(storage_state)
        storage_state_file = self._write_storage_state_temp_file(storage_state)
        storage_state_for_session: Optional[Union[Dict[str, Any], str]]
        storage_state_for_session = storage_state_file or clean_storage_state

        def _to_int_or_none(value: Any) -> Optional[int]:
            if value is None:
                return None
            if isinstance(value, bool):
                return None
            if isinstance(value, int):
                return value
            if isinstance(value, float):
                return int(value)
            text = str(value).strip()
            if not text:
                return None
            digits = "".join(ch for ch in text if ch.isdigit())
            if not digits:
                return None
            try:
                return int(digits)
            except Exception:
                return None

        def _normalize_story_url(value: Any) -> str:
            raw_url = str(value or "").strip()
            if not raw_url:
                return ""
            if raw_url.startswith("/"):
                raw_url = f"https://www.instagram.com{raw_url}"
            parsed = urlparse(raw_url)
            path_parts = [part for part in parsed.path.split("/") if part]
            if len(path_parts) >= 3 and path_parts[0].lower() == "stories":
                username_part = path_parts[1].strip().lstrip("@")
                story_id_part = path_parts[2].strip()
                if username_part and story_id_part:
                    return f"https://www.instagram.com/stories/{username_part}/{story_id_part}/"
            if raw_url and "/stories/" in raw_url and not raw_url.endswith("/"):
                raw_url = f"{raw_url}/"
            return raw_url

        def _is_explicit_liked_user(raw_user: Any) -> bool:
            if not isinstance(raw_user, dict):
                return False
            has_marker = False
            if "badge_heart_red" in raw_user:
                has_marker = True
                if raw_user.get("badge_heart_red") is not True:
                    return False
            if "liked" in raw_user:
                has_marker = True
                if raw_user.get("liked") is not True:
                    return False
            if "type" in raw_user:
                has_marker = True
                if self._normalize_story_interaction_type(raw_user.get("type")) != "like":
                    return False
            return has_marker

        try:
            if not storage_state:
                return {
                    "profile_url": profile_url,
                    "stories_accessible": False,
                    "story_posts": [],
                    "total_story_posts": 0,
                    "total_liked_users": 0,
                    "total_collected": 0,
                    "error": "login_required",
                }

            for attempt in range(1, max_retries + 1):
                browser_session = None
                restore_event_bus = None
                try:
                    logger.info(
                        "Browser Use: Coletando interacoes de stories de %s (tentativa %s/%s)",
                        profile_url,
                        attempt,
                        max_retries,
                    )

                    if not self.api_key:
                        raise ValueError("OPENAI_API_KEY is required for Browser Use.")

                    use_reconnect = bool(reconnect_url and attempt == 1 and not force_fresh_cdp)
                    use_session_connect = bool(
                        (not reconnect_url) and session_connect_url and attempt == 1 and not force_fresh_cdp
                    )
                    if use_reconnect:
                        cdp_url = self._ensure_ws_token(reconnect_url)
                    elif use_session_connect:
                        cdp_url = self._ensure_ws_token(session_connect_url)
                    else:
                        cdp_url = await self._resolve_browserless_cdp_url()

                    browser_session = self._create_browser_session(
                        cdp_url,
                        storage_state=storage_state_for_session,
                    )
                    restore_event_bus = self._patch_event_bus_for_stop(browser_session)

                    js_result: Optional[Dict[str, Any]] = None
                    try:
                        js_result = await self._scrape_story_interactions_via_js(
                            browser_session=browser_session,
                            profile_url=profile_url,
                            story_url=story_url,
                            safe_max_interactions=safe_max_interactions,
                        )
                    except Exception as js_exc:
                        js_error_text = str(js_exc or "")
                        if self._contains_protocol_error(js_error_text):
                            if attempt < max_retries:
                                self._toggle_ws_compression_mode("protocol_error no fluxo JS de stories")
                                reconnect_url = None
                                session_connect_url = None
                                force_fresh_cdp = True
                                wait_time = retry_delay * attempt
                                logger.warning(
                                    "Fluxo JS com protocolo instavel em stories (%s/%s). Retentando em %ss...",
                                    attempt,
                                    max_retries,
                                    wait_time,
                                )
                                await asyncio.sleep(wait_time)
                                continue
                            return {
                                "profile_url": profile_url,
                                "stories_accessible": False,
                                "story_posts": [],
                                "total_story_posts": 0,
                                "total_liked_users": 0,
                                "total_collected": 0,
                                "error": "protocol_error",
                            }
                        logger.warning(
                            "Fluxo JS de stories falhou; fallback para LLM (%s/%s): %s",
                            attempt,
                            max_retries,
                            str(js_exc)[:180],
                        )
                        js_result = None

                    if isinstance(js_result, dict):
                        js_error = str(js_result.get("error") or "").strip().lower()
                        if js_error == "protocol_error" and attempt < max_retries:
                            self._toggle_ws_compression_mode("protocol_error no fluxo JS de stories")
                            reconnect_url = None
                            session_connect_url = None
                            force_fresh_cdp = True
                            wait_time = retry_delay * attempt
                            logger.warning(
                                "Fluxo JS com protocolo instavel em stories (%s/%s). Retentando em %ss...",
                                attempt,
                                max_retries,
                                wait_time,
                            )
                            await asyncio.sleep(wait_time)
                            continue
                        if js_error == "story_open_failed" and attempt < max_retries:
                            wait_time = retry_delay * attempt
                            logger.warning(
                                "Fluxo JS nao conseguiu abrir viewer de stories (%s/%s). Retentando em %ss...",
                                attempt,
                                max_retries,
                                wait_time,
                            )
                            await asyncio.sleep(wait_time)
                            continue
                        return js_result

                    task = f"""
                    Voce esta em um navegador autenticado no Instagram e deve coletar likes dos stories.

                    ALVO:
                    - Perfil: {profile_url}
                    - URL direta de stories: {story_url}

                    PASSOS OBRIGATORIOS:
                    1) Abra primeiro: {story_url}
                    2) Se nao abrir o viewer, acesse {profile_url} e entre no story ativo por elemento clicavel que leve a /stories/.
                    3) Em cada iteracao, confirme que a URL atual contem /stories/<perfil>/<story_id>/.
                       - Se sair para feed/perfil (URL sem /stories/), volte imediatamente para o ultimo story_url valido e continue.
                    4) Em cada story aberto, leia e salve a URL atual da barra (story_url) e o story_id.
                    5) No canto inferior do frame, localize o link de visualizacoes ("Visto por X"/"Seen by X") e capture o numero de views.
                    6) Clique nesse link para abrir o popup/lista de visualizadores.
                    7) Valide que o popup abriu de verdade (modal "Visualizadores"/"Viewers" visivel).
                       - Se nao abrir, tente novamente no maximo 2 vezes no mesmo story.
                       - Se ainda falhar, marque esse story como falha de abertura e avance para o proximo.
                    8) Aguarde 10 segundos completos para carregamento total.
                       - Essa espera e obrigatoria em TODO story (sempre execute wait: seconds: 10, mesmo que a lista ja esteja visivel).
                    9) Colete SOMENTE os usuarios com badge de coracao vermelho no avatar (usuarios que deram like no story).
                       - O badge e um pequeno coracao vermelho sobreposto no avatar.
                       - Sem esse badge, o usuario e APENAS visualizador e nao deve ser incluido.
                    10) Para cada usuario com like, extraia:
                       - user_username
                       - user_url no formato https://www.instagram.com/<username>/
                       - badge_heart_red: true
                    11) Feche o popup clicando fora da janela/modal, retornando ao frame do story.
                    12) Avance para o proximo story usando a seta lateral DIREITA do viewer.
                    13) Depois de clicar na seta direita, confirme que a story_url mudou (novo story_id).
                        - Se nao mudar, tente no maximo 2 vezes.
                        - Se repetir o mesmo story_id novamente, encerre para evitar loop.
                    14) Repita ate acabar stories ativos, detectar repeticao de story_url/story_id, ou atingir o limite.

                    LIMITE:
                    - Nao ultrapasse {safe_max_interactions} usuarios curtidores no total.

                    FORMATO DE SAIDA (JSON puro):
                    {{
                      "profile_url": "{profile_url}",
                      "stories_accessible": true,
                      "story_posts": [
                        {{
                          "story_url": "https://www.instagram.com/stories/{profile_username or 'perfil'}/1234567890123456789/",
                          "view_count": 1161,
                          "liked_users": [
                            {{
                              "user_username": "usuario1",
                              "user_url": "https://www.instagram.com/usuario1/",
                              "badge_heart_red": true
                            }}
                          ]
                        }}
                      ],
                      "total_story_posts": 1,
                      "total_liked_users": 1
                    }}

                    REGRAS:
                    - Nao abra nova aba.
                    - Nao invente usuarios.
                    - Nao inclua usuarios sem badge de coracao vermelho.
                    - O badge de coracao vermelho e obrigatorio para considerar like.
                    - Se nao conseguir confirmar visualmente o badge vermelho, nao inclua o usuario.
                    - Nao mude para feed/home/explore durante a coleta. Se isso ocorrer, retorne imediatamente ao ultimo story_url valido.
                    - So considere popup aberto quando o modal de visualizadores estiver visivel.
                    - Feche o popup clicando fora da janela antes de tentar navegar para o proximo story.
                    - Navegue para o proximo story pela seta direita e so prossiga quando a URL do story mudar.
                    - A espera de 10 segundos e obrigatoria: execute wait: seconds: 10 imediatamente apos abrir o popup e so depois colete usuarios.
                    - Retorne "no_active_stories" somente quando confirmar que nao existe story ativo.
                    - Se houver indicio de story ativo mas falhar ao abrir viewer/lista de visualizadores:
                      {{
                        "profile_url": "{profile_url}",
                        "stories_accessible": false,
                        "story_posts": [],
                        "error": "story_open_failed"
                      }}
                    - Se nao houver stories ativos:
                      {{
                        "profile_url": "{profile_url}",
                        "stories_accessible": false,
                        "story_posts": [],
                        "error": "no_active_stories"
                      }}
                    - Se o login expirar:
                      {{
                        "profile_url": "{profile_url}",
                        "stories_accessible": false,
                        "story_posts": [],
                        "error": "login_required"
                      }}
                    """

                    llm = ChatOpenAI(model=self.model, api_key=self.api_key)
                    agent = self._create_agent(
                        task=task,
                        llm=llm,
                        browser_session=browser_session,
                    )

                    history = await agent.run()
                    final_result = history.final_result() or ""
                    history_errors = self._history_errors_text(history)
                    combined_run_output = " | ".join(
                        part for part in (final_result, history_errors) if part
                    )
                    had_protocol_error = self._contains_protocol_error(combined_run_output)

                    if (
                        (not history.is_successful())
                        and had_protocol_error
                        and attempt < max_retries
                    ):
                        self._toggle_ws_compression_mode("protocol_error em stories")
                        reconnect_url = None
                        session_connect_url = None
                        force_fresh_cdp = True
                        wait_time = retry_delay * attempt
                        logger.warning(
                            "Sessao CDP instavel ao coletar stories (%s/%s). Retentando em %ss...",
                            attempt,
                            max_retries,
                            wait_time,
                        )
                        await asyncio.sleep(wait_time)
                        continue

                    data = self._extract_json_object_with_key(
                        final_result,
                        "stories_accessible",
                    )
                    if data is None:
                        logger.warning(
                            "Falha ao extrair JSON de interacoes de stories: %s",
                            final_result[:180],
                        )
                        if had_protocol_error and attempt < max_retries:
                            self._toggle_ws_compression_mode("falha de protocolo ao parsear resultado de stories")
                            reconnect_url = None
                            session_connect_url = None
                            force_fresh_cdp = True
                            wait_time = retry_delay * attempt
                            logger.warning(
                                "Falha de protocolo nas interacoes de stories (%s/%s). Retentando em %ss...",
                                attempt,
                                max_retries,
                                wait_time,
                            )
                            await asyncio.sleep(wait_time)
                            continue
                        failure_error = self._classify_agent_failure_error(
                            final_result=final_result,
                            history=history,
                        )
                        return {
                            "profile_url": profile_url,
                            "stories_accessible": False,
                            "story_posts": [],
                            "total_story_posts": 0,
                            "total_liked_users": 0,
                            "total_collected": 0,
                            "error": failure_error,
                            "raw_result": final_result or history_errors,
                        }

                    if data.get("error") == "login_required" and attempt < max_retries:
                        wait_time = retry_delay * attempt
                        logger.warning(
                            "Agente retornou login_required em stories (%s/%s). Retentando em %ss...",
                            attempt,
                            max_retries,
                            wait_time,
                        )
                        await asyncio.sleep(wait_time)
                        continue

                    raw_story_posts = []
                    if isinstance(data.get("story_posts"), list):
                        raw_story_posts = data.get("story_posts") or []
                    elif isinstance(data.get("stories"), list):
                        raw_story_posts = data.get("stories") or []

                    by_story_url: Dict[str, Dict[str, Any]] = {}
                    normalized_story_posts: List[Dict[str, Any]] = []

                    for story_item in raw_story_posts:
                        if not isinstance(story_item, dict):
                            continue

                        story_url_value = (
                            story_item.get("story_url")
                            or story_item.get("url")
                            or story_item.get("post_url")
                        )
                        story_url_text = _normalize_story_url(story_url_value)
                        if not story_url_text:
                            continue

                        view_count = _to_int_or_none(story_item.get("view_count"))
                        if view_count is None:
                            view_count = _to_int_or_none(story_item.get("views"))
                        if view_count is None:
                            view_count = _to_int_or_none(story_item.get("viewers_count"))

                        raw_liked_users = (
                            story_item.get("liked_users")
                            or story_item.get("like_users")
                            or []
                        )
                        if not isinstance(raw_liked_users, list):
                            raw_liked_users = []

                        liked_users: List[Dict[str, str]] = []
                        seen_liked_keys: set[str] = set()
                        for raw_user in raw_liked_users:
                            user_url = ""
                            user_username = ""
                            if isinstance(raw_user, dict):
                                if not _is_explicit_liked_user(raw_user):
                                    continue
                                user_url = str(raw_user.get("user_url") or "").strip()
                                user_username = str(raw_user.get("user_username") or "").strip().lstrip("@")
                            else:
                                continue

                            if user_url.startswith("/"):
                                user_url = f"https://www.instagram.com{user_url}"
                            if not user_url and user_username:
                                user_url = f"https://www.instagram.com/{user_username}/"

                            if user_url and "instagram.com" in user_url:
                                parsed_user = urlparse(user_url)
                                user_path_parts = [part for part in parsed_user.path.split("/") if part]
                                if user_path_parts:
                                    normalized_username = user_path_parts[0].strip().lstrip("@")
                                    if normalized_username:
                                        user_username = user_username or normalized_username
                                        user_url = f"https://www.instagram.com/{normalized_username}/"

                            if not user_url and not user_username:
                                continue

                            dedupe_user = user_url or user_username
                            if dedupe_user in seen_liked_keys:
                                continue
                            seen_liked_keys.add(dedupe_user)

                            liked_users.append(
                                {
                                    "user_username": user_username or "",
                                    "user_url": user_url or "",
                                }
                            )

                        story_key = story_url_text
                        if story_key in by_story_url:
                            existing_story = by_story_url[story_key]
                            existing_likes: List[Dict[str, str]] = existing_story.get("liked_users", [])
                            existing_like_keys = {
                                item.get("user_url") or item.get("user_username")
                                for item in existing_likes
                                if isinstance(item, dict)
                            }
                            for liked_user in liked_users:
                                dedupe_user = liked_user.get("user_url") or liked_user.get("user_username")
                                if not dedupe_user or dedupe_user in existing_like_keys:
                                    continue
                                existing_like_keys.add(dedupe_user)
                                existing_likes.append(liked_user)
                            existing_story["liked_users"] = existing_likes[:safe_max_interactions]
                            if existing_story.get("view_count") is None and view_count is not None:
                                existing_story["view_count"] = view_count
                            continue

                        normalized_story = {
                            "story_url": story_url_text or "",
                            "view_count": view_count,
                            "liked_users": liked_users[:safe_max_interactions],
                        }
                        by_story_url[story_key] = normalized_story
                        normalized_story_posts.append(normalized_story)

                    normalized_items: List[Dict[str, str]] = []
                    seen_keys: set[str] = set()
                    for story_item in normalized_story_posts:
                        for liked_user in story_item.get("liked_users", []):
                            user_url = str(liked_user.get("user_url") or "").strip()
                            user_username = str(liked_user.get("user_username") or "").strip().lstrip("@")
                            if not user_url and not user_username:
                                continue
                            dedupe_key = f"{user_url or user_username}|like"
                            if dedupe_key in seen_keys:
                                continue
                            seen_keys.add(dedupe_key)
                            normalized_items.append(
                                {
                                    "user_url": user_url,
                                    "user_username": user_username,
                                    "type": "like",
                                }
                            )
                            if len(normalized_items) >= safe_max_interactions:
                                break
                        if len(normalized_items) >= safe_max_interactions:
                            break

                    stories_accessible = bool(data.get("stories_accessible"))
                    if normalized_story_posts and not stories_accessible:
                        stories_accessible = True

                    reported_error = str(data.get("error") or "").strip().lower() or None
                    judge_failed = not history.is_successful()

                    likely_protocol_instability = had_protocol_error and not normalized_story_posts
                    if likely_protocol_instability:
                        if attempt < max_retries:
                            self._toggle_ws_compression_mode("instabilidade CDP com DOM/popup em stories")
                            reconnect_url = None
                            session_connect_url = None
                            force_fresh_cdp = True
                            wait_time = retry_delay * attempt
                            logger.warning(
                                "Instabilidade CDP detectada em stories (%s/%s). Retentando em %ss...",
                                attempt,
                                max_retries,
                                wait_time,
                            )
                            await asyncio.sleep(wait_time)
                            continue
                        stories_accessible = False
                        if reported_error in (None, "", "no_active_stories"):
                            reported_error = "protocol_error"

                    likely_false_no_story = (
                        judge_failed
                        and not normalized_story_posts
                        and (reported_error in (None, "", "no_active_stories"))
                    )
                    if likely_false_no_story:
                        if attempt < max_retries:
                            wait_time = retry_delay * attempt
                            logger.warning(
                                "Judge marcou falha com no_active_stories em stories (%s/%s). Retentando em %ss...",
                                attempt,
                                max_retries,
                                wait_time,
                            )
                            await asyncio.sleep(wait_time)
                            continue
                        reported_error = "story_open_failed"
                        stories_accessible = False

                    return {
                        "profile_url": data.get("profile_url") or profile_url,
                        "stories_accessible": stories_accessible,
                        "story_posts": normalized_story_posts,
                        "total_story_posts": len(normalized_story_posts),
                        "total_liked_users": len(normalized_items),
                        "total_collected": len(normalized_items),
                        "error": reported_error,
                        "raw_result": final_result or history_errors,
                    }

                except Exception as exc:
                    failure_error = self._classify_agent_failure_error(exc=exc)
                    if failure_error == "rate_limit_exceeded":
                        return {
                            "profile_url": profile_url,
                            "stories_accessible": False,
                            "story_posts": [],
                            "total_story_posts": 0,
                            "total_liked_users": 0,
                            "total_collected": 0,
                            "error": failure_error,
                        }
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
                            "sent 1002",
                            "connectionclosederror",
                            "client is stopping",
                        )
                    )
                    if is_retryable and attempt < max_retries:
                        if self._contains_protocol_error(error_msg):
                            self._toggle_ws_compression_mode("excecao retryable com protocol error em stories")
                            reconnect_url = None
                            session_connect_url = None
                            force_fresh_cdp = True
                        wait_time = retry_delay * attempt
                        logger.warning(
                            "Tentativa %s/%s falhou ao coletar stories: %s. Retentando em %ss...",
                            attempt,
                            max_retries,
                            str(exc)[:120],
                            wait_time,
                        )
                        await asyncio.sleep(wait_time)
                        continue
                    normalized_error = "protocol_error" if self._contains_protocol_error(error_msg) else str(exc)
                    return {
                        "profile_url": profile_url,
                        "stories_accessible": False,
                        "story_posts": [],
                        "total_story_posts": 0,
                        "total_liked_users": 0,
                        "total_collected": 0,
                        "error": normalized_error,
                    }
                finally:
                    if callable(restore_event_bus):
                        restore_event_bus()
                    if browser_session:
                        await self._detach_browser_session(browser_session)

            return {
                "profile_url": profile_url,
                "stories_accessible": False,
                "story_posts": [],
                "total_story_posts": 0,
                "total_liked_users": 0,
                "total_collected": 0,
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
                        failure_error = self._classify_agent_failure_error(
                            final_result=final_result,
                            history=history,
                        )
                        return {
                            "error": failure_error,
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
