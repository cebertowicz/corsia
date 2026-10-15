"""Activate a classifier version only after its Measurement-window backfill."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from loguru import logger

ACTIVATIONS_SETTING = "corrections_classifier_activations"
ACTIVE_VERSION_SETTING = "corrections_classifier_active_version"


def measurement_window_start(now: datetime | None = None) -> datetime:
    """Use Measurement's one baseline constant plus its current weekly period."""
    from api.measurement_agents import BASELINE_PERIODS

    current = now or datetime.now(timezone.utc)
    return current - timedelta(weeks=BASELINE_PERIODS + 1)


def _version_key(version) -> str:
    return f"{version.asset_id}:{version.version}:{version.head}:{version.threshold:.6f}:{version.input_policy_id}"


def active_version_key() -> str | None:
    """Return the one version Measurement may read, never an in-progress backfill."""
    from infra.database import get_db
    from infra.settings import get_setting

    db = get_db()
    try:
        value = get_setting(ACTIVE_VERSION_SETTING, db=db) or {}
        key = value.get("key")
        return key if isinstance(key, str) else None
    finally:
        db.close()


def active_correction_count() -> int | None:
    """Use the saved active-version figure for a version-switch notice."""
    from infra.database import get_db
    from infra.settings import get_setting

    db = get_db()
    try:
        active = get_setting(ACTIVE_VERSION_SETTING, db=db) or {}
        key = active.get("key")
        activations = get_setting(ACTIVATIONS_SETTING, db=db) or {}
        activation = activations.get(key) if isinstance(key, str) else None
        value = activation.get("after_count") if isinstance(activation, dict) else None
        return value if isinstance(value, int) else None
    finally:
        db.close()


def complete_backfill(
    version, *, owner_user_id: str, weeks: int, before_count: int | None, after_count: int,
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
            "conductor_told_at": None,
        }
        set_setting(ACTIVATIONS_SETTING, activations, db=db)
        set_setting(ACTIVE_VERSION_SETTING, {"key": key}, db=db)
    return True


def count_corrections(version) -> int:
    """Count retained correction facts for one immutable classifier version."""
    from core.corrections_recording import CORRECTION_EVENT
    from infra.database import get_db

    db = get_db()
    try:
        row = db.execute(
            """SELECT COUNT(*) FROM outcome_events
               WHERE event_name=? AND retracted_at IS NULL
                 AND json_extract(attributes, '$.asset_version')=?
                 AND json_extract(attributes, '$.head')=?
                 AND json_extract(attributes, '$.label')='correction'""",
            (CORRECTION_EVENT, version.version, version.head),
        ).fetchone()
        return int(row[0])
    finally:
        db.close()


async def tell_conductor_about_activation(version) -> None:
    """Send one accepted notice; persist the mark only after delivery accepts it."""
    from core.agent_roster_queries import workspace_conductor_contact
    from infra.database import get_db

    key = _version_key(version)

    def load():
        db = get_db()
        try:
            activations = __import__("infra.settings", fromlist=["get_setting"]).get_setting(ACTIVATIONS_SETTING, db=db) or {}
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
            f"version {version.version}; the weekly corrections figure moved from {old} to {activation['after_count']}."
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
