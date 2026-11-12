"""Whether a rule-started worker's model runs now, and the one refusal that waits first.

Whether it runs is ``effective_status`` on the account its next turn runs on, the one start
decision. The only refusal that waits is a Claude Code or Codex account at its usage limit
in a project that rolls that vendor over, with another account ready whose usage is not read
yet: rollover never moves onto unread usage, and reading it (one read per account per
``harness_usage_check.RECHECK_SECONDS``, scheduled by that same rollover check) may make that
account the turn's. The runner puts such a start back in the queue at its place, where it
takes no slot and is decided again on the next pass, and claims a younger start in its slot.
The wait ends when the read is in, either way, or after ``WAIT_SECONDS`` (a failed read, or
none): then the start is refused in the same words. The workflow's time limit plays no part;
it counts from the worker's start. A restart forgets the wait.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Mapping
from typing import Any

from automation import worker_inputs as inputs
from connections.harness_usage_check import RECHECK_SECONDS

#: How long a start waits on an unread account: the read throttle, a pass to schedule the
#: read, the read's own bound (under a minute) and a pass to see it.
WAIT_SECONDS = RECHECK_SECONDS + 3 * 60
#: Decision id -> monotonic time its start first waited. Process memory.
_waiting: dict[int, float] = {}


def check_runs(row: Mapping[str, Any], name: str, project: str | None, db: sqlite3.Connection) -> None:
    """Return when the owner's model runs now; else raise :class:`worker_inputs.StartRefused`,
    or :class:`worker_inputs.StartWaiting` while an unread account may yet take its turn."""
    from llm.account_catalog import effective_status

    if effective_status(row)[0] == "ready":
        return
    if row.get("is_local"):
        raise inputs.StartRefused(f"refused: your model {name} is not loaded. "
                                  "Load it, or choose a local model that is loaded.")
    refusal = f"refused: your model {name} can't run now. Choose a model that is ready for this chat."
    raise (inputs.StartWaiting if _usage_unread(row, project, db) else inputs.StartRefused)(refusal)


def _usage_unread(row: Mapping[str, Any], project: str | None, db: sqlite3.Connection) -> bool:
    """Whether a refusal is worth asking again: the selected account is at its usage limit, the
    project rolls this vendor over, and another account is ready with its usage not read as
    allowed. Never whether the start runs; the next pass asks ``effective_status`` again."""
    from connections.provider_policy import effective_policy
    from llm.model_status import USAGE_LIMITED

    runs_on = row.get("runs_on") or ()
    selected = next((entry for entry in runs_on if entry["selected"]), None)
    if selected is None or selected["status_reason"] != USAGE_LIMITED or not any(
            not entry["selected"] and entry["status"] == "ready" and entry["usage"].get("state") != "allowed"
            for entry in runs_on):
        return False
    return bool(effective_policy(project, str(row["connection"]), db=db)["rollover"])


def still_waiting(decision_id: int) -> bool:
    """Whether a start that waits on an unread account is inside ``WAIT_SECONDS`` of its first wait."""
    first = _waiting.setdefault(decision_id, time.monotonic())
    if time.monotonic() - first < WAIT_SECONDS:
        return True
    _waiting.pop(decision_id, None)
    return False


def is_waiting(decision_id: int) -> bool:
    return decision_id in _waiting


def forget(decision_id: int) -> None:
    """The start ended or made its worker: it waits no more."""
    _waiting.pop(decision_id, None)
