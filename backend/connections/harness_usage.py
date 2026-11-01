"""Own each account's canonical usage; usage_verdicts decides model availability."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any, cast

from connections.limit_history import append_changed
from connections.vendor_accounts import resolve_account_id
from connections.account_receipts import ReceiptFence, capture_receipt, write_account
from connections.usage_verdicts import (
    ACCOUNT_SCOPE,
    ConnectionAvailability,
    LimitSubject,
    availability_from_projection,
    effective_verdict,
    next_marker,
    release_marker,

    scope_key,
    verdicts,
)
from connections.overage_policy import ALLOW_OVERAGE_KEY, derive_plan_limits, plan_limited_scopes
from connections.usage_projection import account_usage as _account_usage
from connections.usage_projection import project_for_model as _project_for_model
from connections.usage_windows import (
    merged_window, newer, parse_time, window_key, without_reading,
)
from connections.usage_store import USAGE_VERSION, current, empty_usage as _empty_usage
from infra.database import get_db, write_transaction
from infra.write_effects import after_commit

log = logging.getLogger(__name__)

#: The fact naming the meter a full vendor read reported as the plan's own.
PLAN_LIMIT_FACT = "limitId"
from connections.usage_snapshots import (
    CODEX_DEFAULT_LIMIT as CODEX_DEFAULT_LIMIT,
    codex_windows as codex_windows,
    merge_snapshot as merge_snapshot,
    reset_from_snapshot as reset_from_snapshot,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)



def _load(db, provider: str, account_id: str) -> tuple[str, dict[str, Any] | None]:
    identity = cast(str, resolve_account_id(provider, account_id, db=db))
    row = db.execute("SELECT usage_json FROM vendor_accounts WHERE id=?", (identity,)).fetchone()
    usage = json.loads(row["usage_json"]) if row["usage_json"] else None
    return identity, current(usage) if isinstance(usage, dict) else None


def _store(db, usage: dict[str, Any], account_id: str) -> None:
    db.execute(
        "UPDATE vendor_accounts SET usage_json=?, updated_at=? WHERE id=?",
        (json.dumps(usage, sort_keys=True), _now().isoformat(), account_id),
    )


def clear_account_usage(provider: str, account_id: str, *, db=None) -> None:
    """Forget the old login's usage in the identity-change transaction."""
    with write_transaction(db, scope="clear_account_usage") as connection:
        identity = resolve_account_id(provider, account_id, db=connection)
        connection.execute("UPDATE vendor_accounts SET usage_json=NULL WHERE id=?", (identity,))


def _merge_usage(
    usage: dict[str, Any], *, windows: Sequence[dict[str, Any]] = (), facts: dict[str, Any] | None = None,
    verdict: dict[str, Any] | None = None, check_error: str | None = None,
) -> dict[str, Any]:
    merged = dict(usage)
    merged["version"] = USAGE_VERSION
    # One frame is one instant: its verdict is never newer than its own readings.
    stamp = _now().isoformat()
    prior_facts = merged.get("facts")
    fact_map = dict(prior_facts) if isinstance(prior_facts, dict) else {}
    if facts:
        fact_map.update({key: value for key, value in facts.items() if value is not None})
    merged["facts"] = fact_map
    # One meter is one window: a frame naming the plan's own meter is the plan
    # window, whichever source sent it, and never also a by-limit bucket.
    plan_limit = fact_map.get(PLAN_LIMIT_FACT, CODEX_DEFAULT_LIMIT)
    prior_windows = merged.get("windows")
    window_map = {
        key: window for key, window in (prior_windows.items() if isinstance(prior_windows, dict) else ())
        if not (isinstance(window, dict) and window.get("limit_id") == plan_limit)
    }
    for window in windows:
        if isinstance(window, dict) and window.get("limit_id") == plan_limit:
            window = {**window, "limit_id": None}
        if not isinstance(window, dict) or not (key := window_key(window)):
            continue
        candidate = {key: value for key, value in window.items() if value is not None}
        candidate["observed_at"] = str(window.get("observed_at") or stamp)
        current = window_map.get(key)
        if not isinstance(current, dict) or newer(candidate, current):
            window_map[key] = merged_window(current if isinstance(current, dict) else {}, candidate)
    merged["windows"] = window_map
    if isinstance(verdict, dict):
        verdict_map = verdicts(merged)
        key = scope_key(verdict.get("scope_model"))
        candidate_verdict = dict(verdict)
        candidate_verdict["observed_at"] = str(verdict.get("observed_at") or stamp)
        candidate_verdict["scope_model"] = None if key == ACCOUNT_SCOPE else key
        if newer(candidate_verdict, verdict_map.get(key)):
            verdict_map[key] = candidate_verdict
        merged["vendor_verdicts"] = verdict_map
    if check_error is not None:
        merged["last_check"] = {
            "outcome": "unknown", "at": stamp, "error": check_error[:300],
        }
    elif isinstance(verdict, dict):
        merged["last_check"] = {
            "outcome": verdict.get("state", "unknown"), "at": str(verdict.get("observed_at") or stamp),
            "error": None,
        }
    return merged


