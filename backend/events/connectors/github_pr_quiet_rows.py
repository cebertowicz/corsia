"""The quiet-PR sweep's durable rows, all in ``settings``, and their end of life.

``pr_quiet:<external_id>`` holds the head, when the sweep first saw it, the
reasons already fired for it, whether it is settled and whether the reviewer
holds it. While GitHub answers not found for the PR it also holds how many
such answers came in a row and when to ask next (``record_unfound``).
``prune_rows`` ends it, and the notice markers, once a PR is no longer owned
and open. Whether a CI event arrived is not kept here: the
webhook delivery log records it. Whether a wake reached the owner's FIFO is
not kept either: ``wake_committed`` reads its message row.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from events.connectors.github_pr_quiet_reasons import parse_time

log = logging.getLogger(__name__)

STATE_PREFIX = "pr_quiet:"
#: The two fields a row carries only while GitHub answers not found for its PR.
_UNFOUND_FIELDS = ("unfound", "unfound_until")


def wake_committed(message_id: str) -> bool:
    """Whether the owner's FIFO holds this wake's row, in any delivery state.

    A routed event's message id is its idempotency key. A committed row is the
    wake's one identity: its own settlement reports a failure, and a resend
    under the same key is refused as a failed duplicate.
    """
    from infra.database import get_db

    connection = get_db()
    try:
        return connection.execute(
            "SELECT 1 FROM messages WHERE id=?", (message_id,),
        ).fetchone() is not None
    finally:
        connection.close()


def head_state(external_id: str, head: str, now: datetime) -> dict[str, Any]:
    """This PR's durable sweep row, reset when the head moves.

    Reached only once GitHub has shown the PR, so a not-found wait ends here;
    the reasons already fired for an unchanged head are kept.
    """
    from infra.settings import get_setting, set_setting

    state = get_setting(f"{STATE_PREFIX}{external_id}") or {}
    if state.get("head") == head and parse_time(state.get("first_seen_at")):
        if any(field in state for field in _UNFOUND_FIELDS):
            state = {k: v for k, v in state.items() if k not in _UNFOUND_FIELDS}
            set_setting(f"{STATE_PREFIX}{external_id}", state)
        return state
    state = {"head": head, "first_seen_at": now.isoformat(), "fired": [], "settled": None}
    set_setting(f"{STATE_PREFIX}{external_id}", state)
    return state


def update_head_state(
    external_id: str, head: str, *,
    fired: str | None = None, unfired: str | None = None, settled: dict | None = None,
) -> bool:
    """Change the row if it still names ``head``; False when ``fired`` was already there."""
    from infra.database import write_transaction
    from infra.settings import get_setting, set_setting

    key = f"{STATE_PREFIX}{external_id}"
    with write_transaction(None, scope="pr_quiet_update") as connection:
        state = get_setting(key, db=connection) or {}
        if state.get("head") != head:
            return False
        recorded = list(state.get("fired") or [])
        if fired is not None:
            if fired in recorded:
                return False
            recorded.append(fired)
        if unfired is not None and unfired in recorded:
            recorded.remove(unfired)
        state["fired"] = recorded
        if settled is not None:
            state["settled"] = settled
        set_setting(key, state, db=connection)
        return True


def record_review_hold(
    external_id: str, head: str, held: bool, now: datetime,
) -> dict[str, Any]:
    """Note the reviewer holding or releasing this head; a release restarts its clock.

    The reviewer holds drafts and conflicting PRs and starts once one becomes
    reviewable, so ``review_missing`` counts from then, not from CI finishing.
    """
    from infra.database import write_transaction
    from infra.settings import get_setting, set_setting

    key = f"{STATE_PREFIX}{external_id}"
    with write_transaction(None, scope="pr_quiet_review_hold") as connection:
        state = get_setting(key, db=connection) or {}
        if state.get("head") != head:
            return state
        state["review_hold"] = held
        state["review_from"] = None if held else now.isoformat()
        set_setting(key, state, db=connection)
        return state


def unfound_waiting(external_id: str, now: datetime) -> bool:
    """Whether GitHub last answered not found for this PR and it is not yet time to ask again."""
    from infra.settings import get_setting

    until = parse_time((get_setting(f"{STATE_PREFIX}{external_id}") or {}).get("unfound_until"))
    return until is not None and now < until


def record_unfound(external_id: str, now: datetime, sweep_interval_s: float) -> datetime:
    """Note that GitHub answered not found for this PR; return when to ask again.

    The wait is the sweep interval doubled once per not-found answer in a row.
    The first wait is therefore two intervals, the shortest that makes the next
    sweep skip the PR. There is no cap: doubling alone keeps the reads
    logarithmic in how long the PR has been gone, and a PR that comes back is
    read within about as long again as it was gone, so no chosen limit is
    needed. The rest of the row is kept, so a PR that comes back on the same
    head is not woken again for a reason it already fired.
    """
    from infra.database import write_transaction
    from infra.settings import get_setting, set_setting

    key = f"{STATE_PREFIX}{external_id}"
    with write_transaction(None, scope="pr_quiet_unfound") as connection:
        state = get_setting(key, db=connection) or {}
        answers = state.get("unfound")
        answers = answers + 1 if isinstance(answers, int) and answers > 0 else 1
        until = now + timedelta(seconds=sweep_interval_s) * 2 ** answers
        state.update({"unfound": answers, "unfound_until": until.isoformat()})
        set_setting(key, state, db=connection)
    log.info(
        "pr-quiet-watch: %s not found on GitHub; next read at %s", external_id, until.isoformat(),
    )
    return until


def forget(external_id: str) -> None:
    """Drop an ended PR's sweep row (design §5)."""
    from infra.database import write_transaction

    with write_transaction(None, scope="pr_quiet_forget") as connection:
        connection.execute(
            "DELETE FROM settings WHERE key=?", (f"{STATE_PREFIX}{external_id}",),
        )


def prune_rows() -> int:
    """Drop sweep and notice rows of PRs no longer owned and open.

    ``_candidates`` leaves out a PR whose merge or close was recorded, so this,
    not ``forget``, ends those rows.
    """
    from events.connectors.github_pr_notice import NOTICE_PREFIX, OWNED_PR_SQL
    from infra.database import write_transaction

    prefixes = (STATE_PREFIX, NOTICE_PREFIX)
    with write_transaction(None, scope="pr_quiet_prune") as connection:
        owned = {
            (row["parent_thread_id"], row["external_id"])
            for row in connection.execute(OWNED_PR_SQL.format(thread_filter="")).fetchall()
        }
        open_ids = {external_id for _, external_id in owned}
        stale = []
        for (key,) in connection.execute(
            "SELECT key FROM settings WHERE substr(key, 1, 3) = 'pr_'",
        ).fetchall():
            if not key.startswith(prefixes):
                continue
            rest = key.partition(":")[2]
            thread_id, _, external_id = rest.partition(":")
            live = ((thread_id, external_id) in owned if key.startswith(NOTICE_PREFIX)
                    else rest in open_ids)
            if not live:
                stale.append((key,))
        connection.executemany("DELETE FROM settings WHERE key=?", stale)
    return len(stale)
