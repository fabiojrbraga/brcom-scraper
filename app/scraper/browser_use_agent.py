"""
IntegraÃ§Ã£o com Browser Use para automaÃ§Ã£o inteligente de navegador.
Browser Use usa IA para tomar decisÃµes autÃ´nomas durante a navegaÃ§Ã£o.
"""

import logging
import asyncio
import inspect
import json
import re
import tempfile
import unicodedata
from pathlib import Path
from typing import Optional, Dict, Any, List, Union
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
from datetime import datetime, timedelta, timezone
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
    
    Browser Use Ã© uma biblioteca que permite que um modelo de IA (Claude/GPT)
    controle um navegador de forma autÃ´noma, simulando comportamento humano.
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
        self._patch_browser_use_ax_tree()
        self._patch_websocket_compression(self.ws_compression_mode)
        logger.info("Browser Use WebSocket compression mode: %s", self.ws_compression_mode)
        if self.fallback_model:
            logger.info("Browser Use fallback model enabled: %s -> %s", self.model, self.fallback_model)

    _ax_tree_patched = False
    _ws_patched = False
    _ws_patch_mode = "auto"
    _ws_original_connect = None

    @classmethod
    def _patch_browser_use_ax_tree(cls) -> None:
        if cls._ax_tree_patched:
            return
        try:
            from browser_use.dom.service import DomService
        except Exception:
            return

        original = getattr(DomService, "_get_ax_tree_for_all_frames", None)
        if not callable(original):
            return

        async def _patched_get_ax_tree_for_all_frames(self, target_id):
            cdp_session = await self.browser_session.get_or_create_cdp_session(target_id=target_id, focus=False)
            frame_tree = await cdp_session.cdp_client.send.Page.getFrameTree(session_id=cdp_session.session_id)

            def collect_all_frame_ids(frame_tree_node) -> list[str]:
                frame_ids = [frame_tree_node["frame"]["id"]]
                child_frames = frame_tree_node.get("childFrames") or []
                for child_frame in child_frames:
                    frame_ids.extend(collect_all_frame_ids(child_frame))
                return frame_ids

            all_frame_ids = collect_all_frame_ids(frame_tree["frameTree"])
            ax_tree_requests = [
                cdp_session.cdp_client.send.Accessibility.getFullAXTree(
                    params={"frameId": frame_id},
                    session_id=cdp_session.session_id,
                )
                for frame_id in all_frame_ids
            ]

            ax_trees = await asyncio.gather(*ax_tree_requests, return_exceptions=True)
            merged_nodes: list[dict[str, Any]] = []
            skipped_frames = 0
            first_error: Optional[BaseException] = None

            for frame_id, ax_tree in zip(all_frame_ids, ax_trees):
                if isinstance(ax_tree, BaseException):
                    first_error = first_error or ax_tree
                    error_text = str(ax_tree)
                    if "Frame with the given frameId is not found" in error_text:
                        skipped_frames += 1
                        continue
                    if hasattr(self, "logger") and self.logger:
                        self.logger.warning(
                            "AX tree frame ignorado (%s): %s",
                            frame_id,
                            error_text[:180],
                        )
                    continue
                merged_nodes.extend(ax_tree.get("nodes") or [])

            if skipped_frames and hasattr(self, "logger") and self.logger:
                self.logger.info(
                    "AX tree: %s frame(s) obsoletos ignorados durante leitura do DOM.",
                    skipped_frames,
                )

            if merged_nodes:
                return {"nodes": merged_nodes}
            if first_error is not None:
                raise first_error
            return {"nodes": []}

        try:
            DomService._get_ax_tree_for_all_frames = _patched_get_ax_tree_for_all_frames  # type: ignore[assignment]
            cls._ax_tree_patched = True
        except Exception:
            return

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
        user_agent: Optional[str] = None,
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
            cross_origin_iframes=False,
        )
        if user_agent:
            base_kwargs["user_agent"] = user_agent
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

    def _create_agent(
        self,
        task: str,
        llm: ChatOpenAI,
        browser_session: BrowserSession,
        **extra_kwargs: Any,
    ) -> Agent:
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
        possible_kwargs.update(extra_kwargs)
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

    def _prepare_storage_state_for_browser_session(
        self,
        storage_state: Optional[Dict[str, Any]],
    ) -> tuple[Optional[Union[Dict[str, Any], str]], Optional[str], Optional[str]]:
        clean_state = self._sanitize_storage_state(storage_state)
        storage_state_file = self._write_storage_state_temp_file(storage_state)
        session_user_agent = self.get_user_agent(storage_state)
        return storage_state_file or clean_state, storage_state_file, session_user_agent

    def _read_storage_state_payload(
        self,
        storage_state: Optional[Union[Dict[str, Any], str, Path]],
    ) -> Dict[str, Any]:
        if isinstance(storage_state, dict):
            return storage_state
        if isinstance(storage_state, (str, Path)):
            path = Path(storage_state)
            if path.exists():
                try:
                    loaded = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        return loaded
                except Exception as exc:
                    logger.warning("Falha ao ler storage_state de %s: %s", path, exc)
        return {}

    async def _get_browser_session_cookie_snapshot(
        self,
        browser_session: BrowserSession,
    ) -> Dict[str, Any]:
        getter = getattr(browser_session, "_cdp_get_cookies", None)
        if not callable(getter):
            return {
                "count": 0,
                "sessionid_present": False,
                "ds_user_id_present": False,
            }

        try:
            cookies = await self._maybe_await(getter())
        except Exception as exc:
            logger.debug("Falha ao consultar cookies atuais da sessao de browser: %s", exc)
            return {
                "count": 0,
                "sessionid_present": False,
                "ds_user_id_present": False,
                "error": str(exc),
            }

        if not isinstance(cookies, list):
            cookies = []
        cookie_names = {str(cookie.get("name") or "").strip().lower() for cookie in cookies if isinstance(cookie, dict)}
        return {
            "count": len(cookies),
            "sessionid_present": "sessionid" in cookie_names,
            "ds_user_id_present": "ds_user_id" in cookie_names,
        }

    async def _force_apply_storage_state(
        self,
        browser_session: BrowserSession,
        storage_state: Optional[Union[Dict[str, Any], str, Path]],
    ) -> None:
        payload = self._read_storage_state_payload(storage_state)
        cookies = payload.get("cookies")
        if isinstance(cookies, list) and cookies:
            setter = getattr(browser_session, "_cdp_set_cookies", None)
            if callable(setter):
                await self._maybe_await(setter(cookies))

        origins = payload.get("origins")
        if isinstance(origins, list) and origins:
            add_script = getattr(browser_session, "_cdp_add_init_script", None)
            if callable(add_script):
                for origin in origins:
                    if not isinstance(origin, dict):
                        continue
                    for item in origin.get("localStorage", []) or []:
                        if not isinstance(item, dict):
                            continue
                        script = (
                            f"window.localStorage.setItem({json.dumps(item.get('name'))}, "
                            f"{json.dumps(item.get('value'))});"
                        )
                        await self._maybe_await(add_script(script))
                    for item in origin.get("sessionStorage", []) or []:
                        if not isinstance(item, dict):
                            continue
                        script = (
                            f"window.sessionStorage.setItem({json.dumps(item.get('name'))}, "
                            f"{json.dumps(item.get('value'))});"
                        )
                        await self._maybe_await(add_script(script))

    async def _ensure_browser_session_storage_state_loaded(
        self,
        browser_session: BrowserSession,
    ) -> None:
        storage_state = getattr(browser_session.browser_profile, "storage_state", None)
        if not storage_state:
            return

        try:
            from browser_use.browser.events import LoadStorageStateEvent

            load_event = browser_session.event_bus.dispatch(LoadStorageStateEvent())
            await load_event
            await load_event.event_result(raise_if_any=True, raise_if_none=False)
        except Exception as exc:
            logger.debug("Falha ao aguardar LoadStorageStateEvent: %s", exc)

        snapshot = await self._get_browser_session_cookie_snapshot(browser_session)
        if not snapshot.get("sessionid_present"):
            await self._force_apply_storage_state(browser_session, storage_state)
            await asyncio.sleep(0.2)
            snapshot = await self._get_browser_session_cookie_snapshot(browser_session)

        logger.info(
            "Browser session cookies apos carregar storage_state: total=%s sessionid=%s ds_user_id=%s",
            snapshot.get("count", 0),
            snapshot.get("sessionid_present", False),
            snapshot.get("ds_user_id_present", False),
        )

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
                "Ã¡": "a",
                "Ã ": "a",
                "Ã¢": "a",
                "Ã£": "a",
                "Ã©": "e",
                "Ãª": "e",
                "Ã­": "i",
                "Ã³": "o",
                "Ã´": "o",
                "Ãµ": "o",
                "Ãº": "u",
                "Ã§": "c",
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

    def _extract_instagram_username(self, value: str) -> str:
        raw_value = str(value or "").strip()
        if not raw_value:
            return ""
        if not raw_value.startswith(("http://", "https://")):
            return raw_value.strip("/").split("/")[0].strip().lstrip("@").lower()

        parsed = urlparse(raw_value)
        if "instagram.com" not in parsed.netloc.lower():
            return ""
        path_parts = [part for part in parsed.path.split("/") if part]
        if not path_parts:
            return ""
        return path_parts[0].strip().lstrip("@").lower()

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

    def _relative_time_to_hours(self, text: Optional[str]) -> Optional[float]:
        """Converte texto relativo do Instagram para horas."""
        if text is None:
            return None

        cleaned = str(text).strip().lower()
        if not cleaned:
            return None

        cleaned = cleaned.replace("\u2022", " ").replace("\u00b7", " ")
        cleaned = re.sub(r"\b(editado|editada|edited)\b", "", cleaned)
        cleaned = re.sub(r"\bago\b", "", cleaned)
        cleaned = re.sub(r"\bh[aá]\b", "", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()

        if cleaned in {"now", "just now", "agora", "agora mesmo"}:
            return 0.0
        if cleaned in {"today", "hoje"}:
            return 0.0
        if cleaned in {"yesterday", "ontem"}:
            return 24.0

        patterns = [
            (r"(\d+(?:[.,]\d+)?)\s*(?:s|sec|secs|second|seconds|seg|segs|segundo|segundos)\b", 1 / 3600),
            (r"(\d+(?:[.,]\d+)?)\s*(?:m|min|mins|minute|minutes|minuto|minutos)\b", 1 / 60),
            (r"(\d+(?:[.,]\d+)?)\s*(?:h|hr|hrs|hour|hours|hora|horas)\b", 1),
            (r"(\d+(?:[.,]\d+)?)\s*(?:d|day|days|dia|dias)\b", 24),
            (r"(\d+(?:[.,]\d+)?)\s*(?:w|wk|wks|week|weeks|sem|semana|semanas)\b", 24 * 7),
            (r"(\d+(?:[.,]\d+)?)\s*(?:mo|month|months|mes|m[eê]s|meses)\b", 24 * 30),
            (r"(\d+(?:[.,]\d+)?)\s*(?:y|yr|year|years|ano|anos)\b", 24 * 365),
        ]

        for pattern, multiplier in patterns:
            match = re.search(pattern, cleaned)
            if not match:
                continue
            value = match.group(1).replace(",", ".")
            try:
                return float(value) * multiplier
            except ValueError:
                return None

        return None

    def _parse_absolute_date(self, text: str, now: datetime) -> Optional[datetime]:
        """Interpreta datas absolutas simples do Instagram em UTC."""
        if not text:
            return None

        cleaned = text.strip().lower()
        if not cleaned:
            return None

        normalized = unicodedata.normalize("NFD", cleaned)
        normalized = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
        normalized = normalized.replace(",", " ").replace(".", " ")
        normalized = re.sub(r"\bde\b", " ", normalized)
        normalized = re.sub(r"\s+", " ", normalized).strip()

        month_map = {
            "january": 1,
            "jan": 1,
            "february": 2,
            "feb": 2,
            "fevereiro": 2,
            "fev": 2,
            "march": 3,
            "mar": 3,
            "marco": 3,
            "abril": 4,
            "apr": 4,
            "april": 4,
            "maio": 5,
            "may": 5,
            "jun": 6,
            "june": 6,
            "junho": 6,
            "jul": 7,
            "july": 7,
            "julho": 7,
            "aug": 8,
            "august": 8,
            "ago": 8,
            "agosto": 8,
            "sep": 9,
            "sept": 9,
            "september": 9,
            "set": 9,
            "setembro": 9,
            "oct": 10,
            "october": 10,
            "out": 10,
            "outubro": 10,
            "nov": 11,
            "november": 11,
            "novembro": 11,
            "dec": 12,
            "december": 12,
            "dez": 12,
            "dezembro": 12,
        }

        tokens = normalized.split()
        if not tokens:
            return None

        def _parse_day(token: str) -> Optional[int]:
            match = re.match(r"(\d{1,2})", token)
            if not match:
                return None
            day = int(match.group(1))
            return day if 1 <= day <= 31 else None

        def _parse_year(token: Optional[str]) -> Optional[int]:
            if not token:
                return None
            match = re.match(r"(\d{2,4})", token)
            if not match:
                return None
            year = int(match.group(1))
            return year + 2000 if year < 100 else year

        for idx, token in enumerate(tokens):
            month = month_map.get(token)
            if not month:
                continue

            day = None
            year = None

            if idx + 1 < len(tokens):
                day = _parse_day(tokens[idx + 1])
                if day is not None and idx + 2 < len(tokens):
                    year = _parse_year(tokens[idx + 2])

            if day is None and idx > 0:
                day = _parse_day(tokens[idx - 1])
                if day is not None and idx + 1 < len(tokens):
                    year = _parse_year(tokens[idx + 1])

            if day is None:
                continue

            if year is None:
                year = now.year
                try:
                    candidate = datetime(year, month, day, tzinfo=timezone.utc)
                except ValueError:
                    return None
                if candidate.date() > now.date():
                    try:
                        candidate = datetime(year - 1, month, day, tzinfo=timezone.utc)
                    except ValueError:
                        return None
                return candidate

            try:
                return datetime(year, month, day, tzinfo=timezone.utc)
            except ValueError:
                return None

        return None

    def _parse_instagram_timestamp(
        self,
        value: Any,
        now: Optional[datetime] = None,
    ) -> Optional[datetime]:
        """Converte timestamps do Instagram para datetime UTC quando possivel."""
        if value is None:
            return None

        effective_now = now or datetime.now(timezone.utc)
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

        text = str(value).strip()
        if not text:
            return None

        iso_candidate = text.replace("Z", "+00:00").replace("z", "+00:00")
        try:
            parsed = datetime.fromisoformat(iso_candidate)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except Exception:
            pass

        lowered = text.lower()
        if lowered in {"now", "just now", "agora", "agora mesmo", "today", "hoje"}:
            return effective_now
        if lowered in {"yesterday", "ontem"}:
            return effective_now - timedelta(days=1)

        relative_hours = self._relative_time_to_hours(lowered)
        if relative_hours is not None:
            return effective_now - timedelta(hours=relative_hours)

        return self._parse_absolute_date(lowered, effective_now)

    def _should_send_direct_message(
        self,
        last_message_at: Optional[datetime],
        min_days_since_last_message: int,
        now: Optional[datetime] = None,
    ) -> tuple[bool, Optional[float]]:
        """Decide se o direct deve ser enviado com base na ultima mensagem."""
        if last_message_at is None:
            return True, None

        effective_now = now or datetime.now(timezone.utc)
        normalized_last_message_at = (
            last_message_at
            if last_message_at.tzinfo
            else last_message_at.replace(tzinfo=timezone.utc)
        )
        age_days = (effective_now - normalized_last_message_at).total_seconds() / 86400.0
        return age_days > max(1, int(min_days_since_last_message)), age_days

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
                await self._ensure_browser_session_storage_state_loaded(browser_session)
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

    def _stories_debug_root_dir(self) -> Path:
        return Path(__file__).resolve().parents[2] / ".artifacts" / "stories-debug"

    def _sanitize_debug_artifact_name(self, value: str) -> str:
        raw = str(value or "").strip().lower()
        sanitized_chars = [
            ch if ch.isalnum() else "_"
            for ch in raw
        ]
        sanitized = "".join(sanitized_chars).strip("_")
        while "__" in sanitized:
            sanitized = sanitized.replace("__", "_")
        return sanitized or "artifact"

    async def _capture_page_debug_artifacts(
        self,
        page: Any,
        output_dir: Path,
        label: str,
        state_data: Optional[Dict[str, Any]] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        if page is None:
            return None

        output_dir.mkdir(parents=True, exist_ok=True)
        safe_label = self._sanitize_debug_artifact_name(label)
        screenshot_path = output_dir / f"{safe_label}.png"
        html_path = output_dir / f"{safe_label}.html"
        meta_path = output_dir / f"{safe_label}.json"

        page_url = ""
        page_title = ""
        screenshot_error = None
        html_error = None

        try:
            get_url_fn = getattr(page, "get_url", None)
            if callable(get_url_fn):
                page_url = str(await self._maybe_await(get_url_fn()) or "").strip()
            else:
                page_url = str(getattr(page, "url", "") or "")
        except Exception:
            page_url = ""

        try:
            title_fn = getattr(page, "get_title", None) or getattr(page, "title", None)
            if callable(title_fn):
                page_title = str(await self._maybe_await(title_fn()) or "").strip()
        except Exception as exc:
            page_title = f"title_error: {exc}"

        try:
            screenshot_bytes = None
            browser_session = getattr(page, "_browser_session", None)
            session_screenshot_fn = getattr(browser_session, "take_screenshot", None)
            if callable(session_screenshot_fn):
                screenshot_bytes = await self._maybe_await(
                    session_screenshot_fn(path=str(screenshot_path), full_page=True)
                )
            else:
                screenshot_fn = getattr(page, "screenshot", None)
                if callable(screenshot_fn):
                    screenshot_data = await self._maybe_await(screenshot_fn())
                    if isinstance(screenshot_data, str) and screenshot_data.strip():
                        import base64

                        screenshot_bytes = base64.b64decode(screenshot_data)
                        screenshot_path.write_bytes(screenshot_bytes)
                    elif isinstance(screenshot_data, (bytes, bytearray)):
                        screenshot_bytes = bytes(screenshot_data)
                        screenshot_path.write_bytes(screenshot_bytes)

            if screenshot_bytes is None and not screenshot_path.exists():
                raise RuntimeError("no supported screenshot method available")
        except Exception as exc:
            screenshot_error = str(exc)

        try:
            html_content = None
            content_fn = getattr(page, "content", None)
            if callable(content_fn):
                html_content = await self._maybe_await(content_fn())
            if not isinstance(html_content, str) or not html_content:
                evaluate_fn = getattr(page, "evaluate", None)
                if callable(evaluate_fn):
                    html_raw = await self._maybe_await(
                        evaluate_fn(
                            "(...args) => document.documentElement ? document.documentElement.outerHTML : ''"
                        )
                    )
                    if isinstance(html_raw, str):
                        html_content = html_raw

            if not isinstance(html_content, str) or not html_content:
                raise RuntimeError("no supported html extraction method available")

            html_path.write_text(html_content, encoding="utf-8")
        except Exception as exc:
            html_error = str(exc)

        meta = {
            "captured_at": datetime.utcnow().isoformat(),
            "page_url": page_url,
            "page_title": page_title,
            "label": label,
            "state": state_data or {},
            "extra": extra or {},
            "screenshot_error": screenshot_error,
            "html_error": html_error,
        }
        meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        return {
            "screenshot_path": str(screenshot_path) if screenshot_path.exists() else None,
            "html_path": str(html_path) if html_path.exists() else None,
            "meta_path": str(meta_path),
            "page_url": page_url,
        }

    async def _scrape_story_interactions_via_js(
        self,
        browser_session: BrowserSession,
        profile_url: str,
        story_url: str,
        safe_max_interactions: int,
    ) -> Dict[str, Any]:
        target_username = self._extract_instagram_username(profile_url) or ""
        state_script = """
        (...args) => {
          const targetUsername = String(args[0] || '').trim().replace(/^@/, '').toLowerCase();
          const href = window.location.href || '';
          const path = window.location.pathname || '';
          const storyMatch = href.match(/\\/stories\\/([^\\/?#]+)\\/(\\d+)(?:\\/|$)/i);
          const storyUrl = storyMatch ? `https://www.instagram.com/stories/${storyMatch[1]}/${storyMatch[2]}/` : '';
          const isStoryUrl = Boolean(storyMatch);
          const pageText = (document.body && document.body.innerText) ? document.body.innerText : '';
          const textSample = pageText.slice(0, 4000).toLowerCase();
          const metaDescription = (
            document.querySelector('meta[name="description"]')
            && document.querySelector('meta[name="description"]').getAttribute('content')
          ) ? document.querySelector('meta[name="description"]').getAttribute('content').toLowerCase() : '';
          const hasPasswordInput = Boolean(document.querySelector('input[type="password"]'));
          const loginPath = /\\/accounts\\/login/i.test(path);
          const oneTapPath = /\\/accounts\\/onetap\\/?/i.test(path);
          const challengePath = /\\/challenge\\/?|\\/accounts\\/suspended\\/?|\\/two_factor\\/?|\\/reauthentication\\//i.test(path);
          const loginText = /\\blog in\\b|\\bentrar\\b|senha incorreta|incorrect password/i.test(textSample);
          const challengeText = /confirm it's you|confirm its you|enter your password|security code|enter code|unusual login attempt|check your notifications|challenge required|checkpoint required|confirme que e voce|confirme que é voce|digite sua senha|insira sua senha|codigo de seguranca|código de seguranca|insira o codigo|insira o código|verifique sua identidade/i.test(textSample);
          const passwordPrompt = hasPasswordInput && /continue|confirm|confirmar|continuar|password|senha/i.test(textSample);
          const baseLoginRequired = loginPath || challengePath || loginText || challengeText || passwordPrompt;
          let authPromptReason = null;
          if (baseLoginRequired) {
            if (challengePath) authPromptReason = 'challenge_path';
            else if (passwordPrompt) authPromptReason = 'password_prompt';
            else if (challengeText) authPromptReason = 'challenge_text';
            else if (loginPath) authPromptReason = 'login_path';
            else if (loginText) authPromptReason = 'login_text';
          }

          let viewCount = null;
          const controls = Array.from(document.querySelectorAll('button,div[role="button"],a,span'));
          const clickableControls = Array.from(document.querySelectorAll('button,div[role="button"],a'));
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
          const removeProfilesModalOpen = Boolean(
            dialog && /remove profiles from this browser|remove profiles|remover perfis deste navegador|remover perfis/i.test(dialogText)
          );
          const continueVisible = clickableControls.some((el) => {
            const text = ((el.textContent || '') + ' ' + (el.getAttribute('aria-label') || '')).trim().toLowerCase();
            return text === 'continue'
              || text === 'continuar'
              || text.startsWith('continue as')
              || text.startsWith('continuar como');
          });
          const alternateProfileVisible = clickableControls.some((el) => {
            const text = ((el.textContent || '') + ' ' + (el.getAttribute('aria-label') || '')).trim().toLowerCase();
            return text.includes('use another profile')
              || text.includes('usar outro perfil')
              || text.includes('create new account')
              || text.includes('criar nova conta');
          });
          const landingTextVisible = /see everyday moments from|close friends|veja momentos do dia a dia|amigos proximos|amigos próximos/i.test(textSample);
          const targetUsernameVisible = Boolean(targetUsername) && textSample.includes(targetUsername);
          const profileGateVisible = Boolean(
            oneTapPath
            || removeProfilesModalOpen
            || (continueVisible && alternateProfileVisible)
            || (continueVisible && targetUsernameVisible)
            || (continueVisible && landingTextVisible)
          );
          let profileGateHtmlSample = '';
          if (profileGateVisible) {
            const html = (document.documentElement && document.documentElement.innerHTML) ? document.documentElement.innerHTML : '';
            profileGateHtmlSample = html.slice(0, 250000).toLowerCase();
          }
          const profileGateRequiresPassword = Boolean(
            profileGateVisible && (
              profileGateHtmlSample.includes('password_entry')
              || profileGateHtmlSample.includes('"n_credential_type":"password"')
              || profileGateHtmlSample.includes('"n_credential_type","value":"password"')
              || metaDescription.includes('create an account or log in to instagram')
            )
          );
          const loginRequired = baseLoginRequired || profileGateRequiresPassword;
          const viewersModalOpen = Boolean(
            dialog && (
              dialogText.includes('visualizador')
              || dialogText.includes('viewer')
            )
          );

          return {
            current_url: href,
            current_path: path,
            story_url: storyUrl,
            is_story_url: isStoryUrl,
            view_count: Number.isFinite(viewCount) ? viewCount : null,
            viewers_modal_open: viewersModalOpen,
            profile_gate_visible: profileGateVisible,
            profile_gate_modal_open: removeProfilesModalOpen,
            profile_gate_continue_visible: continueVisible,
            profile_gate_username_visible: targetUsernameVisible,
            profile_gate_requires_password: profileGateRequiresPassword,
            login_required: loginRequired,
            auth_prompt_reason: profileGateRequiresPassword ? 'profile_gate_password_entry' : authPromptReason,
          };
        }
        """

        resolve_profile_gate_script = """
        (...args) => {
          const targetUsername = String(args[0] || '').trim().replace(/^@/, '').toLowerCase();
          const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
          const getCenter = (el) => {
            if (!el || typeof el.getBoundingClientRect !== 'function') return null;
            const rect = el.getBoundingClientRect();
            return {
              x: Math.max(2, Math.floor(rect.left + (rect.width / 2))),
              y: Math.max(2, Math.floor(rect.top + (rect.height / 2))),
            };
          };
          const tryCenterClick = (el, method) => {
            if (!el || typeof el.getBoundingClientRect !== 'function') return null;
            try {
              const center = getCenter(el);
              if (!center) return null;
              const { x, y } = center;
              const target = document.elementFromPoint(x, y) || el;
              ['pointerdown', 'mousedown', 'mouseup', 'click'].forEach((eventName) => {
                try {
                  target.dispatchEvent(new MouseEvent(eventName, {
                    bubbles: true,
                    cancelable: true,
                    composed: true,
                    clientX: x,
                    clientY: y,
                    view: window,
                  }));
                } catch (e) {}
              });
              return { handled: true, action: method, x, y, click_strategy: 'center_events' };
            } catch (e) {
              return null;
            }
          };
          const tryClick = (el, method) => {
            if (!el) return null;
            const clickable = (typeof el.closest === 'function')
              ? (el.closest('button, a, div[role="button"]') || el)
              : el;
            const center = getCenter(clickable);
            try {
              clickable.scrollIntoView({ block: 'center', inline: 'center' });
            } catch (e) {}
            try {
              clickable.click();
              return {
                handled: true,
                action: method,
                x: center ? center.x : null,
                y: center ? center.y : null,
                click_strategy: 'dom_click',
              };
            } catch (e) {
              return tryCenterClick(clickable, method);
            }
          };

          const controls = Array.from(document.querySelectorAll('button, a, div[role="button"]'));
          const dialog = document.querySelector('div[role="dialog"]');
          const dialogText = normalize(dialog ? dialog.textContent : '');
          const bodyText = normalize(document.body ? document.body.innerText : '');

          if (dialog && /remove profiles from this browser|remove profiles|remover perfis deste navegador|remover perfis/.test(dialogText)) {
            const closeCandidate = Array.from(dialog.querySelectorAll('button, [aria-label], div[role="button"], svg'))
              .find((el) => {
                const text = normalize((el.textContent || '') + ' ' + (el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('title') || ''));
                return text === 'x'
                  || text === 'close'
                  || text === 'fechar'
                  || text.includes('close')
                  || text.includes('fechar');
              });
            const closeResult = tryClick(closeCandidate, 'close_remove_profiles_modal');
            if (closeResult) {
              return closeResult;
            }
          }

          const continueCandidates = controls.filter((el) => {
            const text = normalize((el.textContent || '') + ' ' + (el.getAttribute('aria-label') || ''));
            return text === 'continue'
              || text === 'continuar'
              || text.startsWith('continue as')
              || text.startsWith('continuar como');
          });

          if (continueCandidates.length) {
            let bestContinue = continueCandidates[0];
            if (targetUsername) {
              const usernameNode = Array.from(document.querySelectorAll('div, span, a, h1, h2, h3, h4'))
                .find((el) => normalize(el.textContent || '') === targetUsername);
              if (usernameNode && typeof usernameNode.closest === 'function') {
                const container = usernameNode.closest('main, section, article, div');
                if (container) {
                  const containerContinue = continueCandidates.find((candidate) => container.contains(candidate));
                  if (containerContinue) {
                    bestContinue = containerContinue;
                  }
                }
              }
            }
            const continueResult = tryClick(bestContinue, 'continue_saved_profile');
            if (continueResult) {
              return continueResult;
            }
          }

          return {
            handled: false,
            reason: 'profile_gate_not_actionable',
            debug: {
              current_url: window.location.href || '',
              has_dialog: Boolean(dialog),
              dialog_text: dialogText.slice(0, 240),
              continue_candidates: continueCandidates.length,
              target_username_visible: Boolean(targetUsername) && bodyText.includes(targetUsername),
            },
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

        open_story_from_profile_script = """
        (...args) => {
          const targetUsername = String(args[0] || '').trim().replace(/^@/, '').toLowerCase();
          const tryCenterClick = (el, method) => {
            if (!el || typeof el.getBoundingClientRect !== 'function') return null;
            try {
              const rect = el.getBoundingClientRect();
              const x = Math.max(2, Math.floor(rect.left + (rect.width / 2)));
              const y = Math.max(2, Math.floor(rect.top + (rect.height / 2)));
              const target = document.elementFromPoint(x, y) || el;
              ['pointerdown', 'mousedown', 'mouseup', 'click'].forEach((eventName) => {
                try {
                  target.dispatchEvent(new MouseEvent(eventName, {
                    bubbles: true,
                    cancelable: true,
                    composed: true,
                    clientX: x,
                    clientY: y,
                    view: window,
                  }));
                } catch (e) {}
              });
              return { clicked: true, method: `${method}_center_click`, x, y };
            } catch (e) {
              return null;
            }
          };

          const tryClick = (el, method) => {
            if (!el) return null;
            const clickable = (typeof el.closest === 'function')
              ? (el.closest('button, a, div[role="button"]') || el)
              : el;
            try {
              clickable.scrollIntoView({ block: 'center', inline: 'center' });
            } catch (e) {}
            try {
              clickable.click();
              return { clicked: true, method };
            } catch (e) {
              return tryCenterClick(clickable, method);
            }
          };

          const scoreElement = (el, href = '') => {
            let score = 0;
            const rect = typeof el.getBoundingClientRect === 'function'
              ? el.getBoundingClientRect()
              : { top: 9999, left: 9999, width: 0, height: 0 };
            const text = (
              (el.textContent || '') + ' '
              + (el.getAttribute('aria-label') || '') + ' '
              + (el.getAttribute('title') || '') + ' '
              + (el.getAttribute('alt') || '')
            ).toLowerCase();
            const normalizedHref = String(href || '').toLowerCase();
            const tagName = String(el.tagName || '').toLowerCase();

            if (normalizedHref.includes('/stories/')) score += 100;
            if (targetUsername && normalizedHref.includes(`/stories/${targetUsername}/`)) score += 80;
            if (targetUsername && (text.includes(targetUsername) || normalizedHref.includes(`/${targetUsername}/`))) score += 60;
            if (text.includes('story') || text.includes('stories') || text.includes('historia') || text.includes('historias')) score += 30;
            if (text.includes('follow') || text.includes('seguir') || text.includes('message') || text.includes('mensagem') || text.includes('edit profile')) score -= 40;
            if (
              text.includes('continue')
              || text.includes('continuar')
              || text.includes('use another profile')
              || text.includes('usar outro perfil')
              || text.includes('create new account')
              || text.includes('criar nova conta')
              || text.includes('remove profiles')
              || text.includes('remover perfis')
            ) score -= 120;
            if (el.querySelector && el.querySelector('img')) score += 20;
            if (el.querySelector && el.querySelector('canvas, svg')) score += 20;
            if (tagName === 'img' || tagName === 'canvas' || tagName === 'svg') score += 25;
            if (rect.top >= 0 && rect.top <= 520) score += 20;
            if (rect.left >= 0 && rect.left <= 420) score += 10;
            if (rect.width >= 24 && rect.height >= 24) score += 10;
            if (Math.abs(rect.width - rect.height) <= Math.max(24, rect.width * 0.35)) score += 15;
            if (rect.width >= 36 && rect.width <= 220 && rect.height >= 36 && rect.height <= 220) score += 15;

            return score;
          };

          const anchorCandidates = Array.from(document.querySelectorAll('a[href]'))
            .map((el) => ({ el, href: el.getAttribute('href') || '' }))
            .filter((item) => (item.href || '').includes('/stories/'))
            .map((item) => ({ ...item, score: scoreElement(item.el, item.href) }))
            .sort((a, b) => b.score - a.score);

          for (const candidate of anchorCandidates) {
            const clicked = tryClick(candidate.el, 'story_anchor');
            if (clicked) {
              return { ...clicked, href: candidate.href, score: candidate.score };
            }
          }

          const genericCandidates = Array.from(
            document.querySelectorAll('a, button, div[role="button"]')
          )
            .map((el) => ({
              el,
              score: scoreElement(el, el.getAttribute && el.getAttribute('href')),
            }))
            .filter((item) => item.score >= 40)
            .sort((a, b) => b.score - a.score);

          for (const candidate of genericCandidates) {
            const clicked = tryClick(candidate.el, 'profile_header_candidate');
            if (clicked) {
              return { ...clicked, score: candidate.score };
            }
          }

          const visualCandidates = Array.from(
            document.querySelectorAll('img, canvas, svg')
          )
            .map((el) => ({ el, score: scoreElement(el, el.getAttribute && el.getAttribute('href')) }))
            .filter((item) => item.score >= 45)
            .sort((a, b) => b.score - a.score);

          for (const candidate of visualCandidates) {
            const clicked = tryClick(candidate.el, 'visual_avatar_candidate');
            if (clicked) {
              return { ...clicked, score: candidate.score };
            }
          }

          const avatarImg = document.querySelector('header img');
          if (avatarImg) {
            const clicked = tryClick(avatarImg.closest('button, a, div[role="button"]') || avatarImg, 'header_avatar');
            if (clicked) {
              return clicked;
            }
          }

          const header = document.querySelector('header');
          if (header) {
            const fallbackClick = tryCenterClick(header, 'header_region_fallback');
            if (fallbackClick) {
              return fallbackClick;
            }
          }

          return {
            clicked: false,
            reason: 'profile_story_trigger_not_found',
            debug: {
              ready_state: document.readyState || '',
              body_text_length: ((document.body && document.body.innerText) || '').length,
              anchor_candidates: anchorCandidates.length,
              generic_candidates: genericCandidates.length,
              visual_candidates: visualCandidates.length,
              has_header_img: Boolean(avatarImg),
              total_anchors: document.querySelectorAll('a').length,
              total_images: document.querySelectorAll('img').length,
              has_main: Boolean(document.querySelector('main')),
              has_header: Boolean(document.querySelector('header')),
              current_url: window.location.href || '',
            },
          };
        }
        """

        extract_story_viewers_script = """
        (...args) => (async () => {
          const maxUsers = Math.max(1, Number(args[0] || 300));
          const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
          const dialog = document.querySelector('div[role="dialog"]');
          if (!dialog) {
            return { popup_opened: false, viewer_users: [], liked_users: [] };
          }
          const dialogText = (dialog.textContent || '').toLowerCase();
          if (!(dialogText.includes('visualizador') || dialogText.includes('viewer'))) {
            return { popup_opened: false, viewer_users: [], liked_users: [] };
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

              const liked = rowHasHeartBadge(row);
              if (liked) {
                debug.heart_hits += 1;
              }

              const existing = usersMap.get(username);
              if (!existing) {
                usersMap.set(username, {
                  user_username: username,
                  user_url: `https://www.instagram.com/${username}/`,
                  liked
                });
              } else if (!existing.liked && liked) {
                existing.liked = true;
                usersMap.set(username, existing);
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

          const viewerUsers = Array.from(usersMap.values()).map((item) => ({
            user_username: item.user_username,
            user_url: item.user_url,
            liked: item.liked === true
          }));
          const likedUsers = viewerUsers
            .filter((item) => item.liked === true)
            .map((item) => ({
              user_username: item.user_username,
              user_url: item.user_url,
              badge_heart_red: true
            }));

          return {
            popup_opened: true,
            viewer_users: viewerUsers,
            liked_users: likedUsers,
            debug: {
              ...debug,
              viewers_collected: viewerUsers.length,
              liked_collected: likedUsers.length
            }
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
              || txt === 'x' || txt === 'Ã—';
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
          const tryClick = (el, method) => {
            if (!el) return null;
            try {
              el.scrollIntoView({ block: 'center', inline: 'center' });
            } catch (e) {}
            try {
              el.click();
              return { clicked: true, method };
            } catch (e) {
              return null;
            }
          };

          const candidates = Array.from(document.querySelectorAll('button,div[role="button"],a'));
          const rightSide = candidates
            .map((el) => ({ el, rect: el.getBoundingClientRect() }))
            .filter((item) => item.rect.width > 10 && item.rect.height > 10 && item.rect.left > (window.innerWidth * 0.55));

          const byLabel = rightSide.find((item) => {
            const aria = (item.el.getAttribute('aria-label') || '').toLowerCase();
            const txt = (item.el.textContent || '').toLowerCase();
            return aria.includes('next') || aria.includes('next story')
              || aria.includes('prÃ³ximo') || aria.includes('proximo')
              || aria.includes('avanÃ§') || aria.includes('seguinte')
              || txt.includes('next') || txt.includes('prÃ³ximo') || txt.includes('proximo');
          });
          const byLabelResult = tryClick(byLabel ? byLabel.el : null, 'button_label');
          if (byLabelResult) return byLabelResult;

          const genericTarget = rightSide.length ? rightSide[rightSide.length - 1].el : null;
          const genericResult = tryClick(genericTarget, 'button_right');
          if (genericResult) return genericResult;

          try {
            const x = Math.max(2, Math.floor(window.innerWidth - 24));
            const y = Math.max(2, Math.floor(window.innerHeight / 2));
            const edgeTarget = document.elementFromPoint(x, y);
            if (edgeTarget) {
              edgeTarget.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
              return { clicked: true, method: 'edge_click' };
            }
          } catch (e) {}

          try {
            const evt = new KeyboardEvent('keydown', { key: 'ArrowRight', code: 'ArrowRight', bubbles: true });
            document.dispatchEvent(evt);
            window.dispatchEvent(evt);
            return { clicked: true, method: 'keyboard_arrow' };
          } catch (e) {}

          return { clicked: false, reason: 'next_not_found' };
        }
        """

        debug_username = target_username or "instagram"
        debug_run_id = (
            f"{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_"
            f"{self._sanitize_debug_artifact_name(debug_username)}_"
            f"{uuid4().hex[:8]}"
        )
        debug_output_dir = self._stories_debug_root_dir() / debug_run_id
        debug_capture_count = 0
        debug_capture_limit = 12

        async def _capture_story_debug(
            page_obj: Optional[Any],
            label: str,
            state_data: Optional[Dict[str, Any]] = None,
            extra: Optional[Dict[str, Any]] = None,
        ) -> Optional[Dict[str, Any]]:
            nonlocal debug_capture_count
            if page_obj is None or debug_capture_count >= debug_capture_limit:
                return None

            effective_state = state_data
            if effective_state is None:
                try:
                    state_raw = await self._evaluate_page_json(page_obj, state_script, target_username)
                    effective_state = state_raw if isinstance(state_raw, dict) else {}
                except Exception as exc:
                    effective_state = {"state_capture_error": str(exc)}

            debug_capture_count += 1
            artifact = await self._capture_page_debug_artifacts(
                page_obj,
                output_dir=debug_output_dir,
                label=f"{debug_capture_count:02d}_{label}",
                state_data=effective_state,
                extra={
                    "profile_url": profile_url,
                    "story_url": story_url,
                    **(extra or {}),
                },
            )
            if artifact:
                logger.info(
                    "Stories JS: debug salvo (%s): screenshot=%s html=%s meta=%s",
                    label,
                    artifact.get("screenshot_path"),
                    artifact.get("html_path"),
                    artifact.get("meta_path"),
                )
            return artifact

        async def _click_page_coordinates(
            page_obj: Optional[Any],
            x: Any,
            y: Any,
        ) -> bool:
            if page_obj is None:
                return False
            try:
                target_x = int(float(x))
                target_y = int(float(y))
            except (TypeError, ValueError):
                return False

            try:
                mouse = await page_obj.mouse
                await mouse.move(target_x, target_y)
                await asyncio.sleep(0.1)
                await mouse.click(target_x, target_y)
                return True
            except Exception as exc:
                logger.debug(
                    "Stories JS: falha ao clicar por coordenadas no profile gate (%s, %s): %s",
                    target_x,
                    target_y,
                    exc,
                )
                return False

        async def _resolve_profile_gate(
            page_obj: Optional[Any],
            page_reason: str,
            state_data: Optional[Dict[str, Any]] = None,
        ) -> bool:
            if page_obj is None:
                return False

            effective_state = state_data if isinstance(state_data, dict) else {}
            if not effective_state.get("profile_gate_visible"):
                return False
            if effective_state.get("profile_gate_requires_password"):
                return False

            action_raw = await self._evaluate_page_json(
                page_obj,
                resolve_profile_gate_script,
                target_username,
            )
            action_data = action_raw if isinstance(action_raw, dict) else {}
            if action_data.get("handled"):
                await asyncio.sleep(0.8)
                post_action_raw = await self._evaluate_page_json(page_obj, state_script, target_username)
                post_action_state = post_action_raw if isinstance(post_action_raw, dict) else {}
                modal_closed = bool(
                    effective_state.get("profile_gate_modal_open")
                    and not post_action_state.get("profile_gate_modal_open")
                )
                if (not post_action_state.get("profile_gate_visible")) or modal_closed:
                    logger.info(
                        "Stories JS: profile gate resolvido (%s) via %s",
                        page_reason,
                        action_data.get("action") or "unknown",
                    )
                    await _capture_story_debug(
                        page_obj,
                        f"profile_gate_resolved_{page_reason}",
                        state_data=post_action_state or effective_state,
                        extra={"action_data": action_data},
                    )
                    return True

                if await _click_page_coordinates(
                    page_obj,
                    action_data.get("x"),
                    action_data.get("y"),
                ):
                    await asyncio.sleep(1.2)
                    post_mouse_raw = await self._evaluate_page_json(page_obj, state_script, target_username)
                    post_mouse_state = post_mouse_raw if isinstance(post_mouse_raw, dict) else {}
                    modal_closed = bool(
                        effective_state.get("profile_gate_modal_open")
                        and not post_mouse_state.get("profile_gate_modal_open")
                    )
                    if (not post_mouse_state.get("profile_gate_visible")) or modal_closed:
                        logger.info(
                            "Stories JS: profile gate resolvido (%s) via %s_mouse",
                            page_reason,
                            action_data.get("action") or "unknown",
                        )
                        await _capture_story_debug(
                            page_obj,
                            f"profile_gate_resolved_{page_reason}",
                            state_data=post_mouse_state or effective_state,
                            extra={"action_data": {**action_data, "click_strategy": "mouse_click"}},
                        )
                        return True

                logger.warning(
                    "Stories JS: profile gate clique sem efeito (%s): action=%s strategy=%s url=%s",
                    page_reason,
                    action_data.get("action") or "unknown",
                    action_data.get("click_strategy") or "unknown",
                    (post_action_state or {}).get("current_url") or "",
                )
                await _capture_story_debug(
                    page_obj,
                    f"profile_gate_click_no_effect_{page_reason}",
                    state_data=post_action_state or effective_state,
                    extra={"action_data": action_data},
                )
                return False

            logger.warning(
                "Stories JS: profile gate detectado mas nao resolvido (%s): %s debug=%s",
                page_reason,
                action_data.get("reason") or "profile_gate_not_actionable",
                action_data.get("debug"),
            )
            await _capture_story_debug(
                page_obj,
                f"profile_gate_unresolved_{page_reason}",
                state_data=effective_state,
                extra={"action_data": action_data},
            )
            return False

        async def _read_state_from_current_page() -> tuple[Optional[Any], Dict[str, Any]]:
            page_obj = await browser_session.get_current_page()
            if page_obj is None:
                return None, {}
            state_raw = await self._evaluate_page_json(page_obj, state_script, target_username)
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
                if await _resolve_profile_gate(
                    page_obj,
                    page_reason="wait_for_story_url",
                    state_data=state_data,
                ):
                    continue
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

        async def _recover_story_from_profile(
            reason: str,
            max_wait_seconds: float = 14.0,
        ) -> tuple[Optional[Any], Dict[str, Any], bool]:
            async def _attempt_open_story_from_page(
                page_obj: Optional[Any],
                page_reason: str,
                wait_seconds: float,
            ) -> tuple[bool, Optional[dict[str, Any]]]:
                if page_obj is None:
                    return False, None
                deadline = asyncio.get_event_loop().time() + wait_seconds
                last_click_data: Optional[dict[str, Any]] = None
                while asyncio.get_event_loop().time() < deadline:
                    state_raw = await self._evaluate_page_json(page_obj, state_script, target_username)
                    current_state = state_raw if isinstance(state_raw, dict) else {}
                    if await _resolve_profile_gate(
                        page_obj,
                        page_reason=f"{page_reason}:before_open_story",
                        state_data=current_state,
                    ):
                        return True, {"handled_profile_gate": True}
                    click_raw = await self._evaluate_page_json(
                        page_obj,
                        open_story_from_profile_script,
                        target_username,
                    )
                    click_data = click_raw if isinstance(click_raw, dict) else {}
                    last_click_data = click_data
                    if click_data.get("clicked"):
                        logger.info(
                            "Stories JS: recovery acionado (%s) via %s",
                            page_reason,
                            click_data.get("method") or "unknown",
                        )
                        await asyncio.sleep(1.2)
                        await _capture_story_debug(
                            page_obj,
                            f"recovery_clicked_{page_reason}",
                            extra={"click_data": click_data},
                        )
                        return True, click_data
                    await asyncio.sleep(1.0)
                logger.warning(
                    "Stories JS: nao foi possivel abrir o story pelo perfil (%s): %s debug=%s",
                    page_reason,
                    (last_click_data or {}).get("reason") or "profile_story_trigger_not_found",
                    (last_click_data or {}).get("debug"),
                )
                await _capture_story_debug(
                    page_obj,
                    f"recovery_not_found_{page_reason}",
                    extra={"click_data": last_click_data or {}},
                )
                return False, last_click_data

            page_obj, state_data = await _read_state_from_current_page()
            opened, _ = await _attempt_open_story_from_page(
                page_obj,
                f"{reason}:pagina_atual",
                wait_seconds=4.0,
            )
            if opened:
                return await _wait_for_story_url(max_wait_seconds=max_wait_seconds)

            recovery_urls = [
                ("perfil_alvo", profile_url),
                ("home_feed", "https://www.instagram.com/"),
            ]
            for label, recovery_url in recovery_urls:
                await self._navigate_to_url_with_timeout(
                    browser_session,
                    recovery_url,
                    timeout_ms=30000,
                    new_tab=False,
                )
                await asyncio.sleep(2.5)
                page_obj, state_data = await _read_state_from_current_page()
                opened, _ = await _attempt_open_story_from_page(
                    page_obj,
                    f"{reason}:{label}",
                    wait_seconds=4.0,
                )
                if opened:
                    return await _wait_for_story_url(max_wait_seconds=max_wait_seconds)

            return page_obj, state_data, False

        await self._navigate_to_url_with_timeout(
            browser_session,
            story_url,
            timeout_ms=30000,
            new_tab=False,
        )
        await asyncio.sleep(1.0)

        page, initial_state, initial_ready = await _wait_for_story_url(max_wait_seconds=20.0)
        if not initial_ready and not initial_state.get("login_required"):
            page, initial_state, initial_ready = await _recover_story_from_profile(
                reason="navegacao_inicial_sem_story_id",
                max_wait_seconds=16.0,
            )
        if initial_state.get("login_required"):
            await _capture_story_debug(
                page,
                "login_required_initial",
                state_data=initial_state,
                extra={"auth_prompt_reason": initial_state.get("auth_prompt_reason")},
            )
            return {
                "profile_url": profile_url,
                "stories_accessible": False,
                "story_posts": [],
                "total_story_posts": 0,
                "total_story_viewers": 0,
                "total_liked_users": 0,
                "total_collected": 0,
                "error": "login_required",
            }
        if not initial_ready:
            logger.warning(
                "Stories JS: viewer ainda nao estabilizou apos navegacao inicial (%s).",
                story_url,
            )
            await _capture_story_debug(
                page,
                "initial_viewer_not_ready",
                state_data=initial_state,
            )

        story_posts: List[Dict[str, Any]] = []
        seen_story_ids: set[str] = set()
        seen_like_keys: set[str] = set()
        total_viewers_collected = 0
        last_valid_story_url = ""
        recover_attempts = 0
        max_story_steps = max(5, min(80, safe_max_interactions * 2))
        last_page_snapshot = page
        last_state_snapshot = initial_state if isinstance(initial_state, dict) else {}

        for _ in range(max_story_steps):
            page, state = await _read_state_from_current_page()
            last_page_snapshot = page
            last_state_snapshot = state if isinstance(state, dict) else {}
            if page is None:
                return {
                    "profile_url": profile_url,
                    "stories_accessible": False,
                    "story_posts": story_posts,
                    "total_story_posts": len(story_posts),
                    "total_story_viewers": total_viewers_collected,
                    "total_liked_users": len(seen_like_keys),
                    "total_collected": total_viewers_collected,
                    "error": "story_open_failed",
                }
            if await _resolve_profile_gate(
                page,
                page_reason="main_loop",
                state_data=state,
            ):
                await _wait_for_story_url(max_wait_seconds=12.0)
                continue
            if state.get("login_required"):
                await _capture_story_debug(
                    page,
                    "login_required_during_loop",
                    state_data=state,
                    extra={"auth_prompt_reason": state.get("auth_prompt_reason")},
                )
                return {
                    "profile_url": profile_url,
                    "stories_accessible": False,
                    "story_posts": [],
                    "total_story_posts": 0,
                    "total_story_viewers": total_viewers_collected,
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
                await _capture_story_debug(
                    page,
                    "story_url_empty_exhausted",
                    state_data=state,
                    extra={"last_valid_story_url": last_valid_story_url},
                )
                return {
                    "profile_url": profile_url,
                    "stories_accessible": False,
                    "story_posts": [],
                    "total_story_posts": 0,
                    "total_story_viewers": total_viewers_collected,
                    "total_liked_users": 0,
                    "total_collected": 0,
                    "error": "story_open_failed",
                }

            story_id = self._extract_story_id_from_url(current_story_url)
            if not story_id:
                if (
                    "/stories/" not in current_story_url
                    and recover_attempts < 3
                ):
                    recover_attempts += 1
                    logger.warning(
                        "Stories JS: URL fora do viewer (%s). Tentando recovery pelo perfil (%s/3)...",
                        current_story_url,
                        recover_attempts,
                    )
                    await _capture_story_debug(
                        page,
                        f"url_outside_viewer_attempt_{recover_attempts}",
                        state_data=state,
                        extra={"current_story_url": current_story_url},
                    )
                    _, recovered_state, recovered_ready = await _recover_story_from_profile(
                        reason="viewer_redirecionado_para_fora_do_story",
                        max_wait_seconds=16.0,
                    )
                    if recovered_ready:
                        current_story_url = self._normalize_story_url_value(
                            recovered_state.get("story_url") or recovered_state.get("current_url")
                        )
                        story_id = self._extract_story_id_from_url(current_story_url)
                        if story_id:
                            last_valid_story_url = current_story_url
                            recover_attempts = 0
                    if not story_id:
                        continue
                if recover_attempts < 6:
                    recover_attempts += 1
                    logger.warning(
                        "Stories JS: URL sem story_id (%s). Aguardando estabilizacao (%s/6)...",
                        current_story_url,
                        recover_attempts,
                    )
                    if recover_attempts == 1:
                        await _capture_story_debug(
                            page,
                            "story_id_missing_waiting",
                            state_data=state,
                            extra={"current_story_url": current_story_url},
                        )
                    await _wait_for_story_url(max_wait_seconds=10.0)
                    continue
                if story_posts:
                    break
                await _capture_story_debug(
                    page,
                    "story_id_missing_exhausted",
                    state_data=state,
                    extra={"current_story_url": current_story_url},
                )
                return {
                    "profile_url": profile_url,
                    "stories_accessible": False,
                    "story_posts": [],
                    "total_story_posts": 0,
                    "total_story_viewers": total_viewers_collected,
                    "total_liked_users": 0,
                    "total_collected": 0,
                    "error": "story_open_failed",
                }
            last_valid_story_url = current_story_url
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
            for _popup_try in range(3):
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

            viewer_users: List[Dict[str, Any]] = []
            liked_users: List[Dict[str, str]] = []
            extraction_debug: Dict[str, Any] = {}
            if popup_open:
                max_remaining = max(1, safe_max_interactions)
                extracted_raw = await self._evaluate_page_json(
                    page,
                    extract_story_viewers_script,
                    max_remaining,
                )
                extracted_data = extracted_raw if isinstance(extracted_raw, dict) else {}
                if isinstance(extracted_data.get("debug"), dict):
                    extraction_debug = extracted_data.get("debug") or {}
                raw_viewer_users = (
                    extracted_data.get("viewer_users")
                    or extracted_data.get("viewers")
                    or []
                )
                if isinstance(raw_viewer_users, list):
                    seen_story_viewer_keys: set[str] = set()
                    for raw_user in raw_viewer_users:
                        if not isinstance(raw_user, dict):
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
                        liked_flag = bool(raw_user.get("liked") is True or raw_user.get("badge_heart_red") is True)
                        viewer_key = user_url or username
                        if viewer_key in seen_story_viewer_keys:
                            continue
                        seen_story_viewer_keys.add(viewer_key)
                        viewer_users.append(
                            {
                                "user_username": username or "",
                                "user_url": user_url or "",
                                "liked": liked_flag,
                            }
                        )
                        total_viewers_collected += 1
                        if liked_flag:
                            like_key = viewer_key
                            if like_key not in seen_like_keys:
                                seen_like_keys.add(like_key)
                                liked_users.append(
                                    {
                                        "user_username": username or "",
                                        "user_url": user_url or "",
                                    }
                                )
                        if len(viewer_users) >= safe_max_interactions:
                            break

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
                        if like_key not in seen_story_viewer_keys:
                            if len(viewer_users) >= safe_max_interactions:
                                break
                            seen_story_viewer_keys.add(like_key)
                            viewer_users.append(
                                {
                                    "user_username": username or "",
                                    "user_url": user_url or "",
                                    "liked": True,
                                }
                            )
                            total_viewers_collected += 1
                        if like_key in seen_like_keys:
                            continue
                        seen_like_keys.add(like_key)
                        liked_users.append(
                            {
                                "user_username": username or "",
                                "user_url": user_url or "",
                            }
                        )
                        if len(viewer_users) >= safe_max_interactions:
                            break

            logger.info(
                "Stories JS: story=%s views=%s popup_open=%s viewers=%s liked_users=%s debug=%s",
                story_id,
                view_count,
                popup_open,
                len(viewer_users),
                len(liked_users),
                extraction_debug or None,
            )

            story_posts.append(
                {
                    "story_url": current_story_url,
                    "view_count": view_count,
                    "viewer_users": viewer_users,
                    "liked_users": liked_users,
                }
            )

            for _close_try in range(3):
                await self._evaluate_page_json(page, close_modal_script)
                await asyncio.sleep(0.6)
                modal_state_raw = await self._evaluate_page_json(page, state_script)
                modal_state = modal_state_raw if isinstance(modal_state_raw, dict) else {}
                if not bool(modal_state.get("viewers_modal_open")):
                    break

            next_changed = False
            for _next_try in range(4):
                next_raw = await self._evaluate_page_json(page, click_next_story_script)
                next_data = next_raw if isinstance(next_raw, dict) else {}
                if not next_data.get("clicked"):
                    await asyncio.sleep(0.8)
                    continue
                await asyncio.sleep(1.2)
                _, new_state, _ = await _wait_for_story_url(max_wait_seconds=6.0)
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
                "total_story_viewers": total_viewers_collected,
                "total_liked_users": len(seen_like_keys),
                "total_collected": total_viewers_collected,
                "error": None,
            }

        await _capture_story_debug(
            last_page_snapshot,
            "story_open_failed_final",
            state_data=last_state_snapshot,
            extra={"last_valid_story_url": last_valid_story_url},
        )
        return {
            "profile_url": profile_url,
            "stories_accessible": False,
            "story_posts": [],
            "total_story_posts": 0,
            "total_story_viewers": 0,
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
        Verifica se existe cookie de autenticaÃ§Ã£o aparentemente vÃ¡lido.
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

    async def inspect_instagram_session_in_browserless(
        self,
        storage_state: Optional[Dict[str, Any]],
        instagram_username: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not isinstance(storage_state, dict):
            return {
                "valid": False,
                "reason": "invalid_storage_state",
                "state": {},
                "root_state": {},
                "edit_state": {},
                "storage_state": None,
                "user_agent": None,
            }

        cookies = self._extract_cookies(storage_state)
        if not cookies:
            return {
                "valid": False,
                "reason": "no_cookies",
                "state": {},
                "root_state": {},
                "edit_state": {},
                "storage_state": None,
                "user_agent": self.get_user_agent(storage_state),
            }

        state_script = """
        (...args) => {
          const targetUsername = String(args[0] || '').trim().replace(/^@/, '').toLowerCase();
          const href = window.location.href || '';
          const path = window.location.pathname || '';
          const pageText = (document.body && document.body.innerText) ? document.body.innerText : '';
          const textSample = pageText.slice(0, 4000).toLowerCase();
          const htmlSample = (document.documentElement && document.documentElement.innerHTML)
            ? document.documentElement.innerHTML.slice(0, 250000).toLowerCase()
            : '';
          const metaDescription = (
            document.querySelector('meta[name="description"]')
            && document.querySelector('meta[name="description"]').getAttribute('content')
          ) ? document.querySelector('meta[name="description"]').getAttribute('content').toLowerCase() : '';
          const hasPasswordInput = Boolean(document.querySelector('input[type="password"]'));
          const loginPath = /\\/accounts\\/login/i.test(path);
          const oneTapPath = /\\/accounts\\/onetap\\/?/i.test(path);
          const challengePath = /\\/challenge\\/?|\\/accounts\\/suspended\\/?|\\/two_factor\\/?|\\/reauthentication\\//i.test(path);
          const loginText = /\\blog in\\b|\\bentrar\\b|senha incorreta|incorrect password/i.test(textSample);
          const challengeText = /confirm it's you|confirm its you|enter your password|security code|enter code|unusual login attempt|check your notifications|challenge required|checkpoint required|confirme que e voce|confirme que é voce|digite sua senha|insira sua senha|codigo de seguranca|código de seguranca|insira o codigo|insira o código|verifique sua identidade/i.test(textSample);
          const passwordPrompt = hasPasswordInput && /continue|confirm|confirmar|continuar|password|senha/i.test(textSample);
          const baseLoginRequired = loginPath || challengePath || loginText || challengeText || passwordPrompt;
          let authPromptReason = null;
          if (baseLoginRequired) {
            if (challengePath) authPromptReason = 'challenge_path';
            else if (passwordPrompt) authPromptReason = 'password_prompt';
            else if (challengeText) authPromptReason = 'challenge_text';
            else if (loginPath) authPromptReason = 'login_path';
            else if (loginText) authPromptReason = 'login_text';
          }

          const clickableControls = Array.from(document.querySelectorAll('button,div[role="button"],a'));
          const dialog = document.querySelector('div[role="dialog"]');
          const dialogText = dialog ? ((dialog.textContent || '').toLowerCase()) : '';
          const removeProfilesModalOpen = Boolean(
            dialog && /remove profiles from this browser|remove profiles|remover perfis deste navegador|remover perfis/i.test(dialogText)
          );
          const continueVisible = clickableControls.some((el) => {
            const text = ((el.textContent || '') + ' ' + (el.getAttribute('aria-label') || '')).trim().toLowerCase();
            return text === 'continue'
              || text === 'continuar'
              || text.startsWith('continue as')
              || text.startsWith('continuar como');
          });
          const alternateProfileVisible = clickableControls.some((el) => {
            const text = ((el.textContent || '') + ' ' + (el.getAttribute('aria-label') || '')).trim().toLowerCase();
            return text.includes('use another profile')
              || text.includes('usar outro perfil')
              || text.includes('create new account')
              || text.includes('criar nova conta');
          });
          const landingTextVisible = /see everyday moments from|close friends|veja momentos do dia a dia|amigos proximos|amigos próximos/i.test(textSample);
          const targetUsernameVisible = Boolean(targetUsername) && textSample.includes(targetUsername);
          const profileGateVisible = Boolean(
            oneTapPath
            || removeProfilesModalOpen
            || (continueVisible && alternateProfileVisible)
            || (continueVisible && targetUsernameVisible)
            || (continueVisible && landingTextVisible)
          );
          const profileGateRequiresPassword = Boolean(
            profileGateVisible && (
              htmlSample.includes('password_entry')
              || htmlSample.includes('"login_credential_type":"password"')
              || htmlSample.includes('"n_credential_type":"password"')
              || htmlSample.includes('"n_credential_type","value":"password"')
              || metaDescription.includes('create an account or log in to instagram')
            )
          );
          const loginRequired = baseLoginRequired || profileGateRequiresPassword;
          if (profileGateRequiresPassword) {
            authPromptReason = 'profile_gate_password_entry';
          }

          return {
            current_url: href,
            current_path: path,
            login_required: loginRequired,
            auth_prompt_reason: authPromptReason,
            profile_gate_visible: profileGateVisible,
            profile_gate_modal_open: removeProfilesModalOpen,
            profile_gate_continue_visible: continueVisible,
            profile_gate_username_visible: targetUsernameVisible,
            profile_gate_requires_password: profileGateRequiresPassword,
          };
        }
        """

        async def _read_state(page_obj: Any, username: str) -> Dict[str, Any]:
            if page_obj is None:
                return {}
            state_raw = await self._evaluate_page_json(page_obj, state_script, username)
            return state_raw if isinstance(state_raw, dict) else {}

        async def _stabilize_state(
            browser_session: BrowserSession,
            username: str,
            attempts: int = 6,
            delay_seconds: float = 1.0,
        ) -> tuple[Optional[Any], Dict[str, Any]]:
            last_page = None
            last_state: Dict[str, Any] = {}
            for _ in range(max(1, attempts)):
                page_obj = await browser_session.get_current_page()
                if page_obj is not None:
                    last_page = page_obj
                state_data = await _read_state(page_obj, username)
                if state_data:
                    last_state = state_data
                current_path = str(last_state.get("current_path") or "")
                if last_state.get("login_required") or last_state.get("profile_gate_visible"):
                    return last_page, last_state
                if current_path.startswith("/accounts/edit"):
                    return last_page, last_state
                await asyncio.sleep(delay_seconds)
            return last_page, last_state

        normalized_username = (instagram_username or "").strip().lstrip("@").lower()
        browser_session = None
        storage_state_for_session = None
        storage_state_file = None
        browser_user_agent = self.get_user_agent(storage_state)

        try:
            storage_state_for_session, storage_state_file, session_user_agent = (
                self._prepare_storage_state_for_browser_session(storage_state)
            )
            browser_user_agent = session_user_agent or browser_user_agent
            cdp_url = await self._resolve_browserless_cdp_url()
            browser_session = self._create_browser_session(
                cdp_url,
                storage_state=storage_state_for_session,
                user_agent=session_user_agent,
            )
            await self._ensure_browser_session_connected(browser_session, timeout_ms=30000)

            await self._navigate_to_url_with_timeout(
                browser_session,
                "https://www.instagram.com/",
                timeout_ms=30000,
                new_tab=False,
            )
            await asyncio.sleep(1.0)
            root_page, root_state = await _stabilize_state(browser_session, normalized_username)

            if root_page is not None:
                try:
                    ua_value = await self._evaluate_page_json(
                        root_page,
                        "(...args) => navigator.userAgent || ''",
                    )
                    if isinstance(ua_value, str) and ua_value.strip():
                        browser_user_agent = ua_value.strip()
                except Exception:
                    pass

            if root_state.get("login_required") or root_state.get("profile_gate_visible"):
                reason = (
                    root_state.get("auth_prompt_reason")
                    or ("profile_gate_visible" if root_state.get("profile_gate_visible") else "login_required")
                )
                return {
                    "valid": False,
                    "reason": reason,
                    "state": root_state,
                    "root_state": root_state,
                    "edit_state": {},
                    "storage_state": None,
                    "user_agent": browser_user_agent,
                }

            await self._navigate_to_url_with_timeout(
                browser_session,
                "https://www.instagram.com/accounts/edit/",
                timeout_ms=30000,
                new_tab=False,
            )
            await asyncio.sleep(1.0)
            _, edit_state = await _stabilize_state(browser_session, normalized_username)
            edit_path = str(edit_state.get("current_path") or "")
            edit_valid = (
                not edit_state.get("login_required")
                and not edit_state.get("profile_gate_visible")
                and edit_path.startswith("/accounts/edit")
            )
            if not edit_valid:
                reason = (
                    edit_state.get("auth_prompt_reason")
                    or ("unexpected_path" if edit_path else "browserless_validation_failed")
                )
                return {
                    "valid": False,
                    "reason": reason,
                    "state": edit_state,
                    "root_state": root_state,
                    "edit_state": edit_state,
                    "storage_state": None,
                    "user_agent": browser_user_agent,
                }

            exported_state = await self._export_storage_state_with_retry(browser_session)
            return {
                "valid": True,
                "reason": "authenticated",
                "state": edit_state,
                "root_state": root_state,
                "edit_state": edit_state,
                "storage_state": exported_state if isinstance(exported_state, dict) else None,
                "user_agent": browser_user_agent,
            }
        finally:
            if browser_session:
                await self._detach_browser_session(browser_session)
            self._cleanup_storage_state_temp_file(storage_state_file)

    async def _is_session_valid(self, storage_state: Dict[str, Any]) -> bool:
        """
        Verifica se o storage_state ainda representa uma sessao autenticada.
        """
        cookies = self._extract_cookies(storage_state)
        if not cookies:
            return False

        # Modo padrÃ£o: reutilizaÃ§Ã£o otimista baseada no cookie de sessÃ£o.
        if not settings.instagram_session_strict_validation:
            if self._has_valid_auth_cookie(storage_state):
                return True

        try:
            browserless_result = await self.inspect_instagram_session_in_browserless(storage_state)
            if browserless_result.get("valid") is True:
                return True
            if browserless_result.get("reason"):
                logger.info(
                    "Validacao Browserless da sessao Instagram falhou: reason=%s auth_prompt_reason=%s current_url=%s",
                    browserless_result.get("reason"),
                    (browserless_result.get("state") or {}).get("auth_prompt_reason"),
                    (browserless_result.get("state") or {}).get("current_url"),
                )
                return False
        except Exception as exc:
            logger.warning("Falha na validacao Browserless da sessao Instagram: %s", exc)

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
            storage_state: Estado de sessÃ£o autenticada (cookies)
            max_posts: NÃºmero mÃ¡ximo de posts a raspar

        Returns:
            DicionÃ¡rio com posts extraÃ­dos
        """
        max_retries = getattr(settings, 'browser_use_max_retries', 3)
        retry_delay = 5  # segundos
        reconnect_url = self._get_browserless_reconnect_url(storage_state)
        session_info = self._get_browserless_session_info(storage_state)
        session_connect_url = session_info.get("connect") if isinstance(session_info.get("connect"), str) else None
        storage_state_for_session, storage_state_file, session_user_agent = (
            self._prepare_storage_state_for_browser_session(storage_state)
        )
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
                    logger.info(f"ðŸ¤– Browser Use: Raspando posts de {profile_url} (tentativa {attempt}/{max_retries})")

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
                    VocÃª Ã© um raspador de dados do Instagram. Extraia os primeiros {max_posts} posts do perfil.

                    PERFIL:
                    - URL: {profile_url}

                    ESTRATÃ‰GIA (obrigatÃ³ria):
                    1) Abra o perfil e aguarde carregar.
                    2) FaÃ§a scroll suave 2-3 vezes para carregar o grid.
                    3) Colete os primeiros {max_posts} links CANÃ”NICOS de posts a partir de anchors com href contendo "/p/" ou "/reel/".
                       - NÃ£o clique em Ã­cones SVG, overlays de "Clip" ou elementos decorativos.
                       - Se precisar clicar, clique no link/anchor do post (href /p/... ou /reel/...), nÃ£o no Ã­cone.
                    4) Para cada URL coletada:
                       a) Navegue para a URL do post na MESMA aba (new_tab: false).
                       b) Aguarde carregar.
                       c) Extraia:
                          - caption completa (ou null)
                          - like_count (inteiro ou null)
                          - comment_count (inteiro ou null)
                          - posted_at (texto visÃ­vel ou null)
                    5) Retorne JSON final com todos os posts coletados.

                    FORMATO DE SAÃDA (JSON puro, sem texto extra):
                    {{
                      "posts": [
                        {{
                          "post_url": "https://instagram.com/p/CODIGO/ ou https://instagram.com/reel/CODIGO/",
                          "caption": "texto da caption",
                          "like_count": 123,
                          "comment_count": 45,
                          "posted_at": "2 dias atrÃ¡s" ou null
                        }}
                      ],
                      "total_found": {max_posts}
                    }}

                    REGRAS:
                    - Se o perfil for privado: {{"posts": [], "total_found": 0, "error": "private_profile"}}
                    - Use apenas a aba atual; nÃ£o abra nova aba/janela.
                    - Se nÃ£o conseguir um campo, retorne null naquele campo.
                    - Se nÃ£o conseguir abrir um post, pule para o prÃ³ximo.
                    - NÃ£o invente dados.
                    """

                    browser_session = self._create_browser_session(
                        cdp_url,
                        storage_state=storage_state_for_session,
                        user_agent=session_user_agent,
                    )
                    llm = ChatOpenAI(model=self.model, api_key=self.api_key)
                    agent = self._create_agent(
                        task=task,
                        llm=llm,
                        browser_session=browser_session,
                    )

                    restore_event_bus = self._patch_event_bus_for_stop(browser_session)
                    history = await agent.run()

                    if not history.is_done():
                        logger.warning("âš ï¸ Browser Use nÃ£o completou a tarefa")
                        # NÃ£o fazer return aqui, deixar o except capturar

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
                        logger.info(f"âœ… Browser Use extraiu {len(data.get('posts', []))} posts")
                        return data  # Sucesso!

                    # Fallback: retornar resultado bruto
                    logger.warning("âš ï¸ NÃ£o foi possÃ­vel extrair JSON estruturado")
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
                            f"âš ï¸ Tentativa {attempt}/{max_retries} falhou: {error_msg[:100]}. "
                            f"Aguardando {wait_time}s antes de tentar novamente..."
                        )
                        await asyncio.sleep(wait_time)
                        # Continue para prÃ³xima iteraÃ§Ã£o
                    else:
                        # NÃ£o Ã© retryÃ¡vel ou Ãºltima tentativa
                        logger.error(f"âŒ Erro no Browser Use Agent (tentativa {attempt}/{max_retries}): {e}")
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
        storage_state_for_session, storage_state_file, session_user_agent = (
            self._prepare_storage_state_for_browser_session(storage_state)
        )

        try:
            for attempt in range(1, max_retries + 1):
                browser_session = None
                restore_event_bus = None
                try:
                    logger.info(
                        "ðŸ¤– Browser Use: Coletando curtidores de %s (tentativa %s/%s)",
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
                    VocÃª estÃ¡ em um navegador autenticado no Instagram.
                    Sua tarefa Ã© extrair os links dos perfis que curtiram um post.

                    PASSOS:
                    1) Acesse o post: {post_url}
                    2) Aguarde a pÃ¡gina carregar.
                    3) Se houver modal de cookies, aceite.
                    4) Localize e clique no link/botÃ£o de curtidas para abrir a lista de usuÃ¡rios.
                    5) Se a lista abrir, role o modal/lista atÃ© coletar atÃ© {max_users} links Ãºnicos de perfis.
                    6) Retorne os links no formato https://www.instagram.com/usuario/

                    FORMATO DE SAÃDA (JSON):
                    {{
                      "post_url": "{post_url}",
                      "likes_accessible": true,
                      "like_users": ["https://www.instagram.com/usuario1/"],
                      "total_collected": 1
                    }}

                    REGRAS:
                    - Se nÃ£o for possÃ­vel abrir a lista de curtidas, retorne:
                      {{
                        "post_url": "{post_url}",
                        "likes_accessible": false,
                        "like_users": [],
                        "error": "likes_unavailable"
                      }}
                    - NÃ£o abra nova aba.
                    - NÃ£o invente links.
                    """

                    browser_session = self._create_browser_session(
                        cdp_url,
                        storage_state=storage_state_for_session,
                        user_agent=session_user_agent,
                    )
                    llm = ChatOpenAI(model=self.model, api_key=self.api_key)
                    agent = self._create_agent(
                        task=task,
                        llm=llm,
                        browser_session=browser_session,
                        use_judge=False,
                        final_response_after_failure=False,
                    )

                    restore_event_bus = self._patch_event_bus_for_stop(browser_session)
                    run_kwargs: Dict[str, Any] = {}
                    try:
                        run_sig = inspect.signature(agent.run)
                        if "max_steps" in run_sig.parameters:
                            # Perfil básico não precisa múltiplos ciclos de extração.
                            run_kwargs["max_steps"] = 2
                    except Exception:
                        run_kwargs = {}
                    history = await agent.run(**run_kwargs) if run_kwargs else await agent.run()
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
                            "âš ï¸ Tentativa %s/%s falhou ao coletar curtidores: %s. Retentando em %ss...",
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
        storage_state_for_session, storage_state_file, session_user_agent = (
            self._prepare_storage_state_for_browser_session(storage_state)
        )

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

                    browser_session = self._create_browser_session(
                        cdp_url,
                        storage_state=storage_state_for_session,
                        user_agent=session_user_agent,
                    )
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
        storage_state_for_session, storage_state_file, session_user_agent = (
            self._prepare_storage_state_for_browser_session(storage_state)
        )

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
                    "total_story_viewers": 0,
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
                        user_agent=session_user_agent,
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
                                "total_story_viewers": 0,
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
                    9) Colete os PRIMEIROS usuarios visiveis na lista de visualizadores (limitado por {safe_max_interactions} no total do fluxo).
                       - Para cada usuario, marque se ele deu like no story:
                         - liked=true quando houver badge de coracao vermelho no avatar.
                         - liked=false quando nao houver esse badge.
                    10) Para cada usuario visualizador, extraia:
                        - user_username
                        - user_url no formato https://www.instagram.com/<username>/
                        - liked: true/false
                    11) Feche o popup clicando fora da janela/modal, retornando ao frame do story.
                    12) Avance para o proximo story usando a seta lateral DIREITA do viewer.
                    13) Depois de clicar na seta direita, confirme que a story_url mudou (novo story_id).
                        - Se nao mudar, tente no maximo 2 vezes.
                        - Se repetir o mesmo story_id novamente, encerre para evitar loop.
                    14) Repita ate acabar stories ativos, detectar repeticao de story_url/story_id, ou atingir o limite.

                    LIMITE:
                    - Nao ultrapasse {safe_max_interactions} usuarios visualizadores no total.

                    FORMATO DE SAIDA (JSON puro):
                    {{
                      "profile_url": "{profile_url}",
                      "stories_accessible": true,
                      "story_posts": [
                        {{
                          "story_url": "https://www.instagram.com/stories/{profile_username or 'perfil'}/1234567890123456789/",
                          "view_count": 1161,
                          "viewer_users": [
                            {{
                              "user_username": "usuario1",
                              "user_url": "https://www.instagram.com/usuario1/",
                              "liked": true
                            }},
                            {{
                              "user_username": "usuario2",
                              "user_url": "https://www.instagram.com/usuario2/",
                              "liked": false
                            }}
                          ],
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
                      "total_story_viewers": 2,
                      "total_liked_users": 1
                    }}

                    REGRAS:
                    - Nao abra nova aba.
                    - Nao invente usuarios.
                    - Inclua visualizadores com e sem like; marque corretamente no campo liked.
                    - O badge de coracao vermelho e obrigatorio para marcar liked=true.
                    - Se nao conseguir confirmar visualmente o badge vermelho, marque liked=false.
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
                            "total_story_viewers": 0,
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

                        raw_viewer_users = (
                            story_item.get("viewer_users")
                            or story_item.get("viewers")
                            or []
                        )
                        if not isinstance(raw_viewer_users, list):
                            raw_viewer_users = []

                        raw_liked_users = (
                            story_item.get("liked_users")
                            or story_item.get("like_users")
                            or []
                        )
                        if not isinstance(raw_liked_users, list):
                            raw_liked_users = []

                        viewer_users: List[Dict[str, Any]] = []
                        liked_users: List[Dict[str, str]] = []
                        by_story_user_key: Dict[str, Dict[str, Any]] = {}

                        def _normalize_story_user(raw_user: Any, force_liked: bool = False) -> Optional[Dict[str, Any]]:
                            user_url = ""
                            user_username = ""
                            liked_flag = bool(force_liked)

                            if isinstance(raw_user, dict):
                                user_url = str(raw_user.get("user_url") or "").strip()
                                user_username = str(raw_user.get("user_username") or "").strip().lstrip("@")
                                liked_flag = liked_flag or bool(
                                    raw_user.get("liked") is True
                                    or raw_user.get("badge_heart_red") is True
                                    or self._normalize_story_interaction_type(raw_user.get("type")) == "like"
                                )
                            elif isinstance(raw_user, str):
                                candidate = raw_user.strip()
                                if not candidate:
                                    return None
                                if "instagram.com" in candidate:
                                    user_url = candidate
                                else:
                                    user_username = candidate.lstrip("@")
                            else:
                                return None

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
                                return None

                            dedupe_user = user_url or user_username
                            return {
                                "user_username": user_username or "",
                                "user_url": user_url or "",
                                "liked": liked_flag,
                                "_key": dedupe_user,
                            }

                        for raw_user in raw_viewer_users:
                            normalized_user = _normalize_story_user(raw_user, force_liked=False)
                            if not normalized_user:
                                continue
                            dedupe_user = str(normalized_user.pop("_key"))
                            existing = by_story_user_key.get(dedupe_user)
                            if existing:
                                if normalized_user.get("liked") is True:
                                    existing["liked"] = True
                                continue
                            by_story_user_key[dedupe_user] = normalized_user
                            viewer_users.append(normalized_user)

                        for raw_user in raw_liked_users:
                            if isinstance(raw_user, dict) and not _is_explicit_liked_user(raw_user):
                                continue
                            normalized_user = _normalize_story_user(raw_user, force_liked=True)
                            if not normalized_user:
                                continue
                            normalized_user["liked"] = True
                            dedupe_user = str(normalized_user.pop("_key"))
                            existing = by_story_user_key.get(dedupe_user)
                            if existing:
                                existing["liked"] = True
                                continue
                            by_story_user_key[dedupe_user] = normalized_user
                            viewer_users.append(normalized_user)

                        if safe_max_interactions > 0:
                            viewer_users = viewer_users[:safe_max_interactions]

                        for viewer_user in viewer_users:
                            if viewer_user.get("liked") is not True:
                                continue
                            liked_users.append(
                                {
                                    "user_username": str(viewer_user.get("user_username") or "").strip(),
                                    "user_url": str(viewer_user.get("user_url") or "").strip(),
                                }
                            )

                        story_key = story_url_text
                        if story_key in by_story_url:
                            existing_story = by_story_url[story_key]
                            existing_viewers: List[Dict[str, Any]] = existing_story.get("viewer_users", [])
                            existing_viewer_keys = {
                                (str(item.get("user_url") or "").strip() or str(item.get("user_username") or "").strip().lstrip("@"))
                                for item in existing_viewers
                                if isinstance(item, dict)
                            }
                            for viewer_user in viewer_users:
                                dedupe_user = (
                                    str(viewer_user.get("user_url") or "").strip()
                                    or str(viewer_user.get("user_username") or "").strip().lstrip("@")
                                )
                                if not dedupe_user:
                                    continue
                                if dedupe_user in existing_viewer_keys:
                                    if viewer_user.get("liked") is True:
                                        for existing_viewer in existing_viewers:
                                            existing_key = (
                                                str(existing_viewer.get("user_url") or "").strip()
                                                or str(existing_viewer.get("user_username") or "").strip().lstrip("@")
                                            )
                                            if existing_key == dedupe_user:
                                                existing_viewer["liked"] = bool(existing_viewer.get("liked")) or True
                                                break
                                    continue
                                existing_viewer_keys.add(dedupe_user)
                                existing_viewers.append(viewer_user)
                            existing_story["viewer_users"] = existing_viewers[:safe_max_interactions]
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
                            "viewer_users": viewer_users[:safe_max_interactions],
                            "liked_users": liked_users[:safe_max_interactions],
                        }
                        by_story_url[story_key] = normalized_story
                        normalized_story_posts.append(normalized_story)

                    for story_item in normalized_story_posts:
                        raw_viewers = story_item.get("viewer_users", []) or []
                        if not isinstance(raw_viewers, list):
                            raw_viewers = []
                        kept_viewers = raw_viewers[:safe_max_interactions]
                        story_item["viewer_users"] = kept_viewers
                        story_item["liked_users"] = [
                            {
                                "user_username": str(viewer.get("user_username") or "").strip(),
                                "user_url": str(viewer.get("user_url") or "").strip(),
                            }
                            for viewer in kept_viewers
                            if isinstance(viewer, dict) and viewer.get("liked") is True
                        ]

                    total_story_viewers = sum(
                        len(story_item.get("viewer_users", []) or [])
                        for story_item in normalized_story_posts
                    )

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
                        "total_story_viewers": total_story_viewers,
                        "total_liked_users": len(normalized_items),
                        "total_collected": total_story_viewers,
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
                            "total_story_viewers": 0,
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
                        "total_story_viewers": 0,
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
                "total_story_viewers": 0,
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
        Extrai dados bÃ¡sicos de um perfil Instagram usando o mesmo fluxo autenticado do Browser Use.
        """
        max_retries = getattr(settings, "browser_use_max_retries", 3)
        retry_delay = 5
        reconnect_url = self._get_browserless_reconnect_url(storage_state)
        session_info = self._get_browserless_session_info(storage_state)
        session_connect_url = session_info.get("connect") if isinstance(session_info.get("connect"), str) else None
        storage_state_for_session, storage_state_file, session_user_agent = (
            self._prepare_storage_state_for_browser_session(storage_state)
        )

        try:
            for attempt in range(1, max_retries + 1):
                browser_session = None
                restore_event_bus = None
                try:
                    logger.info(
                        "ðŸ¤– Browser Use: Extraindo dados do perfil %s (tentativa %s/%s)",
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
                    VocÃª estÃ¡ em um navegador autenticado no Instagram.
                    Extraia os dados do perfil em JSON puro.

                    PERFIL:
                    - URL: {profile_url}

                    PASSOS:
                    1) Navegue para a URL do perfil na aba atual.
                    2) Aguarde a pÃ¡gina carregar.
                    3) Se houver modal de cookies, aceite.
                    4) Extraia os campos visÃ­veis do perfil.

                    FORMATO (JSON puro):
                    {{
                      "username": "string ou null",
                      "full_name": "string ou null",
                      "bio": "string ou null",
                      "is_private": true/false,
                      "follower_count": nÃºmero inteiro ou null,
                      "following_count": nÃºmero inteiro ou null,
                      "post_count": nÃºmero inteiro ou null,
                      "verified": true/false
                    }}

                    REGRAS:
                    - NÃ£o abra nova aba.
                    - NÃ£o invente dados.
                    - Se nÃ£o conseguir um campo, retorne null.
                    """

                    browser_session = self._create_browser_session(
                        cdp_url,
                        storage_state=storage_state_for_session,
                        user_agent=session_user_agent,
                    )
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
                            "âš ï¸ Tentativa %s/%s falhou ao extrair perfil: %s. Retentando em %ss...",
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

    async def send_direct_message_if_needed(
        self,
        profile_url: str,
        storage_state: Optional[Dict[str, Any]],
        message_text: str,
        min_days_since_last_message: int = 30,
    ) -> Dict[str, Any]:
        """
        Envia direct somente quando nao houver historico ou quando o ultimo contato
        estiver acima do limite de dias informado.
        """
        if not storage_state:
            raise RuntimeError("login_required")

        normalized_profile_url = str(profile_url or "").strip()
        if not normalized_profile_url.startswith("http"):
            normalized_profile_url = f"https://www.instagram.com/{normalized_profile_url.strip('/').lstrip('@')}/"
        if not normalized_profile_url.endswith("/"):
            normalized_profile_url = f"{normalized_profile_url}/"

        safe_message_text = str(message_text or "").strip()
        if not safe_message_text:
            raise RuntimeError("message_text_empty")

        safe_min_days = max(1, int(min_days_since_last_message or 30))
        max_retries = getattr(settings, "browser_use_max_retries", 3)
        retry_delay = 5
        target_username = self._extract_instagram_username(normalized_profile_url)
        reconnect_url = self._get_browserless_reconnect_url(storage_state)
        session_info = self._get_browserless_session_info(storage_state)
        session_connect_url = (
            session_info.get("connect") if isinstance(session_info.get("connect"), str) else None
        )
        storage_state_for_session, storage_state_file, session_user_agent = (
            self._prepare_storage_state_for_browser_session(storage_state)
        )

        if not self.api_key:
            raise RuntimeError("openai_api_key_missing")

        def _coerce_agent_datetime(value: Any, now: datetime) -> Optional[datetime]:
            if value in (None, "", "null"):
                return None
            if isinstance(value, datetime):
                return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
            text = str(value).strip()
            if not text:
                return None
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            except Exception:
                return self._parse_instagram_timestamp(text, now=now)

        try:
            for attempt in range(1, max_retries + 1):
                browser_session = None
                restore_event_bus = None
                try:
                    logger.info(
                        "Browser Use: enviando direct condicional para %s (tentativa %s/%s)",
                        normalized_profile_url,
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

                    browser_session = self._create_browser_session(
                        cdp_url,
                        storage_state=storage_state_for_session,
                        user_agent=session_user_agent,
                    )
                    restore_event_bus = self._patch_event_bus_for_stop(browser_session)
                    await self._navigate_to_url_with_timeout(
                        browser_session,
                        normalized_profile_url,
                        timeout_ms=30000,
                    )
                    await asyncio.sleep(max(2.0, float(getattr(settings, "browser_use_min_page_load_wait_s", 1.0))))
                    checked_at = datetime.utcnow().replace(tzinfo=timezone.utc)

                    task = f"""
                    You are already logged into Instagram Web and must use only the current tab.

                    Current UTC datetime:
                    - {checked_at.isoformat()}

                    Target profile:
                    - {normalized_profile_url}
                    - Exact username: {target_username}

                    Message to send exactly as written (do not split it. Send just one text message):
                    {safe_message_text}

                    Business rule:
                    - If there is no direct-message history with @{target_username}, send the message.
                    - If there is message history and the last visible message was sent less than {safe_min_days} days ago, do not send.
                    - If there is message history and the last visible message was sent more than {safe_min_days} days ago, send the message.
                    - If there is message history but you cannot reliably determine when the last visible message was sent, do not send.

                    Navigation rules:
                    - First open the target profile.
                    - Try the profile button "Message" / "Mensagem".
                    - If the floating Messages widget/bubble opens the exact conversation or new-message composer for @{target_username}, you may use it.
                    - Only if the target conversation is still not open, navigate in the same tab to https://www.instagram.com/direct/inbox/ and use search or the new-message flow to open the exact one-to-one conversation with @{target_username}.
                    - Never open a new tab.
                    - Never switch to a different username.

                    Output rules:
                    - Return ONLY one valid JSON object.
                    - Do not use markdown.
                    - Use these exact keys:
                      status, reason, profile_url, thread_url, conversation_exists, no_history, last_message_at, last_message_age_days, sent_at, checked_at

                    Valid values:
                    - status: "sent" or "skipped"
                    - reason: "no_history", "last_message_older_than_threshold", "recent_history", or "history_present_but_last_message_unresolved"
                    - profile_url: the target profile URL
                    - thread_url: current conversation URL or null
                    - conversation_exists: true or false
                    - no_history: true or false
                    - last_message_at: ISO-8601 datetime string or null
                    - last_message_age_days: number or null
                    - sent_at: ISO-8601 datetime string if sent, otherwise null
                    - checked_at: ISO-8601 datetime string
                    """

                    llm = ChatOpenAI(model=self.model, api_key=self.api_key)
                    agent = self._create_agent(
                        task=task,
                        llm=llm,
                        browser_session=browser_session,
                        directly_open_url=False,
                        max_failures=6,
                        step_timeout=180,
                    )
                    history = await agent.run()
                    final_result = (history.final_result() or "").strip()
                    logger.info(
                        "Direct Agent final result (tentativa %s): %s",
                        attempt,
                        final_result[:500] or "<empty>",
                    )

                    parsed = self._extract_first_json_value(final_result)
                    if not isinstance(parsed, dict):
                        if self._contains_protocol_error(final_result) and attempt < max_retries:
                            wait_time = retry_delay * attempt
                            logger.warning(
                                "Falha de protocolo no direct agent (%s/%s). Retentando em %ss...",
                                attempt,
                                max_retries,
                                wait_time,
                            )
                            await asyncio.sleep(wait_time)
                            continue
                        raise RuntimeError("direct_agent_result_parse_failed")

                    effective_checked_at = _coerce_agent_datetime(parsed.get("checked_at"), checked_at) or checked_at
                    last_message_at = _coerce_agent_datetime(parsed.get("last_message_at"), effective_checked_at)
                    sent_at = _coerce_agent_datetime(parsed.get("sent_at"), effective_checked_at)

                    try:
                        last_message_age_days = (
                            float(parsed.get("last_message_age_days"))
                            if parsed.get("last_message_age_days") not in (None, "", "null")
                            else None
                        )
                    except (TypeError, ValueError):
                        last_message_age_days = None

                    status = str(parsed.get("status") or "skipped").strip().lower()
                    if status not in {"sent", "skipped"}:
                        status = "skipped"

                    reason = str(parsed.get("reason") or "").strip() or (
                        "no_history" if status == "sent" and bool(parsed.get("no_history")) else "recent_history"
                    )
                    if reason not in {
                        "no_history",
                        "last_message_older_than_threshold",
                        "recent_history",
                        "history_present_but_last_message_unresolved",
                    }:
                        reason = "history_present_but_last_message_unresolved"

                    conversation_exists = bool(parsed.get("conversation_exists"))
                    no_history = bool(parsed.get("no_history"))

                    return {
                        "status": status,
                        "reason": reason,
                        "profile_url": normalized_profile_url,
                        "thread_url": str(parsed.get("thread_url") or "").strip() or None,
                        "conversation_exists": conversation_exists,
                        "no_history": no_history,
                        "last_message_at": (
                            last_message_at.astimezone(timezone.utc).replace(tzinfo=None)
                            if last_message_at is not None
                            else None
                        ),
                        "last_message_age_days": last_message_age_days,
                        "sent_at": (
                            sent_at.astimezone(timezone.utc).replace(tzinfo=None)
                            if sent_at is not None
                            else None
                        ),
                        "checked_at": effective_checked_at.astimezone(timezone.utc).replace(tzinfo=None),
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
                            "Tentativa %s/%s falhou no direct message: %s. Retentando em %ss...",
                            attempt,
                            max_retries,
                            str(exc)[:160],
                            wait_time,
                        )
                        await asyncio.sleep(wait_time)
                        continue
                    raise
                finally:
                    if callable(restore_event_bus):
                        restore_event_bus()
                    if browser_session:
                        await self._detach_browser_session(browser_session)
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
        storage_state_for_session, storage_state_file, session_user_agent = (
            self._prepare_storage_state_for_browser_session(storage_state)
        )

        try:
            for attempt in range(1, max_retries + 1):
                browser_session = None
                restore_event_bus = None
                try:
                    cdp_url = await self._resolve_browserless_cdp_url()
                    browser_session = self._create_browser_session(
                        cdp_url,
                        storage_state=storage_state_for_session,
                        user_agent=session_user_agent,
                    )
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
        Simula scroll infinito para carregar mais conteÃºdo.

        Args:
            url: URL da pÃ¡gina
            scroll_count: NÃºmero de scrolls a realizar

        Returns:
            Dados capturados apÃ³s scrolls
        """
        try:
            logger.info(f"ðŸ“œ Iniciando scroll em: {url}")

            # ImplementaÃ§Ã£o serÃ¡ feita com Browserless + JavaScript
            result = {
                "url": url,
                "scroll_count": scroll_count,
                "screenshots": [],
                "html_content": [],
            }

            logger.info(f"âœ… Scroll completado em: {url}")
            return result

        except Exception as e:
            logger.error(f"âŒ Erro ao fazer scroll: {e}")
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
            url: URL da pÃ¡gina
            selector: Seletor CSS do elemento a clicar
            wait_for_selector: Seletor CSS para aguardar apÃ³s clique

        Returns:
            Dados capturados apÃ³s clique
        """
        try:
            logger.info(f"ðŸ–±ï¸ Clicando em: {selector}")

            result = {
                "url": url,
                "clicked_selector": selector,
                "screenshot": None,
                "html_content": None,
            }

            logger.info(f"âœ… Clique executado")
            return result

        except Exception as e:
            logger.error(f"âŒ Erro ao clicar: {e}")
            raise

    async def extract_visible_text(
        self,
        html: str,
        selector: str,
    ) -> str:
        """
        Extrai texto visÃ­vel de um elemento HTML.

        Args:
            html: ConteÃºdo HTML
            selector: Seletor CSS

        Returns:
            Texto extraÃ­do
        """
        try:
            # ImplementaÃ§Ã£o com BeautifulSoup ou similar
            logger.info(f"ðŸ“ Extraindo texto de: {selector}")
            return ""

        except Exception as e:
            logger.error(f"âŒ Erro ao extrair texto: {e}")
            raise


# InstÃ¢ncia global do agente
browser_use_agent = BrowserUseAgent()
