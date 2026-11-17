"""``read_newest_cards`` returns exactly the cards its per-subquery form returned."""

from __future__ import annotations

import pytest

from infra import thread_updates
from infra.person_step_state import person_step_state
from infra.thread_update_asks import ask_sql, asks_user_sql
from tests.presence_helpers import pdb
from tests.thread_update_card_seed import seed_cards

__all__ = ["pdb"]


def _reference_cards(db, thread_id: str, *, min_seq: int) -> tuple[list[dict], bool]:
    """The read as it was written before the node was joined once: one correlated subquery per column."""
    limit = thread_updates.CARD_WINDOW_LIMIT
    rows = db.execute(
        f"""SELECT {thread_updates._COLUMNS}, EXISTS (
                SELECT 1 FROM plan_nodes n
                 WHERE n.id = thread_updates.node_id AND n.archived_at IS NULL
             ) AS node_exists, {asks_user_sql()} AS asks_user,
             {asks_user_sql(blocked=True)} AS blocked_asks_user,
             {ask_sql('id')} AS ask_hold_id, {ask_sql('owner_id')} AS ask_artifact_id,
             (SELECT n.state FROM plan_nodes n WHERE n.id=thread_updates.node_id) AS step_node_state,
             (SELECT n.archived_at FROM plan_nodes n WHERE n.id=thread_updates.node_id) AS step_archived_at,
             (SELECT u.kind FROM thread_updates u WHERE u.node_id=thread_updates.node_id
                AND u.kind IN ('done', 'done_follow_up', 'closed', 'failed')
                ORDER BY u.id DESC LIMIT 1) AS step_outcome
             FROM thread_updates
             WHERE thread_id=? AND after_seq >= ? AND kind != 'marked'
             ORDER BY after_seq DESC, id DESC LIMIT ?""",
        (thread_id, min_seq, limit + 1),
    ).fetchall()
    cards = [
        {
            **thread_updates._public(tuple(row)[:-8]),
            **({"step_state": person_step_state(row)} if row["kind"] == "person_step" else {}),
            "node_exists": bool(row["node_exists"]),
            "asks_user": bool(row["asks_user"]),
            "blocked_asks_user": bool(row["blocked_asks_user"]),
            "ask": (
                {"hold_id": row["ask_hold_id"], "artifact_id": row["ask_artifact_id"]}
                if row["ask_hold_id"] else None
            ),
        }
        for row in reversed(rows[:limit])
    ]
    return cards, len(rows) > limit


@pytest.mark.parametrize("min_seq", [0, 200, 350])
def test_cards_match_the_per_subquery_read_on_a_busy_thread(pdb, min_seq):
    seed_cards(pdb, nodes=600, cards=1_200)

    cards, has_more = thread_updates.read_newest_cards(pdb, "focus", min_seq=min_seq)

    assert (cards, has_more) == _reference_cards(pdb, "focus", min_seq=min_seq)
    assert has_more is (min_seq == 0)
    # The seed exercises every derived field, so the comparison is not vacuous.
    assert {card["kind"] for card in cards} == set(thread_updates.PLAN_UPDATE_KINDS)
    assert any(card["asks_user"] for card in cards)
    assert any(card["blocked_asks_user"] for card in cards)
    assert any(card["ask"] for card in cards)
    assert any(not card["node_exists"] for card in cards)
    assert {card["step_state"] for card in cards if card["kind"] == "person_step"} == {"open", "done", "closed"}
