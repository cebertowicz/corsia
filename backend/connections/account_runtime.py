"""The actual frozen account's homes and login at every concrete paid boundary."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

from connections.account_home import missing_home_reason
from connections.account_policy import AccountAdmissionError, require_inference
from connections.account_receipts import ReceiptFence, account_receipt, receipt_is_current, unobserved_default
from connections.vendor_accounts import get_account


@dataclass(frozen=True, slots=True)
class LaunchHomes:
    account_id: str | None
    custom: bool
    claude_dir: str | None = None
    codex_source: str | None = None
    codex_runtime: str | None = None
    isolated: bool = False
    warm_home: str | None = None


DEFAULT_HOMES = LaunchHomes(None, False)


def homes_for(account: dict) -> LaunchHomes:
    # A non-inference account never gets a launch home, whatever its stored homes say.
    require_inference(account['provider'])
    if account['source_home'] is None:
        return LaunchHomes(str(account['id']), False)
    if account['provider'] == 'claude-code':
        # Claude uses its explicit config/source root; runtime_home is unused.
        return LaunchHomes(str(account['id']), True, claude_dir=str(account['source_home']))
    if account['runtime_home'] is None or account['runtime_home'] == account['source_home']:
        raise AccountAdmissionError('account_home_mismatch')
    return LaunchHomes(str(account['id']), True, codex_source=str(account['source_home']),
                       codex_runtime=str(account['runtime_home']))


def launch_homes(account_id: str | None, *, db=None) -> LaunchHomes:
    return homes_for(get_account(account_id, db=db)) if account_id else DEFAULT_HOMES


def thread_launch_homes(thread_id: str, provider: str, *, turn_id: str | None = None, db) -> LaunchHomes:
    """Select paths from a frozen Turn when present, otherwise the unclaimed binding."""
    from connections.account_policy import chosen_account_id

    if turn_id is not None:
        row = db.execute('SELECT vendor_account_id FROM active_turns WHERE id=? AND thread_id=?',
                         (turn_id, thread_id)).fetchone()
        if row is None:
            raise AccountAdmissionError('account_turn_missing')
        identity = row['vendor_account_id']
    else:
        identity = chosen_account_id(thread_id, provider, db=db)
    return launch_homes(identity, db=db)


def thread_claude_dir(thread_id: str, *, turn_id: str | None = None, db) -> str:
    """The one Claude root a thread's sessions are written to, resumed from, and launched in.

    An added account's own config home, else the Default root. Every transcript
    writer, lookup, and launcher for a thread takes its root from here, so none
    can fall back to the Default for a thread bound to another account.
    """
    from subagents.cc_sessions import resolve_claude_dir

    return resolve_claude_dir(thread_launch_homes(thread_id, 'claude-code', turn_id=turn_id, db=db).claude_dir)


OPERATION_KINDS = ('checkpoint', 'artifact_review', 'detached')


def is_operation_session(session) -> bool:
    """Whether the operation owner bound this session for its own explicit account."""
    return getattr(session, 'vendor_operation_kind', None) in OPERATION_KINDS


def session_homes(session, provider: str, account: dict | None) -> LaunchHomes:
    """Describe the process's actual paths, never fill missing paths from current choice."""
    return LaunchHomes(account['id'] if account else None, bool(account and account['source_home']),
                       claude_dir=getattr(session, 'claude_dir', None) if provider == 'claude-code' else None,
                       codex_source=getattr(session, 'source_codex_home', None) if provider == 'codex' else None,
                       codex_runtime=getattr(session, 'codex_home', None) if provider == 'codex' else None,
                       isolated=is_operation_session(session),
                       warm_home=getattr(session, 'vendor_warm_home', None))


def _same_path(left, right):
    return left is not None and right is not None and Path(left).absolute() == Path(right).absolute()


def validate_launch(account: dict | None, *, launch: LaunchHomes, receipt: ReceiptFence | None,
                    model: str | None, db) -> None:
    if launch.account_id != (account['id'] if account else None):
        raise AccountAdmissionError('account_home_mismatch')
    if account is None or account['source_home'] is None:
        if launch.custom or (launch.codex_source is not None and not (
                launch.isolated and _same_path(launch.codex_source, launch.warm_home))):
            raise AccountAdmissionError('account_home_mismatch')
        if launch.claude_dir is not None:
            from subagents.cc_sessions import resolve_claude_dir

            if not _same_path(launch.claude_dir, resolve_claude_dir()):
                raise AccountAdmissionError('account_home_mismatch')
        if launch.codex_runtime is not None and not launch.isolated:
            from subagents.codex_env import _default_app_server_home

            if not _same_path(launch.codex_runtime, _default_app_server_home()):
                raise AccountAdmissionError('account_home_mismatch')
        # A launch with no receipt (an unstamped Turn, a session bound at its first write) on a
        # Default no one has observed yet cannot belong to another login: it is on login 0.
        if (account and account_receipt(account) is not None and not receipt_is_current(receipt, db=db)
                and not (receipt is None and unobserved_default(account))):
            raise AccountAdmissionError('account_login_unverified')
        return
    if not launch.custom or model is None:
        raise AccountAdmissionError('account_home_mismatch' if not launch.custom else 'account_model_not_discovered')
    if not receipt_is_current(receipt, db=db) or cast(ReceiptFence, receipt).account_id != account['id']:
        raise AccountAdmissionError('account_login_unverified')
    if account['provider'] == 'claude-code':
        if not _same_path(launch.claude_dir, account['source_home']) or launch.codex_source or launch.codex_runtime:
            raise AccountAdmissionError('account_home_mismatch')
        if reason := missing_home_reason(account):
            raise AccountAdmissionError(reason)
        return
    if not _same_path(launch.codex_source, account['source_home']) or launch.codex_runtime is None:
        raise AccountAdmissionError('account_home_mismatch')
    from connections.account_projection import validate_projection
    from subagents.codex_env import _subscription_auth_file

    # The home rule decides first, as readiness does; the projection's own source check
    # remains only as its write-time guard against a file removed after this point.
    if reason := missing_home_reason(account):
        raise AccountAdmissionError(reason)
    validate_projection(receipt, home=Path(launch.codex_runtime), db=db)
    try:
        _subscription_auth_file(Path(cast(str, launch.codex_source)))
    except (OSError, RuntimeError):
        raise AccountAdmissionError('account_login_unverified') from None
