"""Activate a classifier version only after its Measurement-window backfill."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from loguru import logger

from core.corrections_recording import (
    ACTIVATIONS_SETTING, ACTIVE_VERSION_SETTING, ClassifierVersion, version_attributes, version_key,
    version_match_sql,
)
from infra.db_async import run_in_db


def measurement_window_start(now: datetime | None = None) -> datetime:
    """Use Measurement's one baseline constant plus its current weekly period."""
    from api.measurement_agents import BASELINE_PERIODS

    current = now or datetime.now(timezone.utc)
    return current - timedelta(weeks=BASELINE_PERIODS + 1)


def complete_backfill(
    version: ClassifierVersion, *, owner_user_id: str, weeks: int, before_count: float | None,
    after_count: float,
) -> bool:
    """Atomically activate a fully backfilled version exactly once."""
    from infra.database import write_transaction
    from infra.settings import get_setting, set_setting

    key = version_key(version)
    with write_transaction(None, scope="complete_corrections_classifier_backfill") as db:
        activations = get_setting(ACTIVATIONS_SETTING, db=db) or {}
        if key in activations:
            return False
        activations[key] = {
            "owner_user_id": owner_user_id,
            "weeks": weeks,
            "before_count": before_count,
            "after_count": after_count,
            "version": version_attributes(version),
            "conductor_told_at": None,
            # A scan fact notes only a source made after this (``core.correction_notes``).
            "activated_at": datetime.now(timezone.utc).isoformat(),
        }
        set_setting(ACTIVATIONS_SETTING, activations, db=db)
        set_setting(ACTIVE_VERSION_SETTING, {"key": key}, db=db)
    return True


def weekly_correction_figure(
    version: ClassifierVersion, *, since: str, weeks: int, messages_only: bool = False,
) -> float:
    """Return the bounded-window weekly correction figure for one full version.

    Message and document-comment ``correction`` facts count, one per comment.
    Decision answers remain triage-visible but are excluded from served
    correction rates. ``messages_only`` counts chat messages alone: the
    like-for-like figure two versions are compared on.
    """
    from core.corrections_recording import (
        CORRECTION_EVENT, DECISION_SUBJECT_PREFIX, LABEL_CORRECTION, MESSAGE_SUBJECT_PREFIX,
    )
    from infra.database import get_db

    version_filter, version_params = version_match_sql("e", version)
    sources = "e.subject_key LIKE :message_prefix || '%'" if messages_only else "e.subject_key NOT LIKE :decision_prefix || '%'"
    db = get_db()
    try:
        row = db.execute(
            f"""SELECT COUNT(*) FROM outcome_events e
               WHERE e.event_name=:event AND e.retracted_at IS NULL AND e.ts >= :since
                 AND {version_filter} AND {sources}
                 AND json_extract(e.attributes, '$.label')=:label""",
            {
                **version_params, "event": CORRECTION_EVENT, "since": since, "label": LABEL_CORRECTION,
                "decision_prefix": DECISION_SUBJECT_PREFIX, "message_prefix": MESSAGE_SUBJECT_PREFIX,
            },
        ).fetchone()
        return int(row[0]) / weeks
    finally:
        db.close()


async def tell_conductor_about_activation(version: ClassifierVersion) -> None:
    """Send one accepted notice; persist the mark only after delivery accepts it."""
    from core.agent_roster_queries import corsia_agent_contact
    from infra.database import get_db
    from infra.settings import get_setting

    key = version_key(version)

    def load():
        db = get_db()
        try:
            activations = get_setting(ACTIVATIONS_SETTING, db=db) or {}
            activation = activations.get(key)
            if not activation or activation.get("conductor_told_at"):
                return None, None, None
            contact, gap = corsia_agent_contact(str(activation.get("owner_user_id") or ""), db)
            return contact, activation, gap
        finally:
            db.close()

    contact, activation, gap = await run_in_db(load)
    if contact is None:
        if activation is not None and gap:
            logger.warning("corrections activation conductor notice skipped: {}", gap)
        return
    old = activation["before_count"]
    installed = f"Corrections classifier version {version.version} was installed and activated. "
    if old is None:
        detail = installed + f"Corrections are now counted for the last {activation['weeks']} weeks."
    else:
        detail = installed + (
            f"Corrections for the last {activation['weeks']} weeks were re-read with it; "
            f"the weekly figure for chat-message corrections moved from {old:.1f} to {activation['after_count']:.1f}. "
            "It compares chat messages only, so a change in which sources are read does not move it."
        )
    from events.delivery import deliver_to_thread

    sent = await deliver_to_thread(
        str(contact["id"]), "system.corrections_classifier_activated", detail,
        {"classifier_version": version.version, "head": version.head},
        delivery_id=f"corrections-classifier-activation:{key}",
    )
    if not sent:
        logger.warning("corrections activation conductor notice could not be delivered")
        return

    def mark_told():
        from infra.database import write_transaction
        from infra.settings import get_setting, set_setting
        from infra.time import utc_now_iso

        with write_transaction(None, scope="mark_corrections_activation_conductor_told") as db:
            activations = get_setting(ACTIVATIONS_SETTING, db=db) or {}
            activation = activations.get(key)
            if activation is not None and not activation.get("conductor_told_at"):
                activation["conductor_told_at"] = utc_now_iso()
                activations[key] = activation
                set_setting(ACTIVATIONS_SETTING, activations, db=db)

    await run_in_db(mark_told)


async def retry_pending_activation_notice() -> None:
    """Retry an accepted-delivery notice after restart or an interrupted mark."""
    from core.corrections_recording import active_corrections_version

    version = await run_in_db(active_corrections_version)
    if isinstance(version, ClassifierVersion):
        await tell_conductor_about_activation(version)


async def activate_after_backfill(version: ClassifierVersion) -> str:
    """Switch Measurement only after this immutable version's bounded fill completes.

    Returns ``done``, or the reason the scan stays held.
    """
    from api.measurement_agents import BASELINE_PERIODS
    from core.corrections_recording import CorruptActivation, active_corrections_version
    from jobs.corrections_recording import owner_user_ids

    owners = await run_in_db(owner_user_ids)
    if len(owners) != 1:
        # Held, not done: the snapshot must show why this version is never activated here.
        return f"activation held: expected one owner, found {len(owners)}"
    old_version = await run_in_db(active_corrections_version)
    if isinstance(old_version, CorruptActivation):
        # Never treat an unreadable record as no version: that would replace it silently.
        return "activation record unreadable"
    weeks = BASELINE_PERIODS + 1
    since = measurement_window_start().isoformat()
    # Like for like: both versions' chat-message figures (``before_count``/``after_count``).
    before = (
        await run_in_db(weekly_correction_figure, old_version, since=since, weeks=weeks, messages_only=True)
        if old_version is not None else None
    )
    after = await run_in_db(weekly_correction_figure, version, since=since, weeks=weeks, messages_only=True)
    activated = await run_in_db(
        complete_backfill,
        version,
        owner_user_id=owners[0],
        weeks=weeks,
        before_count=before,
        after_count=after,
    )
    if activated:
        await tell_conductor_about_activation(version)
    await retry_pending_activation_notice()
    return "done"
