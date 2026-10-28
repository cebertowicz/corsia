"""Schema v25 frees queued inputs whose promised release never came.

Two stranded shapes are covered, each followed by the one startup recovery
pass that runs after every migration, because freeing a row only matters if
the thread then answers it without anyone touching it (invariant 3.8).
"""

from __future__ import annotations

import sqlite3

import pytest

STRANDED_THREAD = "stranded-thread"
HELD_THREAD = "held-thread"


def _thread(db, thread_id, model, *, epoch=0, switch=None):
    pending_model, pending_epoch, status = switch or (None, None, None)
    db.execute(
        "INSERT INTO threads "
        "(id,user_id,model,execution_epoch,pending_model,pending_execution_epoch,"
        "switch_status,switch_operation_id,switch_after_input_order,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,'t0','t0')",
        (
            thread_id, "user-1", model, epoch, pending_model, pending_epoch, status,
            f"switch-{thread_id}" if status else None, 0 if status else None,
        ),
    )


def _input(db, message_id, thread_id, order, model, epoch, **fields):
    columns = {
        "id": message_id, "thread_id": thread_id, "role": "user",
        "content": message_id, "created_at": f"t{order}", "delivery_state": "queued",
        "input_order": order, "target_execution_epoch": epoch, "target_model": model,
        **fields,
    }
    names = ",".join(columns)
    marks = ",".join("?" for _ in columns)
    db.execute(f"INSERT INTO messages ({names}) VALUES ({marks})", tuple(columns.values()))


def _hold(**extra):
    return {
        "write_outcome": "proved_no_write", "delivery_attempt_id": "old-attempt",
        "delivery_error": "minimum meaningful cold fork input does not fit",
        **extra,
    }


@pytest.fixture
def schema_twentyfour():
    from infra.schema import SCHEMA_SQL

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row  # The startup connection's row factory.
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    db.execute("PRAGMA user_version=24")
    for model in ("local/held", "native/destination", "local/other"):
        db.execute(
            "INSERT INTO models (id,provider,provider_model_id,litellm_model,"
            "display_name,context_window,created_at,updated_at) "
            "VALUES (?,'local',?,?,?,100000,'t0','t0')",
            (model, model, model, model),
        )
    try:
        yield db
    finally:
        db.close()


def _migrate(db):
    from infra.database_migrations import migrate_to_current

    return migrate_to_current(db, 25)


def _recover(db):
    from infra.thread_recovery import recover_threads

    return recover_threads(
        boot_epoch="new-boot", recovered_at="t9",
        successor_id=lambda thread_id: f"recovered-{thread_id}", db=db,
    )


def _claimed(recovery, thread_id):
    return [
        [item.message_id for item in batch.items]
        for batch in recovery.batches if batch.thread_id == thread_id
    ]


def _row(db, message_id):
    return dict(db.execute(
        "SELECT target_model,target_execution_epoch,write_outcome,delivery_attempt_id,"
        "delivery_error,delivery_revision FROM messages WHERE id=?", (message_id,),
    ).fetchone())


def test_a_failed_switch_suffix_returns_and_startup_answers_it(schema_twentyfour):
    db = schema_twentyfour
    _thread(db, STRANDED_THREAD, "local/held",
            switch=("native/destination", 1, "failed"))
    _input(db, "stranded-1", STRANDED_THREAD, 1, "native/destination", 1)
    _input(db, "stranded-2", STRANDED_THREAD, 2, "native/destination", 1)
    db.commit()

    result = _migrate(db)

    assert (result.migrated, result.from_version, result.to_version) == (True, 24, 25)
    assert _row(db, "stranded-1")["target_model"] == "local/held"
    assert _row(db, "stranded-2")["target_execution_epoch"] == 0
    assert _claimed(_recover(db), STRANDED_THREAD) == [["stranded-1", "stranded-2"]]


