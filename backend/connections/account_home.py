"""The one rule for whether a custom account's home can launch a turn, read from the filesystem when called.

Inside an open ``connections.fact_memo.fact_reads()`` scope the answer for one home is read once:
a discovery commit projects one account's change onto every thread the account can run, and
without the scope each thread re-stats the same two or three homes inside the writer.
"""
from __future__ import annotations

from pathlib import Path

from connections import fact_memo


def source_auth_path(source_home: str) -> Path:
    """Where a Codex account's source credential lives; the only place that path is built."""
    return Path(source_home).absolute() / 'auth.json'


def source_auth_exists(path: Path) -> bool:
    """Check the source credential file without reading it."""
    return path.is_file()


def missing_home_reason(account: dict) -> str | None:
    """The source facts shared by readiness and concrete launch admission.

    At most two stats and no vendor or network call. Every account has its own home.
    Codex's file check does not read credentials. The answer depends only on the
    provider and the home path, which key it inside a ``fact_reads`` scope.
    """
    source = account['source_home']
    if source is None:
        return 'account_home_mismatch'
    provider = account['provider']
    return fact_memo.memoized(('connections.account_home', 'missing_home_reason', provider, source),
                              lambda: _home_reason(provider, source))


def _home_reason(provider: str, source: str) -> str | None:
    if provider in ('claude-code', 'codex') and not Path(source).is_dir():
        return 'account_home_mismatch'
    if provider == 'codex' and not source_auth_exists(source_auth_path(source)):
        return 'codex_auth_source_missing'
    return None
