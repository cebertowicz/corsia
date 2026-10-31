"""The one rule for whether a custom account's home can launch a turn, read from the filesystem when called."""
from __future__ import annotations

from pathlib import Path


def source_auth_path(source_home: str) -> Path:
    """Where a Codex account's source credential lives; the only place that path is built."""
    return Path(source_home).absolute() / 'auth.json'


def source_auth_exists(path: Path) -> bool:
    """Check the source credential file without reading it."""
    return path.is_file()


def missing_home_reason(account: dict) -> str | None:
    """The source facts shared by readiness and concrete launch admission.

    At most two stats and no vendor or network call. Every account has its own home.
    Codex's file check does not read credentials.
    """
    source = account['source_home']
    if source is None:
        return 'account_home_mismatch'
    if account['provider'] in ('claude-code', 'codex') and not Path(source).is_dir():
        return 'account_home_mismatch'
    if account['provider'] == 'codex' and not source_auth_exists(source_auth_path(source)):
        return 'codex_auth_source_missing'
    return None
