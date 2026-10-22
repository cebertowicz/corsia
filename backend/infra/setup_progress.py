"""Where first-run setup stands, kept by the backend: one record per install.

The record is the only owner of "did this person finish setup". It holds the
step reached after the account exists, the person's local-AI choice, which
subscriptions they said they pay for (``picks``), the setup-to-chat handoff, and
``setup_completed``, which is written only when they finish. Eligibility facts
(memory, disk, sources) are never stored here: setup re-reads them.

An install with no record, or one made by ``corsia user add``, has no setup in
progress: with an account present it counts as completed. A record the app's own
setup made and nobody finished is reset on the next launch (``onboarding.setup_reset``).
"""

from __future__ import annotations

import logging
import sqlite3

from infra.database import write_transaction
from infra.settings import get_setting, set_setting

logger = logging.getLogger(__name__)

KEY = "setup_progress"
#: The record field marking an account the app's own setup path created.
CREATED_BY_APP = "created_by_app"
#: The record field holding the profile write that waits for the person to finish setup.
PENDING_PROFILE = "pending_profile"
#: Steps after the account exists: Connect an AI, and the existing-oMLX question inside it.
STEPS = frozenset({"models", "local-consent"})
#: What the person says they pay for: the two subscriptions.
PICKS = ("claude", "chatgpt")


class SetupProgressError(ValueError):
    """A change the record refuses: an unknown step, or one outside what the caller may write."""


def read(*, db: sqlite3.Connection | None = None) -> dict | None:
    """The stored record, or None when this install never started setup in the app."""
    return get_setting(KEY, db=db)


def view(*, db: sqlite3.Connection | None = None) -> dict:
    """What the signed-in app reads: whether setup is completed, and where an unfinished one stands."""
    record = read(db=db) or {}
    incomplete = record.get("step") in STEPS and record.get("setup_completed") is not True
    local_ai = record.get("local_ai")
    picks = record.get("picks")
    return {
        "setup_completed": not incomplete,
        "step": record["step"] if incomplete else None,
        "local_ai": local_ai if isinstance(local_ai, bool) else None,
        "picks": {key: picks.get(key) is True for key in PICKS} if isinstance(picks, dict) else None,
    }


def clear(*, db: sqlite3.Connection) -> None:
    """Forget the record, so the install reads as one that never started setup."""
    db.execute("DELETE FROM settings WHERE key = ?", (KEY,))


def start_after_account(
    first_run_path: str | None, *, db: sqlite3.Connection, user_id: str, created_files: dict[str, str] | None = None,
    pending_profile: dict[str, str] | None = None,
) -> None:
    """The first account was made in the app: setup is now at Connect an AI, with any earlier choice kept.

    The record also gets the creation marker: who the app made, in which work folder, and the digest of each
    file it created there. Only an install with this marker can have an unfinished setup reset
    (``onboarding.setup_reset``); an adopted, taken-over or older install never has one.
    ``pending_profile`` is a profile write held until setup is finished (see :func:`update`).
    """
    if first_run_path != "app":
        return
    from infra import config

    marker = {"user_id": user_id, "workspace": str(config.WORKSPACE), "files": dict(created_files or {})}
    record = {**(read(db=db) or {}), "step": "models", CREATED_BY_APP: marker}
    if pending_profile:
        record[PENDING_PROFILE] = pending_profile
    set_setting(KEY, record, db=db)


def update(
    *, step: str | None = None, local_ai: bool | None = None, picks: dict[str, bool] | None = None,
    handoff: dict | None = None, completed: bool = False, db: sqlite3.Connection | None = None,
) -> None:
    """Record setup state and the validated setup-to-chat handoff; a completed setup never reopens.

    Finishing also writes the profile change held since the account was made (an email for a profile the
    person already had), so nothing in their work folder changes while setup is unfinished.
    """
    if step is not None and step not in STEPS:
        raise SetupProgressError("unknown setup step")
    if picks is not None and (set(picks) != set(PICKS) or not all(isinstance(on, bool) for on in picks.values())):
        raise SetupProgressError("picks must say yes or no for each of claude and chatgpt")
    # One write transaction around the read and the write: overlapping changes queue, so a step
    # written late can never overwrite a completion that landed first.
    pending = None
    with write_transaction(db, scope="setup_progress_update") as connection:
        record = read(db=connection) or {}
        if record.get("setup_completed") is True:
            return
        if step is not None and "step" in record:  # only a setup the app started can move; none is begun here
            record["step"] = step
        if local_ai is not None:
            record["local_ai"] = local_ai
        if picks is not None:
            record["picks"] = {key: picks[key] for key in PICKS}
        if handoff is not None:
            record["handoff"] = handoff
        if completed:
            record["setup_completed"] = True
            pending = record.pop(PENDING_PROFILE, None)
        set_setting(KEY, record, db=connection)
    if pending:
        _write_pending_profile(pending)


def _write_pending_profile(pending: dict) -> None:
    """Write the held profile change; a failure is logged and never blocks finishing setup."""
    from infra import config, user_profile

    try:
        staged = user_profile.stage_user_profile(
            config.WORKSPACE, pending["full_name"], pending.get("timezone", ""), pending.get("email", ""),
        )
        if staged is not None:
            staged.publish()
    except (OSError, ValueError, KeyError):
        logger.exception("Could not write the profile change held until setup finished")
