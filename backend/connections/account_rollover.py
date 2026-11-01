"""One opt-in account-limit move before the canonical unwritten FIFO claim."""
from __future__ import annotations

import json
import uuid

from connections.account_policy import account_reason, chosen_account_id, thread_scope
from connections.account_receipts import account_receipt
from connections.harness_usage import stored_usage
from connections.provider_policy import effective_policy
from connections.vendor_accounts import get_account


NOT_PERMITTED = 'account_project_denied'


def _eligible(account, base, project, *, db):
    """A destination runs the model by the one per-account decision, has a login to fence,
    and its vendor has said it is allowed: a move never rests on unknown usage."""
    from llm.account_catalog import account_model_status

    if base is None or account_receipt(account) is None:
        return False
    decision = account_model_status(account, base, project_ids=[project], db=db, move=True)
    return decision['status'] == 'ready' and decision['usage']['state'] == 'allowed'


def rollover_order(provider: str, project: str | None, policy: dict, *, db) -> list[str] | None:
    """The one ordered list rollover moves along, or ``None`` when rollover is off.

    A saved order is used as saved. With none saved (every account permitted),
    the order is every signed-in account, the built-in Default first,
    then the rest by creation.
    """
    if not policy['rollover']:
        return None
    from connections.provider_policy import ordered_accounts

    return [item['id'] for item in ordered_accounts(provider, policy, db=db)
            if item['state'] == 'signed_in']


def rollover_destination(
    provider: str,
    source_id: str | None,
    base: dict | None,
    project: str | None,
    *,
    db,
) -> str | None:
    """Return the ordered opt-in destination when this model's source is limited.

    This is the shared availability decision for the catalog, model binding, and
    an already-held input. An account explicitly named by a caller is not
    passed here: project rollover is for the account a thread would resolve to.
    Candidates are the order read from the top without the source (the order is
    a preference), so an account that has reset is reachable again. A source left
    out of a saved order is denied by the project and moves to the first usable
    account in it.
    """
    from llm.model_status import USAGE_LIMITED

    policy = effective_policy(project, provider, db=db)
    order = rollover_order(provider, project, policy, db=db)
    if base is None or not order or source_id is None:
        return None
    if source_id in order:
        source = get_account(source_id, db=db)
        from llm.account_catalog import account_model_status

        if account_model_status(source, base, project_ids=[project], db=db)['status_reason'] != USAGE_LIMITED:
            return None
        candidates = [identity for identity in order if identity != source_id]
    else:
        candidates = order
    for identity in candidates:
        destination = get_account(identity, db=db)
        if _eligible(destination, base, project, db=db):
            return destination['id']
    return None


def movable_hold(thread_id: str, model: str, reason: str | None, *, db) -> bool:
    """Whether a hold is the project not permitting the thread's account while rollover is on.

    The claim may plan a move after its pause and plan guards; any other hold,
    or rollover off, keeps the input queued as before.
    """
    from infra.vendor_turn_binding import model_vendor

    provider = model_vendor(model, db=db)
    return (reason == NOT_PERMITTED and provider is not None
            and effective_policy(thread_scope(thread_id, db=db)['project_id'], provider, db=db)['rollover'])


