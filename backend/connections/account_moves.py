"""The single durable writer for an existing thread account binding move."""
from __future__ import annotations

import json
import uuid

from connections.account_policy import AccountAdmissionError
from connections.vendor_accounts import get_account
from infra.time import utc_now_iso
from infra.write_effects import after_commit, after_commit_merged


MOVE_REASONS = frozenset({'account_limit', 'account_not_permitted', 'account_signed_out', 'account_removed',
                          'account_pinned'})


def movable_thread(thread_id: str, *, db) -> bool:
    """Whether changing this thread's account cannot cross an in-flight write."""
    row = db.execute(
        "SELECT lifecycle_state,switch_operation_id FROM threads WHERE id=?", (thread_id,),
    ).fetchone()
    if row is None or row['lifecycle_state'] != 'active' or row['switch_operation_id'] is not None:
        return False
    if db.execute(
        "SELECT 1 FROM active_turns WHERE thread_id=? AND status IN ('running','waiting_retry')", (thread_id,),
    ).fetchone():
        return False
    return db.execute(
        """SELECT 1 FROM messages WHERE thread_id=? AND input_order IS NOT NULL
           AND delivery_state='queued' AND (
             write_started_at IS NOT NULL OR delivery_attempt_id IS NOT NULL
             OR (write_outcome IS NOT NULL AND write_outcome != 'proved_no_write')
           ) LIMIT 1""",
        (thread_id,),
    ).fetchone() is None


def commit_move(thread_id: str, provider: str, destination_id: str, reason: str, *, db, limit=None,
                message_id: str | None = None, defer_receipt: bool = False) -> dict:
    """Move an existing binding once, including its receipt and wake effects."""
    if reason not in MOVE_REASONS:
        raise ValueError(f'Unknown account move reason: {reason}')
    row = db.execute(
        'SELECT model,runtime_context_epoch,user_id FROM threads WHERE id=?', (thread_id,),
    ).fetchone()
    if row is None:
        raise AccountAdmissionError('account_thread_missing')
    destination = get_account(destination_id, db=db)
    source_row = db.execute(
        'SELECT account_id FROM thread_vendor_accounts WHERE thread_id=? AND provider=?',
        (thread_id, provider),
    ).fetchone()
    if source_row is None:
        raise AccountAdmissionError('account_binding_missing')
    if str(source_row['account_id']) == destination_id:
        _set_pending(thread_id, provider, None, db=db)
        return {'account_id': destination_id, 'runtime_context_epoch': row['runtime_context_epoch'], 'changed': False}
    if not movable_thread(thread_id, db=db):
        raise AccountAdmissionError('account_change_requires_idle')

    source = get_account(str(source_row['account_id']), db=db)
    epoch = int(row['runtime_context_epoch']) + 1
    committed_at = utc_now_iso()
    if message_id is None:
        anchor = (thread_id, source['id'], destination['id'], epoch)
        message_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'corsia-account-pinned:' + repr(anchor)))
    metadata = {
        'type': 'account_switch', 'reason': reason, 'provider': provider, 'model': row['model'],
        'from_account_id': source['id'], 'to_account_id': destination['id'],
        'from_account_name': source['name'], 'to_account_name': destination['name'],
        'resets_at': (limit or {}).get('resets_at'), 'runtime_context_epoch': epoch,
    }
    deferred_move = None
    if defer_receipt:
        deferred_move = json.dumps({
            'reason': reason,
            'from_account_id': source['id'], 'from_account_name': source['name'],
            'to_account_id': destination['id'], 'to_account_name': destination['name'],
            'resets_at': metadata['resets_at'],
        }, separators=(',', ':'))
    db.execute(
        'INSERT INTO thread_vendor_accounts(thread_id,provider,account_id,deferred_move) VALUES (?,?,?,?) '
        'ON CONFLICT(thread_id,provider) DO UPDATE SET account_id=excluded.account_id,pending_account_id=NULL,'
        'deferred_move=excluded.deferred_move',
        (thread_id, provider, destination['id'], deferred_move),
    )
    db.execute('UPDATE threads SET runtime_context_epoch=?,updated_at=? WHERE id=?',
               (epoch, committed_at, thread_id))
    from connections.account_profiles import profile_for
    from connections.account_holds import refresh_thread_holds
    from connections.account_selection import retire_provider_sessions
    from infra.queries import insert_message

    retire_provider_sessions(thread_id, provider, db=db)
    refresh_thread_holds(thread_id, db=db)
    result = metadata | {'message_id': message_id, 'thread_id': thread_id, 'user_id': row['user_id'],
                         'updated_at': committed_at, 'account_id': destination['id'], 'changed': True}
    if deferred_move is None:
        insert_message(thread_id, 'system', f"Moved to {profile_for(provider).label} · {destination['name']}",
                       metadata, msg_id=message_id, created_at=committed_at, db=db)
        after_commit(db, lambda: _publish(result))
    # The moved held head is now eligible; offering it is part of committing the move.
    from infra.connection_limit_drive import schedule_limited_heads
    from infra.unavailable_model_recovery import schedule_unavailable_model_recovery

    after_commit(db, schedule_unavailable_model_recovery)
    after_commit_merged(db, ('limited_heads', provider), [thread_id], schedule_limited_heads)
    return result


