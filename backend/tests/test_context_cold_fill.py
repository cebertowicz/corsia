"""The authorized truncation: it fits the window, and it says what it dropped.

``destination_fit_blocked`` used to stop a thread outright because the user
picked a model whose window could not hold it. The replacement is a cold fill
with a declared truncation — so these tests check both halves: the ladder
really produces smaller replays, and each rung carries its own notice.
"""

from __future__ import annotations

from core.context_cold_fill import cold_fill_candidates, dropped_turn_count
from core.context_replay import ReplaySelection, history_loss_notice
from history.types import (
    PORTABLE_HISTORY_FORMAT_VERSION,
    HistoryBoundary,
    PortableHistoryProjection,
    PortableMessage,
    PortableTurn,
)

THREAD = "cold-fill-thread"


def _turn(index: int) -> PortableTurn:
    user_seq = index * 2 - 1
    assistant_seq = index * 2
    boundary = HistoryBoundary(THREAD, 1, assistant_seq, f"message-{assistant_seq}")
    return PortableTurn(
        turn_id=f"turn-{index}",
        status="completed",
        messages=(
            PortableMessage(
                message_id=f"message-{user_seq}",
                seq=user_seq,
                turn_id=f"turn-{index}",
                role="user",
                content=f"ask {index}",
                created_at=f"2026-08-08T00:00:{user_seq:02d}Z",
            ),
            PortableMessage(
                message_id=f"message-{assistant_seq}",
                seq=assistant_seq,
                turn_id=f"turn-{index}",
                role="assistant",
                content=f"answer {index}",
                created_at=f"2026-08-08T00:00:{assistant_seq:02d}Z",
            ),
        ),
        boundary=boundary,
    )


def _projection(turns: int) -> PortableHistoryProjection:
    built = tuple(_turn(index) for index in range(1, turns + 1))
    return PortableHistoryProjection(
        format_version=PORTABLE_HISTORY_FORMAT_VERSION,
        thread_id=THREAD,
        history_epoch=1,
        start_exclusive=None,
        sealed_boundary=built[-1].boundary,
        turns=built,
        full_raw_replay_available=True,
    )


def _selection(turns: int) -> ReplaySelection:
    projection = _projection(turns)
    return ReplaySelection(
        mode="exact",
        history_epoch=1,
        sealed_boundary=projection.sealed_boundary,
        tail=projection,
    )


def test_the_ladder_drops_whole_turns_from_the_oldest_end():
    """Largest first, whole turns only, and the newest turn is never dropped."""
    selection = _selection(4)
    ladder = cold_fill_candidates(selection)

    assert len(ladder) == 4
    assert ladder[0] is selection
    kept = [len(candidate.tail.turns) for candidate in ladder]
    assert kept == [4, 3, 2, 1], "each rung must be strictly smaller"
    for candidate in ladder:
        # Never a partial turn: an assistant step whose tool results are gone is
        # worse than not sending it at all.
        assert all(len(turn.messages) == 2 for turn in candidate.tail.turns)
    newest = selection.tail.turns[-1].turn_id
    assert all(
        candidate.tail.turns[-1].turn_id == newest for candidate in ladder
    ), "a replay with no recent history is a different conversation"


def test_a_single_turn_thread_has_no_rung_below_itself():
    """Nothing to drop means the honest answer stays 'this cannot fit'."""
    assert len(cold_fill_candidates(_selection(1))) == 1


def test_every_truncated_rung_declares_its_own_gap():
    """The notice is not a separate step someone could forget to run."""
    selection = _selection(4)
    ladder = cold_fill_candidates(selection)

    assert history_loss_notice(ladder[0]) == ""
    for candidate in ladder[1:]:
        notice = history_loss_notice(candidate)
        assert "[CORSIA incomplete history]" in notice
        assert "DROPPED" in notice
        assert "search this thread's history" in notice
        assert dropped_turn_count(selection.tail, candidate.tail) > 0
