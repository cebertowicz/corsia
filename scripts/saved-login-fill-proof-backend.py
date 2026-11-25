#!/usr/bin/env python3
"""Run a disposable CORSIA backend whose operator can raise saved-login fill requests.

Fill approvals live in the server's memory, and no route or agent tool raises
one, so this harness runs the backend in its own process and takes commands
on standard input:

    seed-login <https-site> <username>   save a login with a random password (never printed); prints its id
    request <thread-id> <login-id>       raise a fill request, as the fill executor would; prints it
    may-fill <thread-id> <login-id>      print whether a live grant allows the fill
    fill <thread-id> <login-id>          run the fill executor on the thread's open browser; prints its text only

Use it only for a test build on a disposable instance, never on a live one:

    CORSIA_TEST_MODE=1 CORSIA_WORKSPACE=/tmp/fill/ws CORSIA_STATE_DIR=/tmp/fill/state \\
        .venv/bin/python scripts/saved-login-fill-proof-backend.py --root /tmp/fill --port 3290
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import secrets
import sys
import threading

_LIVE_PORTS = {1956, 3000, 3001}


def _refuse_unless(ok: bool, why: str) -> None:
    """Stop before serving; a real check, kept under ``python -O`` where ``assert`` is stripped."""
    if not ok:
        raise SystemExit(f"FILL_PROOF_REFUSED {why}")


def _commands(fills, saved_logins, fill) -> None:
    for line in sys.stdin:
        words = line.split()
        try:
            if len(words) == 3 and words[0] == "seed-login":
                login_id = saved_logins.save_login(
                    site=words[1], username=words[2], password=secrets.token_urlsafe(24), saved_by="fill-proof",
                )
                print(f"LOGIN {login_id}", flush=True)
            elif len(words) == 3 and words[0] == "request":
                print("REQUEST " + json.dumps(fills.request_fill(words[1], words[2]), sort_keys=True), flush=True)
            elif len(words) == 3 and words[0] == "may-fill":
                print(f"MAY_FILL {fills.may_fill(words[1], words[2])}", flush=True)
            elif len(words) == 3 and words[0] == "fill":
                print(fill(words[1], words[2]), flush=True)
            elif words:
                print("ERROR commands: seed-login <site> <username> | request <thread> <login> | may-fill <thread> <login>"
                      " | fill <thread> <login>", flush=True)
        except ValueError as exc:
            print(f"ERROR {exc}", flush=True)
        except TimeoutError:
            print("ERROR the fill didn't finish in time", flush=True)


def main() -> None:
    """Assert the runtime is disposable before any listener can answer, then serve and read commands."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, help="disposable root holding the workspace and state, under /tmp")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    _refuse_unless(root.is_relative_to(Path("/tmp").resolve()), "proof root must be under /tmp")
    _refuse_unless(args.port not in _LIVE_PORTS, "live port refused")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
    from infra import config, database
    paths = {"state_root": str(config.STATE_ROOT.resolve()), "database": str(Path(database.DB_PATH).resolve()),
             "workspace": str(config.WORKSPACE.resolve())}
    _refuse_unless(all(Path(value).is_relative_to(root) for value in paths.values()), "runtime isolation failed")
    print("FILL_PROOF_RUNTIME " + json.dumps(paths), flush=True)

    import uvicorn
    from host_access import browser_login_fill
    from infra import saved_login_fills, saved_logins
    from main import app

    # The fill runs on the server's own loop, where the browser sessions live.
    loop = asyncio.new_event_loop()

    def fill(thread_id: str, login_id: str) -> str:
        coroutine = browser_login_fill.fill_saved_login(thread_id, login_id)
        # No limit of its own: the fill's waits are already bounded (browser_login_fill.SETTLE_LIMIT).
        return asyncio.run_coroutine_threadsafe(coroutine, loop).result()

    threading.Thread(target=_commands, args=(saved_login_fills, saved_logins, fill), daemon=True).start()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port))
    loop.run_until_complete(server.serve())


if __name__ == "__main__":
    main()