def record_usage(
    *, provider: str, windows: Sequence[dict[str, Any]] = (), facts: dict[str, Any] | None = None,
    verdict: dict[str, Any] | None = None, check_error: str | None = None,
    observed_before: datetime | None = None, release_signal: bool = True,
    receipt: ReceiptFence | None, db=None,
) -> None:
    """Atomically merge observations into the state of the account ``receipt`` names.

    A write whose login is no longer current changes nothing (logged once).

    An accepted ``allowed`` verdict that replaces a ``limited`` one releases that
    scope's held heads after the commit, unless ``release_signal`` is off
    because the caller drives the release itself. Any change to a scope's
    verdict pushes ``models_updated`` after the commit, so the catalog, chip and
    held line follow it without a reload.
    """
    with write_transaction(db, scope="harness_usage") as connection:
        identity = write_account(provider, receipt, db=connection)
        if identity is None:
            return
        _, usage = _load(connection, provider, identity)
        from connections.account_evidence import readiness_reason
        from connections.account_receipts import limit_subject
        from connections.vendor_accounts import get_account

        account = get_account(identity, db=connection)
        runtime_effects = readiness_reason(account) is None
        key = scope_key(verdict.get("scope_model")) if isinstance(verdict, dict) else None
        current_verdict = verdicts(usage).get(key) if isinstance(usage, dict) and key else None
        if observed_before is not None and isinstance(current_verdict, dict):
            observed = parse_time(current_verdict.get("observed_at"))
            if observed is None or observed >= observed_before:
                return
        merged = _merge_usage(
            usage if isinstance(usage, dict) else _empty_usage(), windows=windows,
            facts=facts, verdict=verdict, check_error=check_error,
        )
        plan_limits = derive_plan_limits(merged, usage, _now())
        _store(connection, merged, identity)
        append_changed(
            connection, identity,
            usage.get("windows") if isinstance(usage, dict) else None, merged["windows"],
        )
        if _verdict_states(merged) != _verdict_states(usage):
            from infra.state.ws import schedule_models_changed

            after_commit(connection, schedule_models_changed)
        prior = verdicts(usage) if isinstance(usage, dict) else {}
        allowed = [scope for scope, stored in verdicts(merged).items() if runtime_effects and (
            stored.get("state") == "allowed" and (prior.get(scope) or {}).get("state") != "allowed")]
        for scope in allowed if release_signal else ():
            if (prior.get(scope) or {}).get("state") != "limited":
                continue
            from infra.connection_limit_drive import schedule_scope_release

            subject = limit_subject(provider, scope, receipt=receipt)
            after_commit(connection, lambda subject=subject: schedule_scope_release(subject))
        if allowed:
            # This account can now take over heads held on another account's limit.
            from connections.account_rollover import offer_destination

            offer_destination(provider, identity, db=connection)
        if plan_limits and runtime_effects:
            from infra.connection_limit_release import apply_plan_limit_effect

            apply_plan_limit_effect(provider, plan_limits, receipt=receipt, db=connection)


