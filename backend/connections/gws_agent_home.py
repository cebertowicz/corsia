"""Where one agent thread's Google Workspace (``gws``) credentials live: no state, no database.

Each thread gets its own home outside the workspace, placed and refused by the same
rule as the agents' GitHub home. Only that thread's shell is granted it, read-write.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from connections.github_agent_home import refusal

logger = logging.getLogger(__name__)

ROOT_ENV = "CORSIA_GWS_AGENT_HOMES_DIR"
# The one name that tells a shell where its own gws home is; the helper fails closed without it.
SHELL_ENV = "CORSIA_GWS_HOME"
_THREAD_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def configured_root() -> Path | None:
    """Where the per-thread gws homes are created, or None when they are off."""
    if ROOT_ENV in os.environ:
        value = os.environ[ROOT_ENV].strip()
        return Path(value).expanduser() if value else None
    if os.environ.get("CORSIA_TEST_MODE") == "1":
        return None
    return Path("~/.config/corsia-gws").expanduser()


def existing_home_for(thread_id: object) -> Path | None:
    """Return one valid gws-home path without creating it.

    Handover needs to inspect or remove a legacy home without accidentally
    creating a new credential location when the old Assistant was never set up.
    """
    root = configured_root()
    if root is None or not isinstance(thread_id, str) or not _THREAD_ID.match(thread_id):
        return None
    home = root / thread_id
    if (reason := refusal(home)) is not None:
        logger.warning("gws home refused for a thread shell: %s", reason)
        return None
    return home


def home_for(thread_id: object) -> Path | None:
    """This thread's gws home, created 0700, or None when it may not be used.

    None means no environment name and no grant, so the helper fails closed. A path inside
    the workspace, or any other path the agents' GitHub home rule refuses, is never used.
    """
    home = existing_home_for(thread_id)
    if home is None:
        return None
    try:
        home.mkdir(mode=0o700, parents=True, exist_ok=True)
        home.chmod(0o700)
    except OSError:
        logger.warning("gws home could not be created for a thread shell", exc_info=True)
        return None
    return home
