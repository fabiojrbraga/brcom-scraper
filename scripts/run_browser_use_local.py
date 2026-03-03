"""
Executa Browser Use no navegador local para tarefas genericas.

Exemplos:
  python scripts/run_browser_use_local.py --prompt "Abra example.com e extraia o titulo"
  python scripts/run_browser_use_local.py --start-url https://example.com --prompt "Liste os links principais em JSON"
  python scripts/run_browser_use_local.py --prompt-file .\\prompt.txt --output .\\result.json
"""

import argparse
import asyncio
import inspect
import json
import sys
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


def _read_prompt(args: argparse.Namespace) -> str:
    if args.prompt and args.prompt.strip():
        return args.prompt.strip()

    if args.prompt_file:
        text = Path(args.prompt_file).read_text(encoding="utf-8").strip()
        if text:
            return text

    if not sys.stdin.isatty():
        text = sys.stdin.read().strip()
        if text:
            return text

    text = input("Digite o prompt da tarefa: ").strip()
    if text:
        return text

    raise RuntimeError("Prompt vazio. Informe --prompt, --prompt-file ou stdin.")


def _extract_first_json_value(text: str) -> Any | None:
    if not text:
        return None
    decoder = json.JSONDecoder()
    for idx, char in enumerate(text):
        if char not in ("{", "["):
            continue
        try:
            value, _ = decoder.raw_decode(text[idx:])
        except Exception:
            continue
        return value
    return None


async def _safe_close_session(session: Any) -> None:
    disconnect_fn = getattr(session, "disconnect", None)
    if callable(disconnect_fn):
        try:
            result = disconnect_fn()
            if asyncio.iscoroutine(result):
                await result
            return
        except Exception:
            pass

    stop_fn = getattr(session, "stop", None)
    if callable(stop_fn):
        try:
            result = stop_fn()
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            pass


def _build_task(user_prompt: str, start_url: str | None) -> str:
    start_url_line = f"- URL inicial obrigatoria: {start_url}" if start_url else "- URL inicial: livre"
    return f"""
Voce e um agente de automacao web em navegador local.

Contexto:
{start_url_line}

Tarefa do usuario (seguir literalmente):
{user_prompt}

Regras:
- Nao invente dados.
- Se solicitar resultado estruturado, retorne JSON puro.
- Se algo impedir a execucao, retorne um JSON com campo "error".
"""


async def _run(args: argparse.Namespace) -> int:
    from browser_use import Agent, BrowserSession, ChatOpenAI
    from config import settings

    prompt = _read_prompt(args)
    model = args.model or settings.openai_model_text
    api_key = settings.openai_api_key
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY nao configurada.")

    storage_state: str | dict[str, Any] | None = None
    if args.storage_state:
        storage_state = str(Path(args.storage_state).resolve())

    session_kwargs: dict[str, Any] = {
        "is_local": True,
        "headless": bool(args.headless),
        "keep_alive": True,
    }
    if storage_state:
        session_kwargs["storage_state"] = storage_state
    if args.user_data_dir:
        session_kwargs["user_data_dir"] = str(Path(args.user_data_dir).resolve())

    browser_session = BrowserSession(**session_kwargs)
    llm = ChatOpenAI(model=model, api_key=api_key)
    task = _build_task(prompt, args.start_url)

    agent_kwargs = {
        "task": task,
        "llm": llm,
        "browser_session": browser_session,
        "keep_browser_open": True,
        "keep_browser_session": True,
        "directly_open_url": bool(args.start_url),
    }
    try:
        sig = inspect.signature(Agent.__init__)
        filtered_kwargs = {k: v for k, v in agent_kwargs.items() if k in sig.parameters}
    except Exception:
        filtered_kwargs = agent_kwargs

    agent = Agent(**filtered_kwargs)
    history = await agent.run(max_steps=max(1, int(args.max_steps)))

    final_result = history.final_result() or ""
    parsed_result = _extract_first_json_value(final_result)
    payload = {
        "status": "success" if history.is_successful() else "failed",
        "model": model,
        "start_url": args.start_url,
        "prompt": prompt,
        "final_result": final_result,
        "parsed_result": parsed_result,
        "errors": history.errors() if hasattr(history, "errors") else [],
    }

    if args.output:
        output_path = Path(args.output).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[ok] Resultado salvo em: {output_path}")

    if args.json_only:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print("\n=== Browser Use Local Result ===")
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    if args.keep_open:
        input("\nPressione ENTER para encerrar o navegador local...")

    await _safe_close_session(browser_session)
    return 0 if history.is_successful() else 2


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Roda Browser Use localmente com prompt informado em tempo de execucao."
    )
    parser.add_argument("--prompt", type=str, help="Prompt da tarefa.")
    parser.add_argument("--prompt-file", type=Path, help="Arquivo .txt com o prompt.")
    parser.add_argument("--start-url", type=str, default=None, help="URL inicial opcional.")
    parser.add_argument("--model", type=str, default=None, help="Modelo OpenAI (default: config).")
    parser.add_argument("--max-steps", type=int, default=40, help="Maximo de passos do agente.")
    parser.add_argument("--headless", action="store_true", help="Executa sem UI (padrao: com UI local).")
    parser.add_argument("--keep-open", action="store_true", help="Mantem navegador aberto ate ENTER.")
    parser.add_argument("--storage-state", type=Path, default=None, help="Storage state JSON opcional.")
    parser.add_argument("--user-data-dir", type=Path, default=None, help="Diretorio de perfil local opcional.")
    parser.add_argument("--output", type=Path, default=None, help="Arquivo para salvar resultado JSON.")
    parser.add_argument("--json-only", action="store_true", help="Imprime somente JSON em uma linha.")
    args = parser.parse_args()

    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("\n[!] Execucao cancelada.")
        return 130
    except Exception as exc:
        print(f"[erro] Falha ao executar Browser Use local: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

