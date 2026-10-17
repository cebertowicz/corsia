"""Where first-run setup stands, kept by the backend: one record per install.

The record is the only owner of "did this person finish setup". It holds the
step reached after the account exists, the person's local-AI choice, and
``setup_completed``, which is written only when they finish. Eligibility facts
(memory, disk, sources) are never stored here: a resumed setup re-reads them.

An install with no record, or one made by ``corsia user add``, has no setup to
resume: with an account present it counts as completed. Only a record that
reached a step and was never completed reopens setup.
"""

from __future__ import annotations

import sqlite3

from infra.settings import get_setting, set_setting

KEY = "setup_progress"
#: Steps after the account exists: Connect an AI, and the existing-oMLX question inside it.
STEPS = frozenset({"models", "local-consent"})


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
    return {
        "setup_completed": not incomplete,
        "step": record["step"] if incomplete else None,
        "local_ai": local_ai if isinstance(local_ai, bool) else None,
    }


def start_after_account(*, db: sqlite3.Connection) -> None:
    """The first account was made in the app: setup is now at Connect an AI, with any earlier choice kept."""
    set_setting(KEY, {**(read(db=db) or {}), "step": "models"}, db=db)


def update(
    *, step: str | None = None, local_ai: bool | None = None, completed: bool = False,
    db: sqlite3.Connection | None = None,
) -> None:
    """Record a step, the local-AI choice or completion; a completed setup is never reopened."""
    if step is not None and step not in STEPS:
        raise SetupProgressError("unknown setup step")
    record = read(db=db) or {}
    if record.get("setup_completed") is True:
        return
    if step is not None and "step" in record:  # only a setup the app started can move; none is begun here
        record["step"] = step
    if local_ai is not None:
        record["local_ai"] = local_ai
    if completed:
        record["setup_completed"] = True
    set_setting(KEY, record, db=db)
