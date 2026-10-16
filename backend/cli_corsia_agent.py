"""``corsia agent warm`` — pre-warm the CORSIA agent's prefix at install.

When the local starter model is ready, the first reply from the
CORSIA agent is faster if the server has already prefilled its system and tool
prefix. The server owns that work (``POST /api/threads/{id}/warm`` renders
through the send path and runs the warm); this command asks for it once, holds
that one request open until the warm ends, and shows the person that something
is happening.

It never fails the install: every outcome exits 0, and the starter simply not
being ready prints nothing at all. The one-hour service token comes in
``CORSIA_SOURCES_SERVICE_TOKEN`` from the shell, as for ``corsia sources``.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import urllib.parse
from collections.abc import Callable

from cli_sources import READ_TIMEOUT_SECONDS, RouteMissing, _read_token, request_json
from llm.local_profiles import DEFAULT_PROFILE

WARM_TIMEOUT_SECONDS = 300
# The request outlives the deadline a little so the server's answer, not our
# socket, decides a warm that finishes right at the limit.
WARM_REQUEST_TIMEOUT_SECONDS = WARM_TIMEOUT_SECONDS + 15

PROGRESS = "Getting CORSIA ready (its first reply will be faster)"
DONE = "CORSIA ready."
FAILED = (
    "Couldn't get CORSIA ready ahead of time; "
    "its first reply will take a little longer."
)
TIMED_OUT = "Still getting ready in the background; you can start now."

# What a request can raise: no server, a refusal, a 404, or an unreadable body.
_REQUEST_ERRORS = (OSError, ValueError, RouteMissing)

REQUEST_THREAD = "corsia-agent-warm-request"
_CLEAR_LINE = "\r\033[K"


def _step_prefix() -> str:
    """The installer's step number, only when the installer is running this."""
    return "8/8  " if os.environ.get("CORSIA_INSTALL_FLOW") == "1" else ""


def _starter_catalog_id(token: str) -> str | None:
    """The starter's catalog id when its row reads ``ready``, else None."""
    payload = request_json("GET", "/api/models", token, READ_TIMEOUT_SECONDS)
    suffix = f"/{DEFAULT_PROFILE.model}"
    for row in payload.get("models") or []:
        if (
            isinstance(row, dict)
            and row.get("status") == "ready"
            and row.get("backend") == "network"
            and row.get("provider") == "omlx"
            and str(row.get("litellm_model") or "").endswith(suffix)
        ):
            return str(row["id"])
    return None


def _agent_id(token: str) -> str | None:
    query = urllib.parse.urlencode({"token": token})
    payload = request_json("GET", f"/api/corsia-agent?{query}", token, READ_TIMEOUT_SECONDS)
    thread_id = payload.get("thread_id")
    return thread_id if isinstance(thread_id, str) and thread_id else None


def _warm(thread_id: str, catalog_id: str, token: str) -> dict:
    """One request that returns when the server's warm has actually ended."""
    query = urllib.parse.urlencode({"token": token, "model": catalog_id, "wait": "true"})
    return request_json(
        "POST", f"/api/threads/{thread_id}/warm?{query}", token,
        WARM_REQUEST_TIMEOUT_SECONDS,
    )


def _elapsed(seconds: float) -> str:
    whole = int(seconds)
    return f"{whole // 60}:{whole % 60:02d}"


def _discover(token: str) -> tuple[str, str] | None:
    """The starter's catalog id and the CORSIA agent's thread id, or None.

    The starter not being ready is the ordinary "nothing to warm" and stays
    silent. A request that fails means a route this command consumes is broken
    or missing, so it names that route on stderr; the install goes on either way.
    """
    try:
        catalog_id = _starter_catalog_id(token)
    except _REQUEST_ERRORS as exc:
        print(f"agent warm: GET /api/models failed ({type(exc).__name__})", file=sys.stderr)
        return None
    if catalog_id is None:
        return None
    try:
        thread_id = _agent_id(token)
    except _REQUEST_ERRORS as exc:
        print(f"agent warm: GET /api/corsia-agent failed ({type(exc).__name__})", file=sys.stderr)
        return None
    if thread_id is None:
        return None
    return catalog_id, thread_id


def warm_corsia_agent(
    *,
    out=None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] | None = None,
    call: Callable[[str, str, str], dict] = _warm,
) -> int:
    """Run one warm to its end; always returns 0 so install continues."""
    out = out or sys.stdout
    token = _read_token()
    if token is None:
        return 0
    found = _discover(token)
    if found is None:
        return 0
    catalog_id, thread_id = found

    live = out.isatty()
    prefix = _step_prefix()
    started = clock()

    def _show() -> None:
        line = f"{prefix}{PROGRESS} · {_elapsed(clock() - started)}"
        out.write(f"{_CLEAR_LINE}{line}" if live else "")
        out.flush()

    def _finish(message: str) -> int:
        out.write(f"{_CLEAR_LINE}{prefix}{message}\n" if live else f"{prefix}{message}\n")
        out.flush()
        return 0

    # One request, run aside so the line keeps ticking while it is held open.
    finished = threading.Event()
    outcome: dict[str, dict | None] = {}

    def _request() -> None:
        try:
            outcome["result"] = call(thread_id, catalog_id, token)
        except _REQUEST_ERRORS:
            outcome["result"] = None
        finally:
            finished.set()

    threading.Thread(target=_request, name=REQUEST_THREAD, daemon=True).start()
    tick = sleep or (lambda seconds: finished.wait(seconds))
    if not live:
        out.write(f"{prefix}{PROGRESS}\n")
    while not finished.is_set() and clock() - started < WARM_TIMEOUT_SECONDS:
        _show()
        tick(1)
    if not finished.is_set():
        return _finish(TIMED_OUT)
    result = outcome.get("result")
    if not isinstance(result, dict) or not result.get("ok"):
        return _finish(FAILED)
    return _finish(DONE)


def cmd_warm(_args: argparse.Namespace) -> int:
    return warm_corsia_agent()


def build_parser(subparsers) -> None:
    """Register ``agent warm``."""
    agent = subparsers.add_parser("agent", help="CORSIA agent helpers")
    commands = agent.add_subparsers(dest="agent_cmd", required=True)
    warm = commands.add_parser(
        "warm", help="Pre-warm the CORSIA agent's first reply on the local starter model",
    )
    warm.set_defaults(func=cmd_warm)
