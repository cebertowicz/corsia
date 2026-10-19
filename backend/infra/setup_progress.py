"""Where first-run setup stands, kept by the backend: one record per install.

The record is the only owner of "did this person finish setup". It holds the
step reached after the account exists, the person's local-AI choice, what they
checked on Welcome (``picks``), the setup-to-chat handoff, and
``setup_completed``, which is written only when they finish. Eligibility facts
(memory, disk, sources) are never stored here: a resumed setup re-reads them.

An install with no record, or one made by ``corsia user add``, has no setup to
resume: with an account present it counts as completed. Only a record that
reached a step and was never completed reopens setup.
"""

from __future__ import annotations

import sqlite3

from infra.database import write_transaction
from infra.settings import get_setting, set_setting

KEY = "setup_progress"
#: Steps after the account exists: Connect an AI, and the existing-oMLX question inside it.
STEPS = frozenset({"models", "local-consent"})
#: What Welcome asks the person to check: the two subscriptions and a pay-as-you-go key.
PICKS = ("claude", "chatgpt", "key")
#: Terminal setup-download wakes that must survive completing before the first chat is rendered.
LOCAL_DOWNLOAD_WAKES = "local_download_wakes"


class SetupProgressError(ValueError):
    """A change the record refuses: an unknown step, or one outside what the caller may write."""


def read(*, db: sqlite3.Connection | None = None) -> dict | None:
    """The stored record, or None when this install never started setup in the app."""
    return get_setting(KEY, db=db)


def view(*, db: sqlite3.Connection | None = None) -> dict:
    """What the signed-in app reads: whether setup is completed, and where to reopen it if not."""
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


def start_after_account(first_run_path: str | None, *, db: sqlite3.Connection) -> None:
    """The first account was made in the app: setup is now at Connect an AI, with any earlier choice kept."""
    if first_run_path != "app":
        return
    set_setting(KEY, {**(read(db=db) or {}), "step": "models"}, db=db)


def update(
    *, step: str | None = None, local_ai: bool | None = None, picks: dict[str, bool] | None = None,
    handoff: dict | None = None, completed: bool = False, db: sqlite3.Connection | None = None,
) -> None:
    """Record setup state and the validated setup-to-chat handoff; a completed setup never reopens."""
    if step is not None and step not in STEPS:
        raise SetupProgressError("unknown setup step")
    if picks is not None and (set(picks) != set(PICKS) or not all(isinstance(on, bool) for on in picks.values())):
        raise SetupProgressError("picks must say yes or no for each of claude, chatgpt and key")
    # One write transaction around the read and the write: overlapping changes queue, so a step
    # written late can never overwrite a completion that landed first.
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
        set_setting(KEY, record, db=connection)


def record_local_download_wake(delivery_id: str, payload: dict, *, db: sqlite3.Connection | None = None) -> None:
    """Durably enqueue one normalized terminal setup-download outcome, idempotently."""
    with write_transaction(db, scope="setup_progress_local_download_enqueue") as connection:
        record = read(db=connection) or {}
        wakes = record.get(LOCAL_DOWNLOAD_WAKES)
        wakes = dict(wakes) if isinstance(wakes, dict) else {}
        wakes.setdefault(delivery_id, payload)
        record[LOCAL_DOWNLOAD_WAKES] = wakes
        set_setting(KEY, record, db=connection)


def pending_local_download_wakes(*, db: sqlite3.Connection | None = None) -> dict[str, dict]:
    """The persisted terminal outcomes that no successful event delivery has settled yet."""
    wakes = (read(db=db) or {}).get(LOCAL_DOWNLOAD_WAKES)
    return {key: value for key, value in wakes.items() if isinstance(key, str) and isinstance(value, dict)} \
        if isinstance(wakes, dict) else {}


def settle_local_download_wake(delivery_id: str, *, db: sqlite3.Connection | None = None) -> None:
    """Remove a wake only after event delivery confirms it; a later retry then cannot duplicate it."""
    with write_transaction(db, scope="setup_progress_local_download_settle") as connection:
        record = read(db=connection) or {}
        wakes = record.get(LOCAL_DOWNLOAD_WAKES)
        if not isinstance(wakes, dict) or delivery_id not in wakes:
            return
        updated = dict(wakes)
        del updated[delivery_id]
        if updated:
            record[LOCAL_DOWNLOAD_WAKES] = updated
        else:
            record.pop(LOCAL_DOWNLOAD_WAKES, None)
        set_setting(KEY, record, db=connection)
