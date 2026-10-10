"""Durable delivery orchestration for bounded signed live bulletins."""
from __future__ import annotations

import asyncio
import hashlib
import random
from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

import infra.bulletin_ledger as bulletin_ledger
from infra import bulletins
from infra.bulletin_fetch import FetchFailure, FetchedBulletins, _date, fetch_live_bulletins
from infra.db_async import run_in_db
from infra.system_package import SYSTEM_VERSION

_DAILY_SECONDS = 24 * 60 * 60
_SYSTEM_RETRY_SECONDS = 60 * 60


def _record_failure(db, failure: FetchFailure) -> None:
    """Park one reason/item failure per UTC day; failures never reach an agent."""
    from events import dead_letter

    reason = "bulletin_" + failure.reason
    today = datetime.now(timezone.utc).date().isoformat()
    row = db.execute(
        "SELECT 1 FROM dead_letter_events WHERE internal_event=? AND reason=? "
        "AND external_id IS ? AND substr(created_at,1,10)=? LIMIT 1",
        ("system_bulletin.infrastructure", reason, failure.item, today),
    ).fetchone()
    if row is None:
        dead_letter.park(
            "system_bulletin.infrastructure", {"reason": failure.reason, "item": failure.item},
            reason, failure.item, db=db,
        )


def _applicable(fields: dict[str, Any]) -> bool:
    """Apply the currently supported System-version and provider filters."""
    try:
        served = Version(SYSTEM_VERSION.removeprefix("v"))
        if served not in SpecifierSet(fields["system_version"]):
            return False
    except (InvalidSpecifier, InvalidVersion):
        raise ValueError("invalid system_version range")
    wanted = fields.get("providers")
    if wanted is None:
        return True
    from infra.settings import get_providers

    configured = get_providers()
    return isinstance(configured, dict) and bool(set(wanted) & set(configured))


def _fetch_delay(failures: int) -> int:
    return _DAILY_SECONDS if failures <= 0 else min(_DAILY_SECONDS, _SYSTEM_RETRY_SECONDS * (2 ** (failures - 1)))


def _seconds_until_live_fetch(state: dict[str, int | str | None], now: datetime) -> int:
    """Return the one durable live-feed deadline; malformed state retries immediately."""
    value = state["next_live_at"]
    if not isinstance(value, str):
        return 0
    try:
        scheduled = datetime.fromisoformat(value)
    except ValueError:
        return 0
    if scheduled.tzinfo is None:
        scheduled = scheduled.replace(tzinfo=timezone.utc)
    return min(_DAILY_SECONDS, max(0, int((scheduled - now).total_seconds())))


def _live_fetch_due(state: dict[str, int | str | None], now: datetime) -> bool:
    return _seconds_until_live_fetch(state, now) == 0


def _prepare_check() -> tuple[dict[str, Any] | None, str | None, str | None]:
    """Read/claim durable state on a DB worker before any async delivery."""
    from infra.conductor import WORKSPACE_SCOPE, get_conductor
    from infra.database import get_db, write_transaction

    db = get_db()
    try:
        conductor = get_conductor(WORKSPACE_SCOPE, db=db)
        with write_transaction(db, scope="prepare_system_bulletin_delivery"):
            pending = bulletin_ledger.pending_system_change(db)
        preferences = bulletins.live_preferences(db)
        state = bulletins.live_runtime_state(db)
        due = (
            preferences["enabled"] is True
            and bool(bulletins.PUBLIC_KEYS)
            and _live_fetch_due(state, datetime.now(timezone.utc))
        )
        return pending, conductor, str(preferences["base_url"]) if due else None
    finally:
        db.close()


def _record_delivery_failures(bulletin_ids: list[str]) -> None:
    from infra.database import get_db, write_transaction

    db = get_db()
    try:
        with write_transaction(db, scope="record_system_bulletin_failure"):
            for bulletin_id in bulletin_ids:
                _record_failure(db, FetchFailure("delivery", bulletin_id))
    finally:
        db.close()


def _complete_system_delivery(bulletin_id: str) -> None:
    from infra.database import get_db, write_transaction

    db = get_db()
    try:
        with write_transaction(db, scope="complete_system_bulletin_delivery"):
            if bulletin_ledger.delivery_state(db, bulletin_id) == "pending":
                bulletin_ledger.complete_delivery(db, bulletin_id, "system_change")
    finally:
        db.close()


async def _deliver_system_change(pending: dict[str, Any] | None, conductor: str | None) -> bool:
    """Deliver a prepared System change, then complete only its surviving claim."""
    from events.delivery import deliver_to_thread
    from infra.product_events import emit_product_event

    if pending is None:
        return False
    if conductor is None:
        await run_in_db(_record_delivery_failures, [pending["id"]])
        return False
    delivered = await deliver_to_thread(
        conductor, "system.bulletin", "System instructions have changed.", pending,
        delivery_id=_delivery_id("system_change", [pending["id"]]), notice=bulletins.NOTICE,
    )
    if not delivered:
        await run_in_db(_record_delivery_failures, [pending["id"]])
        return False
    await run_in_db(_complete_system_delivery, pending["id"])
    emit_product_event("bulletin", "dispatched", properties={"kind": "system_change", "count": 1})
    return True


