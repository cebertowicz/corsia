"""``corsia sources`` — one line per model source, from the backend's own checks.

The backend owns detection: ``GET /api/providers/sources`` reads the stored
state and ``POST /api/providers/sources/recheck`` re-runs the checks once; both
return ``{"sources": [{source, name, state, prior_state, reason, fix,
checked_at, limited?, resets_at?}]}``. This module only formats that, so the
terminal and the app never disagree: one sentence per source.

Where a CLI lives, and whether the service can see it, is the backend's
question (``cli_location``); this module prints no fix for it.

A backend that predates the route answers 404; that prints one line and is
not an error. No secret is printed: the shell mints a one-hour service token,
passes it in ``CORSIA_SOURCES_SERVICE_TOKEN``, and it goes only in a header.
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from datetime import datetime

from infra.default_port import DEFAULT_BACKEND_PORT

ROUTE = "/api/providers/sources"
TOKEN_ENV = "CORSIA_SOURCES_SERVICE_TOKEN"
NOT_SUPPORTED = "Model sources: this version can't list them here. See them in the app."
NONE_READY = "No model source is ready yet; the app will show you how to add one."
READ_TIMEOUT_SECONDS = 10
RECHECK_TIMEOUT_SECONDS = 60


class RouteMissing(Exception):
    """The backend has no sources route yet."""


def _base_url() -> str:
    explicit = os.environ.get("CORSIA_API_BASE")
    if explicit:
        return explicit.rstrip("/")
    return f"http://127.0.0.1:{os.environ.get('BACKEND_PORT') or DEFAULT_BACKEND_PORT}"


def _read_token() -> str | None:
    """Return the short-lived service token the shell injected for this call."""
    return os.environ.get(TOKEN_ENV, "").strip() or None


def request_json(method: str, path: str, token: str, timeout: float,
                 body: dict | None = None) -> dict:
    """Make one authenticated JSON request using the sources CLI conventions."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(f"{_base_url()}{path}", data=data, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode() or "null")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise RouteMissing from error
        raise
    if not isinstance(payload, dict):
        raise ValueError("unexpected response shape")
    return payload


def _call(method: str, path: str, token: str, timeout: float) -> list[dict]:
    payload = request_json(method, path, token, timeout)
    sources = payload.get("sources")
    if not isinstance(sources, list):
        raise ValueError("unexpected response shape")
    return [row for row in sources if isinstance(row, dict)]


def _fetch(token: str, recheck: bool) -> tuple[list[dict], str | None]:
    """Recheck once when asked, returning the stored state and failure reason if it fails."""
    if recheck:
        try:
            return _call("POST", f"{ROUTE}/recheck", token, RECHECK_TIMEOUT_SECONDS), None
        except (OSError, ValueError) as error:
            return _call("GET", ROUTE, token, READ_TIMEOUT_SECONDS), str(error)
    return _call("GET", ROUTE, token, READ_TIMEOUT_SECONDS), None


def _reset_time(value: object) -> str:
    """Return ``resets_at`` in local time: '3:40 PM', or 'Thu 3:40 PM' if not today."""
    try:
        moment = datetime.fromisoformat(str(value)).astimezone()
    except ValueError:
        return "later"
    clock = moment.strftime("%I:%M %p").lstrip("0")
    if moment.date() == datetime.now().astimezone().date():
        return clock
    return f"{moment.strftime('%a')} {clock}"


def counts_as_ready(row: dict) -> bool:
    """Ready, or unverified but still dispatching from a prior ready state."""
    return row.get("state") == "ready" or (
        row.get("state") == "unverified" and row.get("prior_state") == "ready"
    )


def _verified_label(row: dict) -> str | None:
    """The first verified source label, if discovery actually read one."""
    for account in row.get("accounts") or []:
        if isinstance(account, dict) and account.get("label_verified"):
            label = account.get("label")
            if isinstance(label, str) and label:
                return label
    return None


def describe(row: dict) -> str:
    """Return the terminal sentence for one source."""
    name = str(row.get("name") or row.get("source") or "?")
    label = _verified_label(row)
    signed_in = f"{name}: signed in as {label}" if label else f"{name}: signed in"
    if row.get("state") == "ready":
        if row.get("limited"):
            return f"{signed_in} · limited · back {_reset_time(row.get('resets_at'))}"
        return signed_in
    if row.get("state") == "unverified":
        return f"{name}: couldn't check. Check again in the app."
    return f"{name}: not signed in. Connect it in the app."


def describe_installed(row: dict) -> str:
    """The install summary's sentence: only what the row knows, and no fix."""
    name = str(row.get("name") or row.get("source") or "?")
    state = row.get("state")
    if row.get("source") == "omlx":
        # The row names no model, so none is named here.
        return f"{name}: ready" if state == "ready" else f"{name}: not set up"
    if row.get("source") not in ("claude-code", "codex"):
        return f"{name}: ready" if state == "ready" else f"{name}: couldn't check"
    if state in ("ready", "unverified"):
        return describe(row)
    if state == "needs_auth":
        return f"{name}: installed, not signed in"
    return f"{name}: not installed"


def _visible_sources(rows: list[dict]) -> list[dict]:
    return [row for row in rows if not row.get("never_local")]


def format_install_summary(rows: list[dict]) -> list[str]:
    """The summary after a completed install, which always has a working source."""
    return ["     Model sources", *(f"       {describe_installed(row)}" for row in _visible_sources(rows))]


def format_sources(rows: list[dict]) -> list[str]:
    """Return the app-directed install summary; it never prints a shell fix."""
    rows = _visible_sources(rows)
    lines = ["     Model sources"]
    for row in rows:
        lines.append(f"       {describe(row)}")
    if not any(counts_as_ready(row) for row in rows):
        lines.append(f"     {NONE_READY}")
    return lines


def cmd_sources(args: argparse.Namespace) -> int:
    """Print the model-source summary; never fails the caller's flow."""
    token = _read_token()
    if not token:
        print(
            "Model sources: couldn't check from here. See them in the app."
        )
        return 0
    try:
        rows, recheck_failure = _fetch(token, recheck=args.action == "recheck")
    except RouteMissing:
        print(NOT_SUPPORTED)
        return 0
    except (OSError, ValueError) as error:
        print(f"Model sources: couldn't check · {error}. See them in the app.")
        return 0
    if recheck_failure:
        print(f"Model sources: recheck failed: {recheck_failure}; showing last stored state.")
    install_summary = getattr(args, "install_summary", False)
    formatter = format_install_summary if install_summary else format_sources
    print("\n".join(formatter(rows)))
    return 0


def build_parser(sub: argparse._SubParsersAction) -> None:
    """Register ``corsia sources [recheck]``."""
    sources = sub.add_parser("sources", help="Show which model sources are ready")
    sources.add_argument("action", nargs="?", choices=["recheck"])
    sources.add_argument("--install-summary", action="store_true", help=argparse.SUPPRESS)
    sources.set_defaults(func=cmd_sources)