def plan_rollover(thread_id: str, *, eligible_rows, db) -> dict | None:
    """Read a replay-safe rollover move plan without changing queue or binding state."""
    from connections.usage_verdicts import effective_verdict
    from infra.vendor_turn_binding import model_vendor

    row = db.execute('SELECT model,execution_epoch,runtime_context_epoch,switch_operation_id,user_id,lifecycle_state FROM threads WHERE id=?',
                     (thread_id,)).fetchone()
    if row is None or row['lifecycle_state'] != 'active' or row['switch_operation_id'] is not None or db.execute(
            "SELECT 1 FROM active_turns WHERE thread_id=? AND status IN ('running','waiting_retry')", (thread_id,)).fetchone():
        return None
    eligible_ids = [str(item['id']) for item in eligible_rows]
    if not eligible_ids or len(set(eligible_ids)) != len(eligible_ids):
        return None
    placeholders = ','.join('?' for _ in eligible_ids)
    valid_rows = db.execute(
        f"SELECT COUNT(*) FROM messages WHERE thread_id=? AND id IN ({placeholders}) "
        "AND input_order IS NOT NULL AND delivery_state='queued' AND active_turn_id IS NULL "
        "AND write_started_at IS NULL AND delivery_attempt_id IS NULL "
        "AND (write_outcome IS NULL OR write_outcome='proved_no_write') "
        "AND target_model=? AND target_execution_epoch=?",
        (thread_id, *eligible_ids, row['model'], row['execution_epoch']),
    ).fetchone()[0]
    if valid_rows != len(eligible_ids):
        return None
    model, provider = row['model'], model_vendor(str(row['model'] or ''), db=db)
    if provider is None:
        return None
    project = thread_scope(thread_id, db=db)['project_id']
    # Opt-in guard first: with rollover off, an idle claim reads no account or catalog row.
    if not effective_policy(project, provider, db=db)['rollover']:
        return None
    source_id = chosen_account_id(thread_id, provider, db=db)
    from llm.model_status import harness_base_row

    destination_id = rollover_destination(provider, source_id, harness_base_row(str(model), db=db), project, db=db)
    if destination_id is None:
        return None
    source = get_account(source_id, db=db)
    receipt = account_receipt(source)
    if receipt is None:
        return None
    reason = account_reason(source, provider=provider, project_ids=[project], execution=True, model=model, db=db)
    if reason == NOT_PERMITTED:
        # Left out of the saved order: no usage limit to anchor on, so the policy revision does.
        limit = {'resets_at': None}
        observation = {'reason': reason, 'policy_revision': effective_policy(project, provider, db=db)['revision']}
        move_reason = 'account_not_permitted'
    elif reason:
        return None
    else:
        usage = stored_usage(provider=provider, account_id=source_id, db=db)
        from connections.harness_usage import _now

        limit = effective_verdict(usage, model, _now(), provider).blocking
        if limit is None or not limit.get('observed_at'):
            return None
        observation = {key: limit.get(key) for key in ('state', 'observed_at', 'source', 'scope_model', 'reason', 'resets_at')}
        move_reason = 'account_limit'
    anchor = json.dumps([thread_id, source_id, receipt.account_revision, receipt.login_seq, observation], sort_keys=True)
    message_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'corsia-account-limit:' + anchor))
    if db.execute('SELECT 1 FROM messages WHERE id=?', (message_id,)).fetchone():
        return None
    return {'thread_id': thread_id, 'provider': provider, 'destination_id': destination_id, 'reason': move_reason,
            'limit': limit, 'message_id': message_id}


def commit_rollover(plan: dict | None, *, db) -> dict | None:
    """Commit a plan after revalidating its FIFO replay anchor."""
    if plan is None:
        return None
    from connections.account_moves import commit_move

    if db.execute('SELECT 1 FROM messages WHERE id=?', (plan['message_id'],)).fetchone():
        return None
    return commit_move(plan['thread_id'], plan['provider'], plan['destination_id'], plan['reason'], limit=plan['limit'],
                       message_id=plan['message_id'], db=db)


def offer_destination(provider: str, account_id: str, *, db) -> None:
    """After an account may have become usable, offer every head it could now take over.

    Each project whose rollover order holds ``account_id`` offers its limit-held heads;
    ``plan_rollover`` alone decides a move.
    """
    from connections.provider_policy import effective_policy
    from infra.connection_limit_release import limited_project_threads, limited_workspace_threads
    from infra.connection_limit_drive import schedule_limited_heads
    from infra.write_effects import after_commit_merged

    projects = [None, *[str(row['id']) for row in db.execute("SELECT id FROM projects").fetchall()]]
    for project_id in projects:
        policy = effective_policy(project_id, provider, db=db)
        order = rollover_order(provider, project_id, policy, db=db)
        if account_id not in (order or []):
            continue
        if project_id is None:
            limited = limited_workspace_threads(provider, db=db)
        else:
            limited = limited_project_threads(project_id, provider, db=db)
        if limited:
            after_commit_merged(db, ('limited_heads', provider), limited, schedule_limited_heads)
