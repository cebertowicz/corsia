"""Connector records and conditional tool registration."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Sequence
from typing import Any
from pathlib import Path

from connections.account_readiness import AccountReadiness

log = logging.getLogger(__name__)


def read_account_readiness(thread_ids: Sequence[str], *, db: sqlite3.Connection) -> dict[str, AccountReadiness]:
    """Read stored account eligibility for authorized thread batches without effects."""
    from connections.account_policy import read_account_readiness as read

    return read(thread_ids, db=db)


def register_if_available(
    registry, provider: str, tool_factory: Callable[[], Any], *, db=None
) -> bool:
    """Register ``tool_factory()`` iff the connector is available."""
    from connections.models import connection_available

    if connection_available(provider, db=db):
        registry.register(tool_factory())
        return True
    return False


def _gh_status(binary: str) -> tuple[bool | None, str | None]:
    """The person's own ``gh`` sign-in and login; (None, None) if the probe itself failed.

    Run in the backend's own environment (``connections.github_signin``). Raises
    ``GhUpdateRequired`` when this ``gh`` is too old to report its sign-in.
    """
    from connections.github_signin import gh_status

    return gh_status(binary)



_BINARY_CANDIDATES = {
    "claude-code": ("claude", ["~/.local/bin/claude", "/opt/homebrew/bin/claude",
                               "/usr/local/bin/claude"]),
    "github": ("gh", ["/opt/homebrew/bin/gh", "/usr/local/bin/gh", "~/.local/bin/gh"]),
    "codex": ("codex", ["/opt/homebrew/bin/codex", "/usr/local/bin/codex",
                        "~/.local/bin/codex"]),
}


def resolve_builtin_binary(provider: str, *, db=None) -> tuple[dict, str | None]:
    """A built-in CLI's stored config and its executable, or None if missing.

    A configured binary wins; otherwise PATH, then the known install locations.
    """
    from connections.models import get_connection

    cfg = (get_connection(provider, db=db) or {}).get("config") or {}
    return cfg, binary_from_config(provider, cfg)


def binary_from_config(provider: str, cfg: dict, *, candidate_roots: tuple[Path, ...] | None = None) -> str | None:
    """Resolve a CLI; tests may supply search roots without changing this machine's installs.

    The injected roots are a Python-only seam, never a persisted setting or environment override.
    """
    import os
    import shutil

    name, candidates = _BINARY_CANDIDATES[provider]
    manual = cfg.get("binary")
    if manual and os.path.isfile(manual) and os.access(manual, os.X_OK):
        return manual
    if candidate_roots is None:
        found = shutil.which(name)
    else:
        found = shutil.which(name, path=os.pathsep.join(str(root) for root in candidate_roots))
        candidates = [str(root / name) for root in candidate_roots]
    if found:
        return found
    for c in candidates:
        p = os.path.expanduser(c)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None  # None -> not_installed


def agent_binary_from_config(cfg: dict) -> str | None:
    """Resolve the agents' GitHub executable, including their prior verified CORSIA install."""
    if binary := binary_from_config("github", cfg):
        return binary
    from tools.managed_runtime import installed_command_path

    return installed_command_path("gh")


def seed_builtin_connections(*, db=None) -> int:
    """Detect installed CLI connectors on PATH and upsert their records.

    Seed only whether each vendor's CLI is installed, never either vendor's
    ambient login: sign-in state lives on its accounts (``source_state.harness_state``).
    """
    from connections.models import SOURCE_CHECK_KEY, ConnectionStatus, ConnectionType, upsert_connection

    def seed(provider: str, binary_name: str, extra: dict) -> None:
        cfg, binary = resolve_builtin_binary(provider, db=db)
        cfg = {k: v for k, v in cfg.items() if k != SOURCE_CHECK_KEY}
        upsert_connection(
            type=ConnectionType.CLI, provider=provider,
            config={**extra, **cfg, "binary": binary or binary_name},
            status=ConnectionStatus.AVAILABLE if binary else ConnectionStatus.NOT_INSTALLED,
            db=db,
        )

    seed("claude-code", "claude", {"permission_mode": "bypassPermissions"})
    refresh_github_connection(db=db)
    seed("codex", "codex", {})

    log.info("Seeded built-in connections: claude-code, github, codex")
    return 3


def refresh_github_connection(*, db=None, keep_row_on_failed_probe: bool = False) -> bool:
    """Probe the resolved ``gh`` and store GitHub's row; False if the probe failed.

    Startup stores a failed probe as needs_auth. The model-source recheck keeps
    the last stored row instead and reports itself incomplete.
    """
    from connections.github_signin import UPDATE_GH_REASON, GhUpdateRequired
    from connections.models import (
        SOURCE_CHECK_KEY,
        ConnectionStatus,
        ConnectionType,
        get_connection,
        upsert_connection,
    )
    from connections.source_state import UNVERIFIED, resolve_check

    gh_cfg, gh_bin = resolve_builtin_binary("github", db=db)
    gh_cfg = {k: v for k, v in gh_cfg.items() if k != SOURCE_CHECK_KEY}
    update_needed = False
    try:
        authed, login = _gh_status(gh_bin) if gh_bin else (False, None)
    except GhUpdateRequired:
        authed, login, update_needed = None, None, True
    if authed is None and not update_needed and keep_row_on_failed_probe:
        return False
    if update_needed:
        # This gh cannot say whether the person is signed in: ask for the update,
        # never "not signed in".
        gh_status, gh_cfg[SOURCE_CHECK_KEY] = resolve_check(
            get_connection("github", db=db), UNVERIFIED, UPDATE_GH_REASON,
        )
    elif not gh_bin:
        gh_status = ConnectionStatus.NOT_INSTALLED
    elif authed:
        gh_status = ConnectionStatus.AVAILABLE
    else:
        gh_status = ConnectionStatus.NEEDS_AUTH
    upsert_connection(
        type=ConnectionType.CLI,
        provider="github",
        # Only a definite login replaces the stored one: the agents' account compares against it.
        config={**gh_cfg, "binary": gh_bin or "gh", **({"login": login} if authed and login else {})},
        status=gh_status,
        db=db,
    )
    return authed is not None or update_needed
