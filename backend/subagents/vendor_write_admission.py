"""Revalidate one immutable subscription at a harness's paid write boundary."""
from __future__ import annotations

from pathlib import Path

from connections.account_policy import AccountAdmissionError, chosen_account_id, require_claim, require_launch
from connections.account_receipts import account_receipt
from connections.account_runtime import homes_for, is_operation_session, session_homes
from connections.vendor_accounts import get_account, resolve_account_id
from infra.database import get_db


def _turn_id(session):
    identity = getattr(session, 'corsia_turn_identity', None)
    value = getattr(identity, 'turn_id', None)
    return value if isinstance(value, str) else None


def session_receipt(session):
    """The login a session was admitted on; an unadmitted session names none."""
    bound = getattr(session, 'vendor_account_bound', False) is True
    return getattr(session, 'vendor_account_receipt', None) if bound else None


def prepare_session_launch(session, provider: str) -> None:
    """Freeze before spawn, using a Turn's original login or an unclaimed caller's capture."""
    if getattr(session, 'vendor_account_bound', False) is True:
        require_session_write(session, provider)
        return
    thread_id, turn_id = getattr(session, 'corsia_thread_id', None), _turn_id(session)
    db = get_db()
    try:
        row = db.execute('SELECT model FROM active_turns WHERE id=?', (turn_id,)).fetchone() if turn_id else None
        if row is None and thread_id:
            row = db.execute('SELECT model FROM threads WHERE id=?', (thread_id,)).fetchone()
        model = row['model'] if row else getattr(session, 'catalog_model_id', None)
        identity = (require_claim(provider, thread_id=thread_id, turn_id=turn_id, model=model, db=db)
                    if thread_id else resolve_account_id(provider, db=db))
        account = get_account(identity, db=db) if identity is not None else None
        receipt = account_receipt(account) if account else None
        if turn_id:
            from infra.vendor_login import read_turn_receipt

            receipt = read_turn_receipt(turn_id, db=db)
        if account and account['source_home'] is not None:
            homes = homes_for(account)
            if provider == 'claude-code':
                if session.claude_dir is not None and session.claude_dir != homes.claude_dir:
                    raise AccountAdmissionError('account_home_mismatch')
                session.claude_dir = homes.claude_dir
            else:
                if session.codex_home is not None and session.codex_home != homes.codex_runtime:
                    raise AccountAdmissionError('account_home_mismatch')
                if session.source_codex_home is not None and session.source_codex_home != homes.codex_source:
                    raise AccountAdmissionError('account_home_mismatch')
                session.codex_home, session.source_codex_home = homes.codex_runtime, homes.codex_source
                from connections.account_evidence import discovery_record

                cache = discovery_record(account)['discovery']['cache_receipt']
                session.vendor_cache_receipt = cache
                session.vendor_catalog_source = Path(cache['path']) if cache else None
        session.vendor_account_id, session.vendor_account_receipt = identity, receipt
        session.vendor_account_bound = True
        session.vendor_account_custom = bool(account and account['source_home'])
        session.vendor_catalog_model = model
    finally:
        db.close()
    if provider == 'codex' and session.vendor_account_custom:
        from connections.account_projection import ensure_projection

        ensure_projection(receipt, home=Path(session.codex_home), kind='live', owner_turn_id=turn_id)
    require_session_write(session, provider)


def require_session_write(session, provider: str) -> str | None:
    """Freeze a session's paid account once; never retarget a live continuation."""
    thread_id = getattr(session, "corsia_thread_id", None)
    identity = getattr(session, "corsia_turn_identity", None)
    turn_id = getattr(identity, "turn_id", None) if identity is not None else None
    if not isinstance(turn_id, str):
        turn_id = None
    bound = getattr(session, "vendor_account_bound", False) is True
    account = getattr(session, "vendor_account_id", None) if bound else None
    db = get_db()
    try:
        # The chosen-account re-check guards a live thread continuation from being
        # retargeted mid-thread. An operation session (checkpoint or detached) was
        # already admitted through the operation owner for its explicit account and
        # carries the requester's thread id only for attribution, so it is not a
        # thread continuation and is exempt from this re-check.
        if (bound and thread_id and turn_id is None and not getattr(session, "fork_id", None)
                and not is_operation_session(session)
                and chosen_account_id(thread_id, provider, db=db) != account):
            raise AccountAdmissionError("account_session_mismatch")
        row = get_account(account, db=db) if account is not None else None
        result = require_launch(provider, thread_id=thread_id, account_id=account,
                                turn_id=turn_id, frozen=bound,
                                model=getattr(session, 'vendor_catalog_model', getattr(session, 'catalog_model_id', None)),
                                launch=session_homes(session, provider, row) if bound else None,
                                receipt=getattr(session, 'vendor_account_receipt', None),
                                actor_thread_id=getattr(session, "vendor_actor_thread_id", None), db=db)
    finally:
        db.close()
    session.vendor_account_id = result
    session.vendor_account_bound = True
    return result
