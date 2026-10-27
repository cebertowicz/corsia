"""What a managed agent-browser session launches with: the saved sign-in profile and headless stealth.

The profile directory holds the cookie database, so it is a secret: it lives under CORSIA private state,
and ``tools.host_path_policy`` closes that root to every agent route, leases included.
"""
from __future__ import annotations

import logging
import os
import platform
import re
import sqlite3
import subprocess
from pathlib import Path

PROFILE_ROOT_NAME = "browser-profile"
# Copies of the saved sign-in for sessions that cannot share the profile; they live inside the profile root.
COPIES_DIR_NAME = "copies"
STEALTH_INIT_SCRIPT = Path(__file__).with_name("browser_stealth_init.js")
_OWNER_KEY = re.compile(r"[A-Za-z0-9_-]{1,128}")
_CHROME_VERSION = re.compile(r"(\d+)\.\d+\.\d+\.\d+")
_PLATFORM_TOKENS = {
    "Darwin": "Macintosh; Intel Mac OS X 10_15_7",
    "Windows": "Windows NT 10.0; Win64; x64",
}


def profile_root() -> Path:
    """The folder of saved sign-in profiles, under the instance's state root."""
    from infra.config import STATE_ROOT
    return STATE_ROOT / PROFILE_ROOT_NAME


def profile_dir(thread_id: str) -> Path | None:
    """Resolve the instance's saved profile only for a thread with a valid owner."""
    # Contributor workspaces run in separate instances with separate state roots; see
    # docs/contributing/bootstrap/CROSS-WORKSPACE-PROTOCOL.md:19,92,120-126.
    # Unknown owners fail closed to an ephemeral session.
    owner = _owner_key(thread_id)
    return profile_root() / owner if owner is not None else None


def copy_path(session: str) -> Path:
    """Where a concurrent session's copy of the saved sign-in is kept; removed when it closes."""
    return profile_root() / COPIES_DIR_NAME / f"{session}.json"


def _owner_key(thread_id: str) -> str | None:
    from infra.database import get_db
    try:
        db = get_db()
        try:
            row = db.execute("SELECT user_id FROM threads WHERE id = ?", (thread_id,)).fetchone()
        finally:
            db.close()
    except sqlite3.Error:
        logging.getLogger(__name__).warning("browser sign-in owner lookup failed", exc_info=True)
        return None
    owner = row[0] if row is not None else None
    return owner if isinstance(owner, str) and _OWNER_KEY.fullmatch(owner) else None


def chrome_major(chrome: str | None) -> str | None:
    """The major version of the Chrome that will launch, or None when it cannot be told."""
    if chrome:
        try:
            out = subprocess.run([chrome, "--version"], capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        if match := _CHROME_VERSION.search(out):
            return match[1]
        return None
    # agent-browser's own download is a folder named chrome-<version>.
    for folder in sorted((Path.home() / ".agent-browser" / "browsers").glob("chrome-*"), reverse=True):
        if match := _CHROME_VERSION.search(folder.name):
            return match[1]
    return None


def user_agent(major: str | None) -> str | None:
    """Chrome's own user agent for this version, without the ``HeadlessChrome`` token."""
    if major is None:
        return None
    token = _PLATFORM_TOKENS.get(platform.system(), "X11; Linux x86_64")
    return f"Mozilla/5.0 ({token}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"


def inherited_args() -> list[str]:
    """Preserve launch settings without overriding owned listeners, profiles or origins."""
    from host_access.browser_identity import BrowserIdentityError
    # Match Chrome argv's splitlines() boundaries before checking each switch.
    inherited = [part.strip() for line in os.environ.get("AGENT_BROWSER_ARGS", "").splitlines()
                 for part in line.split(",") if part.strip()]
    for arg in inherited:
        switch = re.split(r"[=\s]", arg.lstrip("-"), maxsplit=1)[0].lower()
        if switch in {"explicitly-allowed-ports", "remote-allow-origins", "remote-debugging-port",
                      "remote-debugging-address", "user-data-dir"}:
            raise BrowserIdentityError(f"managed browser refuses --{switch}; remove it from AGENT_BROWSER_ARGS")
    return inherited


def stealth_args(ua: str | None, *, headless: bool) -> str:
    """The ``--args`` value: what the environment already set, plus the evasions.

    agent-browser splits this value on commas or newlines, and a user agent holds a comma, so newlines it is.
    """
    inherited = inherited_args()
    ours = ["--disable-blink-features=AutomationControlled"]
    if headless and ua:
        ours.append(f"--user-agent={ua}")
    return "\n".join([*inherited, *(arg for arg in ours if arg not in inherited)])