def test_an_uncertain_row_stays_and_nothing_overtakes_it(schema_twentyfour):
    db = schema_twentyfour
    _thread(db, STRANDED_THREAD, "local/held",
            switch=("native/destination", 1, "failed"))
    _input(db, "stranded-1", STRANDED_THREAD, 1, "native/destination", 1)
    _input(db, "uncertain-1", STRANDED_THREAD, 2, "native/destination", 1,
           write_outcome="ambiguous", active_turn_id=None, write_started_at="t2")
    _input(db, "behind-1", STRANDED_THREAD, 3, "native/destination", 1)
    db.commit()

    _migrate(db)

    assert _row(db, "stranded-1")["target_model"] == "local/held"
    assert _row(db, "uncertain-1")["target_model"] == "native/destination"
    assert _row(db, "behind-1")["target_model"] == "native/destination"


def test_an_unreachable_hold_is_released_and_startup_answers_it(schema_twentyfour):
    db = schema_twentyfour
    _thread(db, HELD_THREAD, "local/held")
    _input(db, "held-1", HELD_THREAD, 1, "local/held", 0, **_hold())
    _input(db, "held-2", HELD_THREAD, 2, "local/held", 0, **_hold())
    _input(db, "behind-hold", HELD_THREAD, 3, "local/held", 0)
    db.commit()
    assert _claimed(_recover(db), HELD_THREAD) == []  # The wedge, before the step.
    db.execute("DELETE FROM active_turns")
    db.commit()

    _migrate(db)

    for message_id in ("held-1", "held-2"):
        released = _row(db, message_id)
        assert released["write_outcome"] is None
        assert released["delivery_attempt_id"] is None
        assert released["delivery_error"] is None
        assert released["delivery_revision"] == 2
    assert _claimed(_recover(db), HELD_THREAD) == [["held-1", "held-2", "behind-hold"]]


def test_a_released_passive_digest_rides_with_the_next_real_input(schema_twentyfour):
    """Passive context never starts a Turn, but once released it stops blocking."""
    import json

    from infra.thread_inputs import start_idle_turn

    db = schema_twentyfour
    _thread(db, HELD_THREAD, "local/held")
    _input(
        db, "held-digest", HELD_THREAD, 1, "local/held", 0, role="system",
        metadata=json.dumps({"type": "context_event", "event_disposition": "passive"}),
        **_hold(),
    )
    db.commit()

    _migrate(db)

    assert _claimed(_recover(db), HELD_THREAD) == []
    _input(db, "real-ask", HELD_THREAD, 2, "local/held", 0)
    db.commit()
    batch = start_idle_turn(
        HELD_THREAD, turn_id="next-turn", boot_epoch="new-boot", started_at="t10", db=db,
    )
    assert [item.message_id for item in batch.items] == ["held-digest", "real-ask"]


def test_a_preparing_switch_keeps_the_hold_it_will_transfer(schema_twentyfour):
    db = schema_twentyfour
    _thread(db, HELD_THREAD, "local/held",
            switch=("native/destination", 1, "preparing"))
    _input(db, "held-1", HELD_THREAD, 1, "local/held", 0, **_hold())
    db.commit()
    before = _row(db, "held-1")

    _migrate(db)

    assert _row(db, "held-1") == before


def test_only_the_existing_release_predicate_decides(schema_twentyfour):
    """A hold the install path would not release is not released here either."""
    db = schema_twentyfour
    _thread(db, HELD_THREAD, "local/held", epoch=1)
    _input(db, "stale-epoch", HELD_THREAD, 1, "local/held", 0, **_hold())
    _input(db, "other-model", HELD_THREAD, 2, "local/other", 1, **_hold())
    _input(db, "in-flight", HELD_THREAD, 3, "local/held", 1,
           **_hold(write_started_at="t3"))
    db.commit()
    before = {mid: _row(db, mid) for mid in ("stale-epoch", "other-model", "in-flight")}

    _migrate(db)

    assert {mid: _row(db, mid) for mid in before} == before


def test_the_step_runs_once(schema_twentyfour):
    db = schema_twentyfour
    _thread(db, HELD_THREAD, "local/held")
    _input(db, "held-1", HELD_THREAD, 1, "local/held", 0, **_hold())
    db.commit()
    _migrate(db)

    from infra.schema import SCHEMA_VERSION

    repeated = _migrate(db)

    assert SCHEMA_VERSION == 155
    assert (repeated.migrated, repeated.from_version, repeated.to_version) == (False, 25, 25)
    assert _row(db, "held-1")["delivery_revision"] == 2