def release_deferred_receipt(thread_id: str, provider: str, *, db) -> None:
    """Post the provider row's deferred move receipt once a switch to that vendor commits."""
    row = db.execute(
        'SELECT deferred_move FROM thread_vendor_accounts WHERE thread_id=? AND provider=?',
        (thread_id, provider),
    ).fetchone()
    if row is None or row['deferred_move'] is None:
        return
    deferred = json.loads(str(row['deferred_move']))
    thread = db.execute(
        'SELECT model,runtime_context_epoch,user_id FROM threads WHERE id=?', (thread_id,),
    ).fetchone()
    if thread is None:
        return
    committed_at = utc_now_iso()
    metadata = {
        'type': 'account_switch', 'reason': deferred['reason'], 'provider': provider,
        'model': thread['model'], 'from_account_id': deferred['from_account_id'],
        'to_account_id': deferred['to_account_id'], 'from_account_name': deferred['from_account_name'],
        'to_account_name': deferred['to_account_name'], 'resets_at': deferred['resets_at'],
        'runtime_context_epoch': thread['runtime_context_epoch'],
    }
    anchor = (thread_id, metadata['from_account_id'], metadata['to_account_id'], metadata['runtime_context_epoch'])
    message_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'corsia-account-pinned:' + repr(anchor)))
    db.execute('UPDATE thread_vendor_accounts SET deferred_move=NULL WHERE thread_id=? AND provider=?',
               (thread_id, provider))
    from connections.account_profiles import profile_for
    from infra.queries import insert_message

    insert_message(thread_id, 'system', f"Moved to {profile_for(provider).label} · {metadata['to_account_name']}",
                   metadata, msg_id=message_id, created_at=committed_at, db=db)
    receipt = metadata | {'message_id': message_id, 'thread_id': thread_id, 'user_id': thread['user_id'],
                          'updated_at': committed_at, 'account_id': metadata['to_account_id'], 'changed': True}
    after_commit(db, lambda: _publish(receipt))


def hold_pending_account(thread_id: str, provider: str, account_id: str, *, db) -> None:
    """Keep a person's choice made during a running turn for the next claim to apply.

    Choosing the account already bound clears it. The next turn runs on this
    account (``turn_account`` reads it), the way a model chosen mid-turn is.
    """
    bound = db.execute('SELECT account_id FROM thread_vendor_accounts WHERE thread_id=? AND provider=?',
                       (thread_id, provider)).fetchone()
    if bound is None:
        raise AccountAdmissionError('account_binding_missing')
    _set_pending(thread_id, provider, None if str(bound['account_id']) == account_id else account_id, db=db)


def _set_pending(thread_id: str, provider: str, account_id: str | None, *, db) -> None:
    """The one writer of a pending choice; a move clears it in the same statement as the binding."""
    db.execute('UPDATE thread_vendor_accounts SET pending_account_id=? WHERE thread_id=? AND provider=?',
               (account_id, thread_id, provider))


def apply_pending_accounts(thread_id: str, *, db) -> None:
    """Move to a pending choice at the claim, once no turn is running.

    An account that stopped being permitted since it was chosen is dropped,
    and the binding stays where it is. Only the vendor the thread runs moves:
    another vendor's choice waits for the thread's first claim on that vendor,
    so it never bumps the running vendor's context.
    """
    rows = db.execute('SELECT v.provider,v.pending_account_id,t.model FROM thread_vendor_accounts v '
                      'JOIN threads t ON t.id=v.thread_id '
                      'WHERE v.thread_id=? AND v.pending_account_id IS NOT NULL', (thread_id,)).fetchall()
    if not rows:
        return
    from infra.vendor_turn_binding import model_vendor

    running = model_vendor(str(rows[0]['model'] or ''), db=db)
    rows = [row for row in rows if row['provider'] == running]
    if not rows or not movable_thread(thread_id, db=db):
        return
    from connections.account_policy import admit_account, thread_project_scopes

    for row in rows:
        try:
            admit_account(row['provider'], row['pending_account_id'], thread_project_scopes(thread_id, None, db=db), execution=False, db=db)
        except AccountAdmissionError:
            _set_pending(thread_id, row['provider'], None, db=db)
            continue
        commit_move(thread_id, row['provider'], row['pending_account_id'], 'account_pinned', db=db)


async def _post_effects(receipt) -> None:
    from thread.model_runtime import post_account_switch_effects
    from thread.runtime_boundary import post_context_boundary_effects

    await post_context_boundary_effects(
        receipt['thread_id'], user_id=receipt['user_id'], reason='vendor_account_changed')
    await post_account_switch_effects(receipt)


def _publish(receipt) -> None:
    from infra import app_loop

    try:
        app_loop.submit(_post_effects(receipt))
    except app_loop.AppLoopUnavailable:
        import logging

        logging.getLogger(__name__).warning('account switch publication has no live application loop')