def _prepare_fetch_reconciliation(
    fetched: FetchedBulletins,
) -> tuple[list[tuple[str, list[dict[str, Any]]]], list[dict[str, Any]]]:
    """Persist fetch timing, classify candidates, and claim delivery on a DB worker."""
    from infra.database import get_db, write_transaction

    db = get_db()
    try:
        with write_transaction(db, scope="prepare_live_bulletin_delivery"):
            for failure in fetched.failures:
                _record_failure(db, failure)
            state = bulletins.live_runtime_state(db)
            failures = 0 if fetched.complete else int(state["failures"]) + 1
            next_live_at = (datetime.now(timezone.utc) + timedelta(seconds=_fetch_delay(failures))).isoformat()
            bulletins.save_live_runtime_state(
                db, failures=failures, next_live_at=next_live_at,
            )
            pending, withdrawals = _classify(db, fetched.bulletins)
            groups = _claim_pending_groups(db, pending)
        return groups, withdrawals
    finally:
        db.close()


def _complete_withdrawal(bulletin_id: str) -> None:
    from infra.database import get_db, write_transaction

    db = get_db()
    try:
        with write_transaction(db, scope="withdraw_live_bulletin"):
            bulletin_ledger.complete_withdrawal(db, bulletin_id, "live", after_notice=True)
    finally:
        db.close()


async def _deliver_withdrawals(withdrawals: list[dict[str, Any]], conductor: str | None) -> bool:
    from events.delivery import deliver_to_thread
    from infra.product_events import emit_product_event

    delivered_any = False
    for fields in withdrawals:
        delivered = False
        if conductor is not None:
            delivered = await deliver_to_thread(
                conductor, "system.bulletin.withdrawn", "A System bulletin was withdrawn.",
                {"id": fields["id"]}, delivery_id=_delivery_id("withdrawn", [fields["id"]]),
                notice=bulletins.WITHDRAWAL_NOTICE,
            )
        if not delivered:
            await run_in_db(_record_delivery_failures, [fields["id"]])
        else:
            await run_in_db(_complete_withdrawal, fields["id"])
            emit_product_event("bulletin", "dispatched", properties={"kind": "withdrawal", "count": 1})
        delivered_any = delivered_any or delivered
    return delivered_any


def _may_have_reached_conductor(db, bulletin_id: str) -> bool:
    """Tell whether a dispatched but uncompleted claim may already be with the conductor.

    A pending row without a delivery_id was never dispatched. With one, an accepted dispatch
    is a message whose id is the event's deterministic admission key and whose content names
    the bulletin; when the lookup cannot decide, assume it was accepted.
    """
    if bulletin_ledger.delivery_state(db, bulletin_id) != "pending":
        return False
    if bulletin_ledger.delivery_id(db, bulletin_id) is None:
        return False
    try:
        return db.execute(
            "SELECT 1 FROM messages WHERE id LIKE 'event:%' AND content LIKE '[EVENT: system.bulletins]%' "
            "AND instr(content, ?)>0 LIMIT 1",
            (f'"id": "{bulletin_id}"',),
        ).fetchone() is not None
    except Exception:  # Cannot tell: a spurious notice is cheaper than a silent withdrawal.
        logger.exception("bulletin acceptance lookup failed for {}", bulletin_id)
        return True


