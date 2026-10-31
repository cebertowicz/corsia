"""Fenced Codex session observations; only the plan meter controls admission."""
from __future__ import annotations

import logging
from datetime import datetime

from connections import harness_usage
from connections.account_receipts import receipt_is_current
from infra.db_async import run_in_db
from subagents.vendor_write_admission import session_receipt

log = logging.getLogger(__name__)
RATE_LIMITS_UPDATED = 'account/rateLimits/updated'
USAGE_LIMIT_ERROR = 'usageLimitExceeded'


async def _apply(fn, *, session, **kwargs) -> None:
    try:
        await run_in_db(fn, **kwargs, receipt=session_receipt(session))
    except Exception as exc:
        log.error('codex usage observation failed: %s', exc)


def _facts(snapshot: object) -> dict:
    if not isinstance(snapshot, dict):
        return {}
    return {key: snapshot.get(key) for key in ('planType', 'credits', 'individualLimit', 'spendControlReached')}


def _plan_meter(session) -> str:
    from infra.database import get_db

    db = get_db()
    try:
        receipt = session_receipt(session)
        if receipt_is_current(receipt, db=db):
            usage = harness_usage.stored_usage(provider='codex', account_id=receipt.account_id, db=db)
            facts = (usage.get('facts') or {}) if isinstance(usage, dict) else {}
            return facts.get(harness_usage.PLAN_LIMIT_FACT) or harness_usage.CODEX_DEFAULT_LIMIT
        return harness_usage.CODEX_DEFAULT_LIMIT
    finally:
        db.close()


async def handle_rate_limits_notification(session, method: str, params: dict) -> bool:
    if method != RATE_LIMITS_UPDATED:
        return False
    update = params.get('rateLimits') if isinstance(params, dict) else None
    if getattr(session, 'vendor_discovery_staged', False):
        session.vendor_staged_rate_updates.append(update)
        return True
    if not isinstance(update, dict):
        return True
    plan_id = await run_in_db(_plan_meter, session)
    meter = update.get('limitId') or harness_usage.CODEX_DEFAULT_LIMIT
    by_meter = getattr(session, '_rate_limits_by_meter', None) or {}
    plan = meter == plan_id
    previous = by_meter.get(meter)
    if previous is None and plan:
        previous = getattr(session, '_rate_limits', None)
        if isinstance(previous, dict) and (previous.get('limitId') or harness_usage.CODEX_DEFAULT_LIMIT) != meter:
            previous = None
    snapshot = harness_usage.merge_snapshot(previous or {}, update)
    by_meter[meter] = snapshot
    session._rate_limits_by_meter = by_meter
    if plan:
        session._rate_limits = snapshot
    reached_present = 'rateLimitReachedType' in update
    if plan and reached_present and update.get('rateLimitReachedType'):
        verdict = {'state': 'limited', 'reason': str(update['rateLimitReachedType']),
                   'resets_at': harness_usage.reset_from_snapshot(snapshot), 'source': 'rate_limit_update'}
    elif plan and reached_present:
        verdict = {'state': 'allowed', 'source': 'rate_limit_update'}
    else:
        verdict = None
    windows = harness_usage.codex_windows(update, limit_id=None if plan else meter, source='rate_limit_update')
    await _apply(harness_usage.record_usage, session=session, provider='codex',
        windows=windows, facts=_facts(update) if plan else {}, verdict=verdict)
    if plan and not windows:
        session._corsia_usage_control_read_after_turn = True
    return True


def _schedule_sparse_usage_control_read(session) -> None:
    if not getattr(session, '_corsia_usage_control_read_after_turn', False):
        return
    session._corsia_usage_control_read_after_turn = False
    receipt = session_receipt(session)
    if receipt is not None:
        from connections.harness_usage_check import schedule_account_check

        schedule_account_check('codex', receipt.account_id)


async def observe_turn_error(session, error: object) -> None:
    if not isinstance(error, dict) or error.get('codexErrorInfo') != USAGE_LIMIT_ERROR:
        return
    snapshot = getattr(session, '_rate_limits', None)
    await _apply(harness_usage.record_limited, session=session, reason=USAGE_LIMIT_ERROR,
        resets_at=harness_usage.reset_from_snapshot(snapshot), source='turn_error', provider='codex')
    _schedule_sparse_usage_control_read(session)


async def observe_turn_success(session, started: datetime | None) -> None:
    if started is None:
        return
    await _apply(harness_usage.record_allowed, session=session, source='turn_completed',
                 provider='codex', observed_before=started)
    _schedule_sparse_usage_control_read(session)
