"""
Importa storage_state do Instagram para a tabela instagram_sessions.

Uso:
  python scripts/import_instagram_session.py --username meu_usuario
  python scripts/import_instagram_session.py --skip-validation
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

DEFAULT_INPUT = Path(".secrets/instagram_storage_state.json")


def _load_runtime_dependencies():
    try:
        from app.database import SessionLocal
        from app.models import InstagramSession
        from app.scraper.browser_use_agent import browser_use_agent
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Dependencias ausentes. Execute: python -m pip install -r requirements.txt"
        ) from exc
    return SessionLocal, InstagramSession, browser_use_agent


def _load_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Arquivo nao encontrado: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("storage_state precisa ser um JSON objeto.")
    return data


def _extract_user_agent(raw_state: dict[str, Any]) -> str | None:
    meta = raw_state.get("_meta")
    if isinstance(meta, dict):
        user_agent = meta.get("user_agent")
        if isinstance(user_agent, str) and user_agent.strip():
            return user_agent.strip()
    legacy_user_agent = raw_state.get("_user_agent")
    if isinstance(legacy_user_agent, str) and legacy_user_agent.strip():
        return legacy_user_agent.strip()
    return None


async def _validate_state(agent: Any, clean_state: dict[str, Any], skip_validation: bool) -> int:
    cookies = agent.get_cookies(clean_state)
    if not cookies:
        raise RuntimeError("storage_state sem cookies. Login nao parece valido.")

    if skip_validation:
        return len(cookies)

    is_valid = await agent._is_session_valid(clean_state)
    if not is_valid:
        raise RuntimeError(
            "Sessao invalida pelo check HTTP. Refaca captura apos login manual."
        )
    return len(cookies)


def _persist_session(
    session_factory: Any,
    session_model: Any,
    clean_state: dict[str, Any],
    username: str | None,
) -> tuple[str, int]:
    db = session_factory()
    try:
        deactivate_query = db.query(session_model).filter(session_model.is_active.is_(True))
        if username:
            deactivate_query = deactivate_query.filter(session_model.instagram_username == username)
        else:
            deactivate_query = deactivate_query.filter(session_model.instagram_username.is_(None))
        deactivated = deactivate_query.update({session_model.is_active: False}, synchronize_session=False)

        session = session_model(
            instagram_username=username,
            storage_state=clean_state,
            is_active=True,
            last_used_at=datetime.utcnow(),
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        return session.id, int(deactivated or 0)
    finally:
        db.close()


async def _run(args: argparse.Namespace) -> None:
    session_factory, session_model, agent = _load_runtime_dependencies()
    raw_state = _load_json_file(args.input)
    clean_state = agent._sanitize_storage_state(raw_state)
    if not isinstance(clean_state, dict):
        raise RuntimeError("storage_state invalido. Esperado JSON com cookies/origins.")
    user_agent = _extract_user_agent(raw_state)
    if user_agent:
        clean_state["_meta"] = {"user_agent": user_agent}

    cookie_count = await _validate_state(agent, clean_state, args.skip_validation)
    username = args.username.strip().lstrip("@").lower() if args.username else None
    session_id, deactivated = _persist_session(
        session_factory,
        session_model,
        clean_state,
        username or None,
    )

    print(f"[ok] Sessao importada. session_id={session_id}")
    print(f"[ok] Cookies no storage_state: {cookie_count}")
    if user_agent:
        print(f"[ok] User-Agent preservado: {user_agent[:120]}")
    print(f"[ok] Sessoes antigas desativadas: {deactivated}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Importa storage_state do Instagram no banco."
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_INPUT,
        help=f"Arquivo JSON de entrada (padrao: {DEFAULT_INPUT}).",
    )
    parser.add_argument(
        "--username",
        type=str,
        default="",
        help="Username associado a sessao (opcional).",
    )
    parser.add_argument(
        "--skip-validation",
        action="store_true",
        help="Pula validacao HTTP antes de salvar a sessao.",
    )
    args = parser.parse_args()

    try:
        asyncio.run(_run(args))
        return 0
    except KeyboardInterrupt:
        print("\n[!] Operacao cancelada.")
        return 130
    except Exception as exc:
        print(f"[erro] Falha no import: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