def _classify(db, candidates: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    from infra.database import write_transaction

    by_id: dict[str, list[dict[str, Any]]] = {}
    for fields in candidates:
        by_id.setdefault(fields["id"], []).append(fields)
    pending: list[dict[str, Any]] = []
    withdrawals: list[dict[str, Any]] = []
    for bulletin_id, versions in by_id.items():
        withdrawn = next((fields for fields in versions if fields.get("withdrawn")), None)
        if withdrawn is not None:
            with write_transaction(db, scope="classify_live_bulletin_withdrawal"):
                action = bulletin_ledger.withdrawal_action(db, bulletin_id, "live")
                if action == "silent" and _may_have_reached_conductor(db, bulletin_id):
                    # A completion write can fail after an accepted dispatch; tell the conductor.
                    bulletin_ledger.complete_delivery(db, bulletin_id, "live")
                    action = "notice"
                if action == "silent":
                    bulletin_ledger.complete_withdrawal(db, bulletin_id, "live", after_notice=False)
            if action == "notice":
                withdrawals.append(withdrawn)
            continue
        if bulletin_ledger.delivery_state(db, bulletin_id) == "withdrawn":
            continue
        fields = versions[-1]
        try:
            expired = _date(fields["expires"]) < datetime.now(timezone.utc).date()
            applicable = _applicable(fields)
        except ValueError:
            _record_failure(db, FetchFailure("unparseable", bulletin_id))
            continue
        if expired or not applicable:
            with write_transaction(db, scope="skip_live_bulletin"):
                bulletin_ledger.mark_skipped(
                    db, bulletin_id, "live", "expired" if expired else "not applicable to this workspace",
                )
            continue
        if bulletin_ledger.delivery_state(db, bulletin_id) in {None, "pending", "skipped"}:
            pending.append(fields)
    return pending, withdrawals


def _delivery_id(kind: str, ids: list[str]) -> str:
    digest = hashlib.sha256(",".join(sorted(ids)).encode()).hexdigest()
    return f"{kind}:{digest}"


def _claim_pending_groups(
    db, pending: list[dict[str, Any]],
) -> list[tuple[str, list[dict[str, Any]]]]:
    """Claim fresh chunks once and retain their key for retry-safe redelivery."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    fresh: list[dict[str, Any]] = []
    for fields in pending:
        delivery_id = bulletin_ledger.delivery_id(db, fields["id"])
        if delivery_id is None:
            fresh.append(fields)
        else:
            grouped.setdefault(delivery_id, []).append(fields)
    for chunk in _pending_chunks(fresh):
        delivery_id = _delivery_id("bulletins", [item["id"] for item in chunk])
        claimed = [
            fields for fields in chunk
            if bulletin_ledger.claim_delivery(db, fields["id"], "live", delivery_id=delivery_id)
        ]
        if claimed:
            grouped[delivery_id] = claimed
    return list(grouped.items())


def _pending_chunks(pending: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    chunks: list[list[dict[str, Any]]] = []
    chunk: list[dict[str, Any]] = []
    body_total = 0
    for fields in pending:
        body_size = len(fields["body"].encode("utf-8"))
        if chunk and body_total + body_size > bulletins.MAX_BATCH_BODY_BYTES:
            chunks.append(chunk)
            chunk = []
            body_total = 0
        chunk.append(fields)
        body_total += body_size
    return chunks + [chunk] if chunk else chunks


def _complete_live_delivery(bulletin_ids: list[str]) -> None:
    from infra.database import get_db, write_transaction

    db = get_db()
    try:
        with write_transaction(db, scope="complete_live_bulletin_delivery"):
            for bulletin_id in bulletin_ids:
                if bulletin_ledger.delivery_state(db, bulletin_id) == "pending":
                    bulletin_ledger.complete_delivery(db, bulletin_id, "live")
    finally:
        db.close()


async def _deliver_pending(
    groups: list[tuple[str, list[dict[str, Any]]]], conductor: str | None,
) -> bool:
    from events.delivery import deliver_to_thread
    from infra.product_events import emit_product_event

    delivered_any = False
    for delivery_id, chunk in groups:
        delivered = False
        if conductor is not None:
            delivered = await deliver_to_thread(
                conductor, "system.bulletins", "Here are new System bulletins.", {"bulletins": chunk},
                delivery_id=delivery_id,
                notice=bulletins.LIVE_NOTICE,
            )
        if not delivered:
            await run_in_db(_record_delivery_failures, [fields["id"] for fields in chunk])
        else:
            await run_in_db(_complete_live_delivery, [fields["id"] for fields in chunk])
            emit_product_event("bulletin", "dispatched", properties={"kind": "live", "count": len(chunk)})
        delivered_any = delivered_any or delivered
    return delivered_any


async def _reconcile_fetch(fetched: FetchedBulletins, conductor: str | None) -> bool:
    """Prepare durable live delivery on a DB worker, then dispatch without blocking it."""
    groups, withdrawals = await run_in_db(_prepare_fetch_reconciliation, fetched)
    delivered = await _deliver_withdrawals(withdrawals, conductor)
    return await _deliver_pending(groups, conductor) or delivered


async def check_live_bulletins() -> bool:
    """Reconcile System changes every check and live feed only at its own schedule."""
    pending, conductor, base_url = await run_in_db(_prepare_check)
    system_delivered = await _deliver_system_change(pending, conductor)
    if base_url is None:
        return system_delivered
    fetched = await fetch_live_bulletins(base_url=base_url)
    return await _reconcile_fetch(fetched, conductor) or system_delivered


def _next_loop_delay() -> int:
    """Compute the earlier System retry or durable live-fetch deadline on a DB worker."""
    from infra.database import get_db

    db = get_db()
    try:
        preferences = bulletins.live_preferences(db)
        state = bulletins.live_runtime_state(db)
        if preferences["enabled"] is not True or not bulletins.PUBLIC_KEYS:
            live_delay = _DAILY_SECONDS
        else:
            live_delay = _seconds_until_live_fetch(state, datetime.now(timezone.utc))
        return min(live_delay, _SYSTEM_RETRY_SECONDS) if bulletin_ledger.has_pending_system_change(db) else live_delay
    finally:
        db.close()


async def live_bulletin_loop() -> None:
    """Check immediately; retry pending System changes without accelerating the live feed."""
    while True:
        try:
            await check_live_bulletins()
            delay = await run_in_db(_next_loop_delay)
        except Exception as exc:
            logger.warning("live bulletin loop failed: {}", exc)
            delay = _SYSTEM_RETRY_SECONDS
        await asyncio.sleep(delay + random.randint(0, 30 * 60))
