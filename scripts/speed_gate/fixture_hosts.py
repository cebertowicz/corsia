"""Rows the application derives from the machine it runs on, fixed so every host builds the same fixture.

``init_db`` records which CLIs it finds (Claude Code, Codex, GitHub) from PATH and a list of install
directories, and asks Codex whether it is signed in. A Mac with those tools and a hosted runner without them
therefore store different connection rows, and the model-source routes then run different queries. A read
that changes with the machine would put a host difference into the baseline, so the fixture overwrites those
rows with one fixed shape: both harnesses installed and signed in (what a working instance has) and GitHub
needing sign-in. Nothing here is probed or run; the binary names are plain words, never paths.
"""

from __future__ import annotations

import json

from .sqlite_pin import sqlite3

CHECKED_AT = "2026-10-08T11:00:00+00:00"
#: provider -> (status, config). A harness row is ready; ``source_check`` is the key the application reads.
CLI_CONNECTIONS = {
    "claude-code": ("available", {"permission_mode": "bypassPermissions", "binary": "claude",
                                  "source_check": {"state": "ready", "prior_state": None, "reason": None,
                                                   "checked_at": CHECKED_AT}}),
    "codex": ("available", {"binary": "codex",
                            "source_check": {"state": "ready", "prior_state": None, "reason": None,
                                             "checked_at": CHECKED_AT}}),
    "github": ("needs_auth", {"binary": "gh"}),
}


def pin_cli_connections(db: sqlite3.Connection) -> None:
    """Overwrite the built-in CLI connection rows ``init_db`` wrote with the fixed shape above."""
    for provider, (status, config) in CLI_CONNECTIONS.items():
        found = db.execute("UPDATE connections SET status = ?, config = ? WHERE provider = ?",
                           (status, json.dumps(config), provider)).rowcount
        if found != 1:
            raise RuntimeError(f"expected one {provider} connection row after init_db, found {found}")
