"""The quiet-PR sweep's durable rows, all in ``settings``, and their end of life.

``pr_quiet:<external_id>`` holds the head, when the sweep first saw it, the
reasons already fired for it, whether it is settled and whether the reviewer
holds it. ``prune_rows`` ends it, and the notice markers, once a PR is no
longer owned and open. Whether a CI event arrived is not kept here: the
webhook delivery log records it. Whether a wake reached the owner's FIFO is
not kept either: ``wake_committed`` reads its message row.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from events.connectors.github_pr_quiet_reasons import parse_time

STATE_PREFIX = "pr_quiet:"


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
    """This PR's durable sweep row, reset when the head moves."""
    from infra.settings import get_setting, set_setting

    state = get_setting(f"{STATE_PREFIX}{external_id}") or {}
    if state.get("head") == head and parse_time(state.get("first_seen_at")):
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
