"""Activate a classifier version only after its Measurement-window backfill."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from loguru import logger

from core.corrections_recording import ACTIVATIONS_SETTING, ACTIVE_VERSION_SETTING


def measurement_window_start(now: datetime | None = None) -> datetime:
    """Use Measurement's one baseline constant plus its current weekly period."""
    from api.measurement_agents import BASELINE_PERIODS

    current = now or datetime.now(timezone.utc)
    return current - timedelta(weeks=BASELINE_PERIODS + 1)


def _version_key(version) -> str:
    return f"{version.asset_id}:{version.version}:{version.head}:{version.threshold:.6f}:{version.input_policy_id}"


def complete_backfill(
    version, *, owner_user_id: str, weeks: int, before_count: float | None, after_count: float,
) -> bool:
    """Atomically activate a fully backfilled version exactly once."""
    from infra.database import write_transaction
    from infra.settings import get_setting, set_setting

    key = _version_key(version)
    with write_transaction(None, scope="complete_corrections_classifier_backfill") as db:
        activations = get_setting(ACTIVATIONS_SETTING, db=db) or {}
        if key in activations:
            return False
        activations[key] = {
            "owner_user_id": owner_user_id,
            "weeks": weeks,
            "before_count": before_count,
            "after_count": after_count,
            "version": {
                "asset_id": version.asset_id, "version": version.version, "head": version.head,
                "threshold": version.threshold, "input_policy_id": version.input_policy_id,
            },
            "conductor_told_at": None,
        }
        set_setting(ACTIVATIONS_SETTING, activations, db=db)
        set_setting(ACTIVE_VERSION_SETTING, {"key": key}, db=db)
    return True


def weekly_correction_figure(version, *, since: str, weeks: int) -> float:
    """Return the bounded-window weekly correction figure for one full version."""
    from core.corrections_recording import CORRECTION_EVENT
    from infra.database import get_db

    db = get_db()
    try:
        row = db.execute(
            """SELECT COUNT(*) FROM outcome_events
               WHERE event_name=? AND retracted_at IS NULL AND ts >= ?
                 AND json_extract(attributes, '$.asset_id')=?
                 AND json_extract(attributes, '$.asset_version')=?
                 AND json_extract(attributes, '$.head')=?
                 AND json_extract(attributes, '$.threshold')=?
                 AND json_extract(attributes, '$.input_policy_id')=?
                 AND json_extract(attributes, '$.label')='correction'""",
            (CORRECTION_EVENT, since, version.asset_id, version.version, version.head,
             f"{version.threshold:.6f}", version.input_policy_id),
        ).fetchone()
        return int(row[0]) / weeks
    finally:
        db.close()


async def tell_conductor_about_activation(version) -> None:
    """Send one accepted notice; persist the mark only after delivery accepts it."""
    from core.agent_roster_queries import workspace_conductor_contact
    from infra.database import get_db
    from infra.settings import get_setting

    key = _version_key(version)

    def load():
        db = get_db()
        try:
            activations = get_setting(ACTIVATIONS_SETTING, db=db) or {}
            activation = activations.get(key)
            if not activation or activation.get("conductor_told_at"):
                return None, None, None
            contact, gap = workspace_conductor_contact(str(activation.get("owner_user_id") or ""), db)
            return contact, activation, gap
        finally:
            db.close()

    from infra.db_async import run_in_db

    contact, activation, gap = await run_in_db(load)
    if contact is None:
        if activation is not None and gap:
            logger.warning("corrections activation conductor notice skipped: {}", gap)
        return
    old = activation["before_count"]
    if old is None:
        detail = f"Corrections are now counted for the last {activation['weeks']} weeks."
    else:
        detail = (
            f"Corrections for the last {activation['weeks']} weeks were re-read with classifier "
            f"version {version.version}; the weekly corrections figure moved from {old:.1f} to {activation['after_count']:.1f}."
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
