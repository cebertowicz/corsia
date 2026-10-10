"""An idle marker's timer cancel and invalidation count follow its clear's commit.

Issue #2150: both used to happen inside the still-open transaction, so a
rollback undid the durable clear while the cancelled timer and the emitted
count survived, leaving an armed marker nothing would ever fold.
"""

from __future__ import annotations

import sqlite3

import pytest

from core.checkpoint_types import IdleCheckpointReason
from infra.database import TransactionOwnershipError, write_transaction
from infra.write_effects import after_commit
from tests.test_idle_marker_input_invalidation import (
    _admit,
    _db,
    _idle_thread_with_marker,
    _marker_pending,
)


@pytest.fixture
def side_effects(monkeypatch) -> dict[str, list]:
    import infra.idle_checkpoint_timers as idle_checkpoint_timers
    import infra.product_events as product_events

    seen: dict[str, list] = {"cancelled": [], "emitted": []}
    monkeypatch.setattr(idle_checkpoint_timers, "cancel", seen["cancelled"].append)
    monkeypatch.setattr(
        product_events, "emit_product_event",
        lambda category, action, **kw: seen["emitted"].append((category, action)),
    )
    return seen


def _clear(db: sqlite3.Connection, thread_id: str) -> None:
    from infra.thread_inputs import invalidate_idle_checkpoint_markers

    invalidate_idle_checkpoint_markers(
        db, thread_id, reason=IdleCheckpointReason.SUPERSEDED_BY_TURN,
    )


def test_a_rolled_back_clear_keeps_the_timer_and_the_count(side_effects) -> None:
    db = _db()
    thread_id = _idle_thread_with_marker(db)

    with pytest.raises(RuntimeError, match="later step failed"):
        with write_transaction(db, scope="outer"):
            _clear(db, thread_id)
            raise RuntimeError("later step failed")

    assert _marker_pending(db, thread_id) == 1
    assert side_effects == {"cancelled": [], "emitted": []}


def test_a_rolled_back_savepoint_drops_only_its_own_clear(side_effects) -> None:
    db = _db()
    thread_id = _idle_thread_with_marker(db)

    with write_transaction(db, scope="outer"):
        with pytest.raises(RuntimeError):
            with write_transaction(db, scope="inner"):
                _clear(db, thread_id)
                raise RuntimeError("inner frame failed")
        assert side_effects["cancelled"] == []
        _clear(db, thread_id)
        assert side_effects["cancelled"] == [], "nothing runs before the commit"

    assert _marker_pending(db, thread_id) == 0
    assert side_effects["cancelled"] == [thread_id]
    assert side_effects["emitted"] == [("idle_checkpoint", "invalidated")]


def test_a_committed_clear_cancels_and_counts_once(side_effects) -> None:
    db = _db()
    thread_id = _idle_thread_with_marker(db)

    with write_transaction(db, scope="outer"):
        _clear(db, thread_id)

    assert side_effects["cancelled"] == [thread_id]
    assert side_effects["emitted"] == [("idle_checkpoint", "invalidated")]


def test_a_failed_turn_start_leaves_timer_and_count_untouched(side_effects) -> None:
    """The ``active_turns`` INSERT failing rolls the clear back with no side effect."""
    db = _db()
    thread_id = _idle_thread_with_marker(db)
    db.execute(
        "CREATE TRIGGER reject_new_turn BEFORE INSERT ON active_turns "
        "BEGIN SELECT RAISE(ABORT,'injected acceptance failure'); END"
    )

    with pytest.raises(sqlite3.IntegrityError, match="injected acceptance failure"):
        _admit(db, thread_id, "m-refused")

    assert _marker_pending(db, thread_id) == 1
    assert side_effects == {"cancelled": [], "emitted": []}


def test_archive_cancels_only_after_its_transaction_commits(side_effects) -> None:
    from infra.thread_inputs import invalidate_markers_for_archive

    db = _db()
    thread_id = _idle_thread_with_marker(db)

    with pytest.raises(RuntimeError):
        with write_transaction(db, scope="archive"):
            invalidate_markers_for_archive(db, thread_id)
            raise RuntimeError("archive failed")
    assert side_effects == {"cancelled": [], "emitted": []}

    with write_transaction(db, scope="archive"):
        invalidate_markers_for_archive(db, thread_id)
    assert side_effects["cancelled"] == [thread_id]


def test_after_commit_refuses_a_transaction_no_write_transaction_owns(side_effects) -> None:
    """An implicit or raw transaction never reports its commit: raise, don't lose it."""
    from infra.database import TransactionOwnershipError

    db = _db()
    thread_id = _idle_thread_with_marker(db)

    with pytest.raises(TransactionOwnershipError, match="write_transaction"):
        _clear(db, thread_id)  # its UPDATE opened Python's implicit transaction
    db.rollback()

    db.execute("BEGIN")
    with pytest.raises(TransactionOwnershipError):
        _clear(db, thread_id)
    db.commit()
    assert side_effects == {"cancelled": [], "emitted": []}

    with write_transaction(db, scope="owned"):
        _clear(db, thread_id)
    assert side_effects["cancelled"] == [thread_id]


def test_a_frame_inside_a_foreign_transaction_refuses_the_effect(
    side_effects,
) -> None:
    """Nothing reports the foreign commit, so the effect is refused, never run early."""
    db = _db()
    thread_id = _idle_thread_with_marker(db)

    db.execute("BEGIN")
    with pytest.raises(TransactionOwnershipError):
        with write_transaction(db, scope="frame"):
            _clear(db, thread_id)
    assert side_effects == {"cancelled": [], "emitted": []}
    db.rollback()

    # The refusal leaves no state behind: an owned frame afterwards works.
    with write_transaction(db, scope="owned"):
        _clear(db, thread_id)
    assert side_effects["cancelled"] == [thread_id]


def test_after_commit_over_a_raw_begin_raises_and_nothing_runs_at_release() -> None:
    """A registered effect cannot ride a savepoint release inside a raw BEGIN."""
    db = _db()
    ran: list[str] = []
    db.commit()

    db.execute("BEGIN")
    with pytest.raises(TransactionOwnershipError):
        with write_transaction(db, scope="frame"):
            after_commit(db, lambda: ran.append("effect"))
    assert ran == []

    with pytest.raises(TransactionOwnershipError):
        with write_transaction(db, scope="outer"):
            with write_transaction(db, scope="inner"):
                after_commit(db, lambda: ran.append("effect"))
    assert ran == []
    db.rollback()


def test_idle_checkpoints_no_longer_imports_thread_inputs() -> None:
    """One direction only: the writer (thread_inputs) imports the timer owner."""
    import inspect

    import infra.idle_checkpoints as idle_checkpoints

    assert "thread_inputs" not in inspect.getsource(idle_checkpoints)
