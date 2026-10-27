"""The one stored-state rule for whether a custom account's home can launch a turn."""
from __future__ import annotations

from pathlib import Path


def source_auth_exists(path: Path) -> bool:
    """Check the source credential file without reading it."""
    return path.is_file()


def missing_home_reason(account: dict) -> str | None:
    """Read the source facts shared by readiness and concrete launch admission.

    The Default uses ambient login. Codex's file check does not read credentials.
    """
    source = account['source_home']
    if source is None:
        return None
    if account['provider'] in ('claude-code', 'codex') and not Path(source).is_dir():
        return 'account_home_mismatch'
    if account['provider'] == 'codex' and not source_auth_exists(Path(source) / 'auth.json'):
        return 'codex_auth_source_missing'
    return None
