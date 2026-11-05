#!/usr/bin/env python3
"""Shared REST-client helpers for the `corsia` shell CLIs."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# The standalone CLIs share the token location with the backend's shell floor.
_BACKEND = str(Path(__file__).resolve().parents[1])
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from infra.api_token_file import api_token_file, move_legacy_token  # noqa: E402

DEFAULT_PORT = 1956  # held equal to infra.default_port by tests/test_default_port.py
DEFAULT_BASE = f"http://127.0.0.1:{DEFAULT_PORT}"

# An override is the person's own choice of file and lies outside the shell floor.
_OVERRIDE = os.environ.get("CORSIA_API_TOKEN_FILE")
TOKEN_FILE = Path(_OVERRIDE or api_token_file(Path.home()))


def settle_token_file() -> None:
    """Move a token left at an earlier location into the token folder."""
    move_legacy_token(Path.home(), Path(_OVERRIDE) if _OVERRIDE else None)


def resolve_token(explicit: str | None) -> str:
    """Resolve the API token or fail loud with guidance."""
    if explicit and explicit.strip():
        return explicit.strip()
    env = os.environ.get("CORSIA_API_TOKEN")
    if env and env.strip():
        return env.strip()
    # Read the file directly rather than asking whether it exists: a closed
    # folder must say so, and Path.exists either raises or answers False there
    # depending on the Python version.
    try:
        settle_token_file()
        mode = TOKEN_FILE.stat().st_mode
        tok = TOKEN_FILE.read_text().strip()
    except (FileNotFoundError, NotADirectoryError):
        tok = ""
    except PermissionError as exc:
        sys.stderr.write(
            f"Can't read the API token: permission denied ({exc.filename or TOKEN_FILE}).\n"
            "  Agent shells can't reach the token folder, even under a host-access lease.\n"
            "  Agents reach CORSIA through its own tools instead.\n"
        )
        sys.exit(2)
    else:
        import stat
        if mode & (stat.S_IRGRP | stat.S_IWGRP | stat.S_IROTH | stat.S_IWOTH):
            sys.stderr.write(
                f"WARNING: {TOKEN_FILE} is group/world-readable "
                f"(mode {oct(mode & 0o777)}). Run: chmod 600 {TOKEN_FILE}\n"
            )
    if tok:
        return tok
    sys.stderr.write(
        f"No API token found.\n"
        f"  The credential file {TOKEN_FILE} is missing or empty.\n"
        f"  Provision it (any one):\n"
        f"    - mint one into the file (recommended; one time):\n"
        f"        corsia token create --write-delegate-file\n"
        f"    - or set it in the env:   export CORSIA_API_TOKEN=<tok>\n"
        f"    - or pass it explicitly:  --token <tok>\n"
        f"  <tok> is an API token from 'corsia token create'.\n"
    )
    sys.exit(2)


def resolve_base(explicit: str | None) -> str:
    """Resolve the API base: explicit, then $CORSIA_API_BASE, then local."""
    return (explicit or os.environ.get("CORSIA_API_BASE") or DEFAULT_BASE).rstrip("/")


def request(method: str, base: str, path: str, token: str,
            body: dict | None = None, query: dict | None = None,
            *, header_auth: bool = False) -> dict:
    """Call the REST API and exit with a readable error on failure.

    ``header_auth`` sends the credential only as a ``Bearer`` header, never in
    the URL, where it would reach server logs and shell history.
    """
    params = dict(query or {})
    if not header_auth:
        params["token"] = token
    url = f"{base}{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if header_auth:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode()
        try:
            detail = json.loads(detail).get("detail", detail)
        except (json.JSONDecodeError, AttributeError):
            pass
        sys.stderr.write(f"HTTP {e.code}: {detail}\n")
        sys.exit(1)
    except urllib.error.URLError as e:
        sys.stderr.write(f"Connection failed ({base}): {e.reason}\n")
        sys.exit(1)
