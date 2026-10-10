from __future__ import annotations

import asyncio
import os
import socket
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import httpx
import uvicorn

from eval.credentials import mint_disposable_token


def _patched_chat_completion_factory(reply_text: str = "pong-e2e"):
    from llm.router import LLMResponse

    async def fake_chat_completion(**kwargs):
        return LLMResponse(
            content=reply_text,
            tool_calls=[],
            finish_reason="stop",
            usage={"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11},
            reasoning_content=None,
        )

    return fake_chat_completion


def _wait_health(host: str, port: int, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    url = f"http://{host}:{port}/api/health"
    while time.time() < deadline:
        try:
            r = httpx.get(url, timeout=1.0)
            if r.status_code == 200:
                return True
        except (httpx.HTTPError, OSError):
            pass
        time.sleep(0.2)
    return False


def main() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    fake_chat = _patched_chat_completion_factory(reply_text="pong-e2e")

    with patch("core.turn_provider_call.chat_completion", fake_chat):
        from main import app  # noqa: PLC0415

        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()

        try:
            if not _wait_health("127.0.0.1", port, timeout=20.0):
                print("FAIL: backend did not become healthy in 20s", file=sys.stderr)
                return 1
            print(f"  OK: backend up on 127.0.0.1:{port}")

            os.environ["CORSIA_EVAL_ENDPOINT"] = f"http://127.0.0.1:{port}/api/eval/turn"
            os.environ["CORSIA_EVAL_TOKEN"] = mint_disposable_token("e2e", label="e2e")
            log_dir = str(Path("/tmp") / "corsia-eval-e2e-logs")
            Path(log_dir).mkdir(parents=True, exist_ok=True)
            for f in Path(log_dir).glob("*.eval"):
                f.unlink(missing_ok=True)

            from eval.runner import run_eval

            logs = run_eval(
                "corsia_ttft_at_burst",
                judge_model=None,
                log_dir=log_dir,
                limit=2,
            )
            assert logs, "run_eval returned no logs"
            print(f"  OK: run_eval returned {len(logs)} log object(s)")

            eval_files = list(Path(log_dir).glob("*.eval"))
            assert eval_files, f"no .eval file written to {log_dir}"
            print(f"  OK: .eval log written: {eval_files[0]}")

            import subprocess
            venv_inspect = Path(sys.executable).parent / "inspect"
            inspect_cmd = str(venv_inspect) if venv_inspect.exists() else "inspect"
            r = subprocess.run(
                [inspect_cmd, "view", "--help"],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode != 0:
                print(f"FAIL: inspect view --help: {r.stderr[:500]}", file=sys.stderr)
                return 1
            print("  OK: 'inspect view --help' runs (renderer wired up)")

            print()
            print("E2E smoke: PASS")
            return 0
        finally:
            server.should_exit = True


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