def _verdict_states(usage: object) -> dict[str, tuple[Any, Any, Any]]:
    """What a catalog reader shows per scope; observation times alone change nothing."""
    items = verdicts(usage).items() if isinstance(usage, dict) else ()
    return {key: (v.get("state"), v.get("reason"), v.get("resets_at")) for key, v in items if isinstance(v, dict)}


def mark_readings_stale(*, provider: str, account_id: str, db=None) -> None:
    """Drop every stored window's percentage: it read the account before this sign-in."""
    with write_transaction(db, scope="harness_usage") as connection:
        identity, usage = _load(connection, provider, account_id)
        windows = usage.get("windows") if isinstance(usage, dict) else None
        if identity is not None and isinstance(usage, dict) and isinstance(windows, dict):
            stale = {key: without_reading(window) for key, window in windows.items() if isinstance(window, dict)}
            _store(connection, {**usage, "windows": stale}, identity)


def write_release(
    subject: LimitSubject, resets_at: str, state: str, *, at: str,
    thread_id: str | None = None, error: str | None = None,
    owner_thread_id: str | None = None, db=None,
) -> bool:
    """Compare-and-set the release marker of one limited verdict's reset window.

    It writes only while the verdict is still ``limited`` with ``resets_at`` and
    the window is unowned (no marker, or ``failed``), or ``owner_thread_id``
    names the ``started`` probe that owns it. Returns whether it wrote.
    """
    with write_transaction(db, scope="usage_release") as connection:
        from connections.account_receipts import subject_is_current

        if not subject_is_current(subject, db=connection):
            return False
        identity, usage = _load(connection, subject.provider, subject.account_id)
        verdict_map = verdicts(usage) if isinstance(usage, dict) else {}
        verdict = verdict_map.get(subject.scope_key)
        if (
            identity is None or not isinstance(usage, dict) or not isinstance(verdict, dict)
            or verdict.get("state") != "limited"
            or parse_time(verdict.get("resets_at")) != parse_time(resets_at)
        ):
            return False
        current = release_marker(verdict)
        owned = (
            owner_thread_id is not None and current is not None
            and current.get("state") == "started" and current.get("thread_id") == owner_thread_id
        )
        if current is not None and current.get("state") != "failed" and not owned:
            return False
        verdict_map[subject.scope_key] = {
            **verdict,
            "release": next_marker(verdict, state, at=at, thread_id=thread_id, error=error),
        }
        _store(connection, {**usage, "vendor_verdicts": verdict_map}, identity)
        return True


def record_limited(
    *, reason: str, resets_at: str | None, source: str, provider: str,
    scope_model: str | None = None, receipt: ReceiptFence | None, db=None,
) -> None:
    """Record the vendor's explicit unavailable verdict without deleting windows."""
    record_usage(provider=provider, verdict={
        "state": "limited", "reason": reason, "resets_at": resets_at,
        "source": source, "scope_model": scope_model,
    }, receipt=receipt, db=db)
    log.warning("%s usage limit reported (%s, resets %s)", provider, source, resets_at)


def record_allowed(
    *, source: str, provider: str, observed_before: datetime | None = None,
    scope_model: str | None = None, release_signal: bool = True,
    receipt: ReceiptFence | None, db=None,
) -> None:
    """Record a vendor-supported recovery; retained observations are never removed."""
    record_usage(provider=provider, observed_before=observed_before, verdict={
        "state": "allowed", "source": source, "scope_model": scope_model,
    }, release_signal=release_signal, receipt=receipt, db=db)


