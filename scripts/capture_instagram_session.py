"""
Captura storage_state do Instagram com login humano (sem IA no login).

Uso:
  python scripts/capture_instagram_session.py
  python scripts/capture_instagram_session.py --browser chromium
  python scripts/capture_instagram_session.py --mode browserless
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


DEFAULT_OUTPUT = Path(".secrets/instagram_storage_state.json")
LOGIN_URL = "https://www.instagram.com/accounts/login/"
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


def _append_token(ws_url: str, token: str) -> str:
    parsed = urlparse(ws_url)
    query = dict(parse_qsl(parsed.query))
    if "token" not in query:
        query["token"] = token
    return urlunparse(parsed._replace(query=urlencode(query)))


def _build_browserless_cdp_url() -> str:
    try:
        from config import settings
    except Exception as exc:
        raise RuntimeError(f"Nao foi possivel carregar config.py/.env: {exc}") from exc

    token = (settings.browserless_token or "").strip()
    ws_url = (settings.browserless_ws_url or "").strip()
    host = (settings.browserless_host or "").strip()
    if not token:
        raise RuntimeError("BROWSERLESS_TOKEN nao configurado.")

    if ws_url:
        return ws_url if "token=" in ws_url else _append_token(ws_url, token)

    parsed = urlparse(host)
    if not parsed.netloc:
        raise RuntimeError("BROWSERLESS_HOST invalido.")
    scheme = "wss" if parsed.scheme in ("https", "wss") else "ws"
    base = f"{scheme}://{parsed.netloc}"
    return _append_token(base, token)


async def _launch_local_browser(playwright, browser_name: str):
    launch_kwargs = {"headless": False}
    if browser_name == "chrome":
        launch_kwargs["channel"] = "chrome"

    try:
        return await playwright.chromium.launch(**launch_kwargs)
    except Exception as exc:
        if browser_name == "chrome":
            raise RuntimeError(
                "Google Chrome local nao encontrado pelo Playwright. "
                "Instale o Chrome ou use --browser chromium."
            ) from exc
        raise RuntimeError(
            "Chromium local nao encontrado pelo Playwright. "
            "Execute: python -m playwright install chromium"
        ) from exc


def _default_chrome_user_data_dir() -> Path:
    if sys.platform.startswith("win"):
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        if local_app_data:
            return Path(local_app_data) / "Google" / "Chrome" / "User Data"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    return Path.home() / ".config" / "google-chrome"


def _default_automation_user_data_dir(browser_name: str) -> Path:
    suffix = "chromium-user-data" if browser_name == "chromium" else "chrome-user-data"
    return ROOT_DIR / ".secrets" / suffix


def _resolve_local_profile_mode(profile_mode: str, browser_name: str) -> str:
    if profile_mode != "auto":
        return profile_mode
    return "automation"


def _detect_last_used_chrome_profile(user_data_dir: Path) -> Optional[str]:
    local_state_path = user_data_dir / "Local State"
    if not local_state_path.exists():
        return None

    try:
        data = json.loads(local_state_path.read_text(encoding="utf-8"))
    except Exception:
        return None

    profile_data = data.get("profile") or {}
    last_used = str(profile_data.get("last_used") or "").strip()
    return last_used or None


async def _launch_local_context(
    playwright,
    browser_name: str,
    profile_mode: str,
    chrome_user_data_dir: Optional[Path],
    chrome_profile_directory: Optional[str],
):
    resolved_profile_mode = _resolve_local_profile_mode(profile_mode, browser_name)

    if resolved_profile_mode == "automation":
        user_data_dir = chrome_user_data_dir or _default_automation_user_data_dir(browser_name)
        user_data_dir.mkdir(parents=True, exist_ok=True)

        launch_kwargs = {
            "user_data_dir": str(user_data_dir),
            "headless": False,
        }
        if browser_name == "chrome":
            launch_kwargs["channel"] = "chrome"

        print(f"[i] Abrindo perfil persistente de automacao ({browser_name}) em: {user_data_dir}")

        try:
            context = await playwright.chromium.launch_persistent_context(**launch_kwargs)
        except Exception as exc:
            raise RuntimeError(
                "Falha ao abrir o perfil persistente de automacao. "
                "Se quiser um navegador limpo, use --profile-mode isolated."
            ) from exc

        return context, context, resolved_profile_mode, user_data_dir, None

    if resolved_profile_mode == "system":
        if browser_name != "chrome":
            raise RuntimeError(
                "O perfil local do sistema so e suportado com --browser chrome. "
                "Use --profile-mode isolated com Chromium."
            )

        user_data_dir = chrome_user_data_dir or _default_chrome_user_data_dir()
        profile_directory = (
            (chrome_profile_directory or "").strip()
            or _detect_last_used_chrome_profile(user_data_dir)
        )
        raise RuntimeError(
            "Usar o perfil padrao do Chrome via automacao nao e suportado nas versoes "
            "atuais do Chrome/Playwright. Use o perfil persistente padrao do script "
            f"(.secrets/chrome-user-data) ou --profile-mode isolated. Perfil detectado: "
            f"{user_data_dir}"
            + (f" [{profile_directory}]" if profile_directory else "")
        )

    browser = await _launch_local_browser(playwright, browser_name)
    context = await browser.new_context()
    return context, browser, resolved_profile_mode, None, None


async def _capture(
    mode: str,
    output: Path,
    browser_name: str,
    profile_mode: str,
    chrome_user_data_dir: Optional[Path],
    chrome_profile_directory: Optional[str],
) -> None:
    try:
        from playwright.async_api import async_playwright
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Playwright nao instalado. Execute: python -m pip install -r requirements.txt"
        ) from exc

    async with async_playwright() as playwright:
        if mode == "browserless":
            cdp_url = _build_browserless_cdp_url()
            print(f"[i] Conectando ao Browserless via CDP: {cdp_url.split('?')[0]}")
            browser = await playwright.chromium.connect_over_cdp(cdp_url)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            close_target = browser
            resolved_profile_mode = None
            resolved_user_data_dir = None
            resolved_profile_directory = None
        else:
            (
                context,
                close_target,
                resolved_profile_mode,
                resolved_user_data_dir,
                resolved_profile_directory,
            ) = await _launch_local_context(
                playwright,
                browser_name,
                profile_mode,
                chrome_user_data_dir,
                chrome_profile_directory,
            )

        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")

        print("\n[acao manual] Faca login no Instagram no navegador aberto.")
        print("[acao manual] Complete 2FA/challenge se necessario.")
        input("Quando terminar e estiver logado, pressione ENTER para salvar a sessao...")

        storage_state = await context.storage_state()
        user_agent = ""
        try:
            user_agent = str(await page.evaluate("() => navigator.userAgent")).strip()
        except Exception:
            user_agent = ""

        payload = dict(storage_state)
        payload["_meta"] = {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "capture_mode": mode,
            "local_browser": browser_name if mode == "local" else None,
            "local_profile_mode": resolved_profile_mode if mode == "local" else None,
            "local_user_data_dir": (
                str(resolved_user_data_dir) if mode == "local" and resolved_user_data_dir else None
            ),
            "chrome_user_data_dir": (
                str(resolved_user_data_dir) if mode == "local" and resolved_user_data_dir else None
            ),
            "chrome_profile_directory": (
                resolved_profile_directory if mode == "local" else None
            ),
            "user_agent": user_agent or None,
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"[ok] Storage state salvo em: {output}")
        await close_target.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Captura storage_state do Instagram com login humano."
    )
    parser.add_argument(
        "--mode",
        choices=("local", "browserless"),
        default="local",
        help="local abre um navegador local; browserless conecta no CDP remoto.",
    )
    parser.add_argument(
        "--browser",
        choices=("chromium", "chrome"),
        default="chromium",
        help="Navegador do modo local: Chromium do Playwright (padrao) ou Google Chrome.",
    )
    parser.add_argument(
        "--profile-mode",
        choices=("auto", "automation", "isolated", "system"),
        default="auto",
        help="No modo local: auto usa perfil persistente proprio para Chromium/Chrome.",
    )
    parser.add_argument(
        "--user-data-dir",
        "--chrome-user-data-dir",
        dest="chrome_user_data_dir",
        type=Path,
        help="Diretorio User Data para perfil persistente local. Padrao: .secrets/chromium-user-data ou .secrets/chrome-user-data.",
    )
    parser.add_argument(
        "--profile-directory",
        "--chrome-profile-directory",
        dest="chrome_profile_directory",
        help="Subperfil do Chrome, ex.: Default ou Profile 1. Usado apenas com --profile-mode system.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Arquivo de saida (padrao: {DEFAULT_OUTPUT}).",
    )
    args = parser.parse_args()

    try:
        asyncio.run(
            _capture(
                args.mode,
                args.output,
                args.browser,
                args.profile_mode,
                args.chrome_user_data_dir,
                args.chrome_profile_directory,
            )
        )
        return 0
    except KeyboardInterrupt:
        print("\n[!] Operacao cancelada.")
        return 130
    except Exception as exc:
        print(f"[erro] Falha na captura: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
