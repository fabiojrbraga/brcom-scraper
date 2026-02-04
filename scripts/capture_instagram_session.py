"""
Captura storage_state do Instagram com login humano (sem IA no login).

Uso:
  python scripts/capture_instagram_session.py
  python scripts/capture_instagram_session.py --mode browserless
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
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


async def _capture(mode: str, output: Path) -> None:
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
        else:
            browser = await playwright.chromium.launch(headless=False)
            context = await browser.new_context()

        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")

        print("\n[acao manual] Faça login no Instagram no navegador aberto.")
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
            "user_agent": user_agent or None,
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"[ok] Storage state salvo em: {output}")
        await browser.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Captura storage_state do Instagram com login humano."
    )
    parser.add_argument(
        "--mode",
        choices=("local", "browserless"),
        default="local",
        help="local abre Chromium local; browserless conecta no CDP remoto.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Arquivo de saida (padrao: {DEFAULT_OUTPUT}).",
    )
    args = parser.parse_args()

    try:
        asyncio.run(_capture(args.mode, args.output))
        return 0
    except KeyboardInterrupt:
        print("\n[!] Operacao cancelada.")
        return 130
    except Exception as exc:
        print(f"[erro] Falha na captura: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