def set_allow_overage(provider: str, account_id: str, allow: bool, *, db=None) -> None:
    """Store one account's 'Spend past usage limit' switch.

    On releases each scope this policy holds there through the ordinary ``allowed``
    verdict; off takes effect at the next usage observation, never retroactively.
    """
    with write_transaction(db, scope="allow_overage") as connection:
        identity, usage = _load(connection, provider, account_id)
        updated = dict(usage) if isinstance(usage, dict) else _empty_usage()
        updated[ALLOW_OVERAGE_KEY] = allow
        _store(connection, updated, cast(str, identity))
        receipt = capture_receipt(provider, account_id=identity, db=connection)
        if allow:
            for scope in plan_limited_scopes(updated, _now()):
                record_allowed(source="overage_setting", provider=provider, scope_model=scope,
                               receipt=receipt, db=connection)
        else:
            record_usage(provider=provider, receipt=receipt, db=connection)


def record_check_failed(*, error: str, provider: str, receipt: ReceiptFence | None, db=None) -> None:
    """Preserve a known limit and make an indeterminate refresh visible."""
    record_usage(provider=provider, check_error=error, receipt=receipt, db=db)
    log.warning("%s usage check could not verify availability: %s", provider, error)



def record_rate_limits_read(result: object, *, provider: str, receipt: ReceiptFence | None, db=None) -> None:
    """Apply one full read through the canonical usage owner and its login fence."""
    from connections.codex_usage_read import normalize_rate_limits_read

    record_usage(provider=provider, receipt=receipt, db=db, **normalize_rate_limits_read(result))


def allows_overage(provider: str, account_id: str, *, db=None) -> bool:
    """One account's 'Spend past usage limit' switch (off by default)."""
    from connections.overage_policy import allows_overage as switch_on

    return switch_on(stored_usage(provider=provider, account_id=account_id, db=db))


def stored_usage(*, provider: str, account_id: str, db=None) -> dict[str, Any] | None:
    connection = db or get_db()
    try:
        return _load(connection, provider, account_id)[1]
    finally:
        if db is None:
            connection.close()


def active_limit(*, provider: str, account_id: str, now: datetime | None = None, db=None) -> dict[str, Any] | None:
    """The account-wide blocking condition: an active vendor ``limited`` verdict."""
    usage = stored_usage(provider=provider, account_id=account_id, db=db)
    verdict = effective_verdict(usage, None, now or _now(), provider).blocking
    if verdict is None:
        return None
    result = dict(verdict)
    check = usage.get("last_check") if isinstance(usage, dict) else None
    if isinstance(check, dict) and check.get("error"):
        result["check_error"] = check["error"]
        result["checked_at"] = check.get("at")
    return result


def account_usage(
    usage: dict[str, Any] | None, *, now: datetime | None = None, provider: str | None = None,
) -> dict[str, Any]:
    """One account's display usage using the caller's clock or this owner's patchable clock."""
    return _account_usage(usage, now=now or _now(), provider=provider)


def project_for_model(
    usage: dict[str, Any] | None, *, model: str | None = None, now: datetime | None = None, provider: str | None = None,
) -> dict[str, Any]:
    """Project plan windows using the caller's clock or this owner's patchable clock."""
    return _project_for_model(usage, model=model, now=now or _now(), provider=provider)


def connection_availability(
    model: str | None, *, account_id: str | None, db=None, now: datetime | None = None,
) -> ConnectionAvailability:
    """Return a pure, model-scoped availability read of one account using the caller's DB connection.

    No resolved account (``None``) has no usage to read, so it is ``unknown``.
    """
    if db is None:
        return ConnectionAvailability("unknown", None, check_error="database connection required")
    from llm.model_status import harness_connection_for_model

    connection = harness_connection_for_model(model, db=db)
    if connection is None or account_id is None:
        return ConnectionAvailability("unknown", connection)
    projection = project_for_model(stored_usage(provider=connection, account_id=account_id, db=db), model=model, now=now,
                                   provider=connection)
    return availability_from_projection(connection, projection)
