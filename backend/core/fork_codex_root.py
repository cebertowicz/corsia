"""The registered Codex rollout, found under the account the transcript ran on."""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple


class RegisteredRollout(NamedTuple):
    """A registered rollout plus the frozen login its sessions root came from."""

    provider_id: str
    rollout: Path
    effort: str
    receipt: object | None


def account_sessions_root(thread_id: str, db) -> tuple[Path, object | None] | str:
    """The sessions root the thread's transcript was written under, or why not.

    Taken from the frozen login of the thread's latest Codex Turn, never from
    the thread's current account binding. An unstamped Turn with no account
    keeps the managed default root; an account must prove a current receipt and
    resolves its own runtime home, with no fallback.
    """
    from connections.account_receipts import receipt_is_current
    from connections.account_runtime import launch_homes
    from connections.vendor_accounts import AccountError, get_account
    from infra.vendor_login import thread_login
    from llm.codex_rollout_files import default_codex_sessions_root

    receipt = thread_login(thread_id, "codex", db=db)
    if receipt is None:
        turn = db.execute(
            "SELECT t.vendor_account_id FROM active_turns t "
            "JOIN vendor_accounts a ON a.id=t.vendor_account_id "
            "JOIN connections c ON c.id=a.connection_id "
            "WHERE t.thread_id=? AND c.provider='codex' "
            "ORDER BY t.turn_order DESC LIMIT 1",
            (thread_id,),
        ).fetchone()
        if turn is not None and get_account(
            str(turn["vendor_account_id"]), db=db,
        )["source_home"] is not None:
            return "account_provenance_ambiguous"
        return default_codex_sessions_root(), None
    try:
        homes = launch_homes(receipt.account_id, db=db)
    except (AccountError, ValueError):
        return "account_provenance_ambiguous"
    if homes.codex_runtime is None or not receipt_is_current(receipt, db=db):
        return "account_provenance_ambiguous"
    return Path(homes.codex_runtime) / "sessions", receipt


def registered_rollout(thread_id: str) -> RegisteredRollout | str | None:
    """The registered rollout under the thread's actual account root.

    ``None`` means unregistered; a string is the reason the account root could
    not be resolved without guessing.
    """
    from infra.database import get_db
    from llm.codex_rollout_files import (
        find_codex_rollout,
        registered_rollout_candidate,
    )
    from subagents.codex_runtime_support import codex_thread_registration

    db = get_db()
    try:
        provider_id, recorded_path, effort = codex_thread_registration(thread_id, db)
        if provider_id is None:
            return None
        resolved = account_sessions_root(thread_id, db)
    finally:
        db.close()
    if isinstance(resolved, str):
        return resolved
    sessions_root, receipt = resolved
    rollout = registered_rollout_candidate(recorded_path, sessions_root=sessions_root)
    if rollout is None:
        rollout = find_codex_rollout(provider_id, sessions_root=sessions_root)
    if rollout is None:
        return None
    return RegisteredRollout(provider_id, rollout, effort or "", receipt)
