"""Per-thread idle checkpoint timers: warm timing, exclusions, cancel, restart."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta

import pytest

CC_MODEL = "claude-code/test"
# A native External API model (provider 'external') and a native local oMLX
# model: both are native-harness, but only the local transport routes to the
# shared oMLX engine, so only it stays excluded (#2283).
EXTERNAL_MODEL = "external@test"
LOCAL_MODEL = "omlx@test"
PROVIDER_CACHE_TTL = timedelta(minutes=5)
# The longest last-call generation the ended_at anchor is designed to absorb.
UNRECORDED_GENERATION = timedelta(seconds=45)


def _db() -> sqlite3.Connection:
    from infra.schema import SCHEMA_SQL

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    for model_id, harness, provider in (
        (CC_MODEL, "claude-code", "anthropic"),
        (EXTERNAL_MODEL, "native", "external"),
        (LOCAL_MODEL, "native", "omlx"),
    ):
        db.execute(
            "INSERT INTO models (id,harness,provider,provider_model_id,litellm_model,"
            "display_name,context_window,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,200000,'t0','t0')",
            (model_id, harness, provider, model_id, model_id, model_id),
        )
    # A Claude Code input starts a Turn only on a signed-in account that lists its model.
    from connections.models import upsert_connection
    from vendor_account_fixtures import signed_in_default

    upsert_connection(type="cli", provider="claude-code", status="available",
                      config={"binary": "/bin/synthetic"}, db=db)
    signed_in_default("claude-code", db=db)
    return db


def _thread(
    db: sqlite3.Connection,
    thread_id: str,
    *,
    model: str = CC_MODEL,
    spawned_by: str | None = None,
    parent_thread_id: str | None = None,
    lifecycle_state: str = "active",
) -> None:
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at,spawned_by,"
        "parent_thread_id,lifecycle_state) VALUES (?,?,?,'t0','t0',?,?,?)",
        (thread_id, "user", model, spawned_by, parent_thread_id, lifecycle_state),
    )
    db.commit()


def _turn(
    db: sqlite3.Connection,
    thread_id: str,
    turn_id: str,
    *,
    status: str = "running",
    ended_at: str | None = None,
    pending: int = 0,
    model: str = CC_MODEL,
) -> None:
    db.execute(
        "INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,"
        "started_at,ended_at,boot_epoch,idle_checkpoint_pending) "
        "VALUES (?,?,0,?,?,'t0',?,'boot',?)",
        (turn_id, thread_id, model, status, ended_at, pending),
    )
    db.commit()


def _pending(db: sqlite3.Connection, turn_id: str) -> int:
    return db.execute(
        "SELECT idle_checkpoint_pending FROM active_turns WHERE id=?", (turn_id,)
    ).fetchone()[0]


def _finish(
    db: sqlite3.Connection,
    turn_id: str,
    ended_at: str = "2026-01-01T00:00:00",
    *,
    mark_idle_checkpoint: bool = True,
):
    from infra.thread_driver import finish_turn

    return finish_turn(
        turn_id,
        outcome="completed",
        ended_at=ended_at,
        successor_turn_id=f"{turn_id}-next",
        boot_epoch="boot",
        db=db,
        mark_idle_checkpoint=mark_idle_checkpoint,
    )


def _due(thread_id: str = "thread-1", ended_at: str = "2026-01-01T00:00:00"):
    from infra.idle_checkpoints import IdleCheckpointDue

    return IdleCheckpointDue(f"{thread_id}-turn", 1, thread_id, ended_at)


def test_fold_starts_within_the_cache_ttl_of_the_last_request_start() -> None:
    ended_at = datetime(2026, 1, 1, 12, 0, 0)
    due = _due(ended_at=ended_at.isoformat())
    last_request_start = ended_at - UNRECORDED_GENERATION

    assert due.fires_at() - last_request_start <= timedelta(minutes=4, seconds=45)
    assert due.fires_at() - last_request_start < PROVIDER_CACHE_TTL


def test_timezone_aware_end_is_normalized_to_storage_utc() -> None:
    assert _due(ended_at="2026-01-01T12:00:00+00:00").fires_at() == datetime(
        2026, 1, 1, 12, 4, 0
    )


@pytest.mark.asyncio
async def test_arm_schedules_the_fold_from_now_not_after_a_sweep(monkeypatch) -> None:
    import infra.idle_checkpoint_timers as idle_checkpoint_timers
    from thread import idle_checkpoint

    armed: list[tuple] = []
    now = datetime(2026, 1, 1, 12, 1, 0)
    monkeypatch.setattr("infra.time.utc_now", lambda: now)
    monkeypatch.setattr(
        idle_checkpoint_timers, "arm", lambda due, delay, fire: armed.append((due, delay, fire))
    )

    idle_checkpoint.arm_idle_checkpoint(_due(ended_at="2026-01-01T12:00:00"))

    assert armed[0][1] == 180.0
    assert armed[0][2] is idle_checkpoint.fire_idle_checkpoint


@pytest.mark.asyncio
async def test_armed_timer_fires_once_and_releases_itself() -> None:
    import infra.idle_checkpoint_timers as idle_checkpoint_timers

    fired = []

    async def fire(due) -> None:
        fired.append(due.turn_id)

    idle_checkpoint_timers.arm(_due(), 0.01, fire)
    assert set(idle_checkpoint_timers.pending()) == {"thread-1"}
    await asyncio.sleep(0.05)

    assert fired == ["thread-1-turn"]
    assert idle_checkpoint_timers.pending() == {}


@pytest.mark.asyncio
async def test_rearming_a_thread_replaces_its_previous_timer() -> None:
    import infra.idle_checkpoint_timers as idle_checkpoint_timers

    fired = []

    async def fire(due) -> None:
        fired.append(due.turn_id)

    idle_checkpoint_timers.arm(_due(), 0.02, fire)
    newer = _due()
    newer = type(newer)("newer-turn", 2, "thread-1", newer.ended_at)
    idle_checkpoint_timers.arm(newer, 0.02, fire)
    await asyncio.sleep(0.08)

    assert fired == ["newer-turn"]


@pytest.mark.asyncio
async def test_concurrent_fires_are_capped_at_the_global_limit() -> None:
    """A burst of simultaneously-ready timers cannot exceed the fold ceiling."""
    import infra.idle_checkpoint_timers as idle_checkpoint_timers

    limit = idle_checkpoint_timers.IDLE_CONCURRENT_LIMIT
    running = 0
    peak = 0
    release = asyncio.Event()

    async def fire(_due) -> None:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await release.wait()
        running -= 1

    for index in range(limit + 3):
        idle_checkpoint_timers.arm(_due(f"thread-{index}"), 0.0, fire)
    await asyncio.sleep(0.05)

    assert peak == limit

    release.set()
    await asyncio.sleep(0.05)


@pytest.mark.parametrize(
    ("thread_kwargs", "model", "reason"),
    (
        ({"model": LOCAL_MODEL}, LOCAL_MODEL, "local_shared_model"),
        ({"spawned_by": "parent"}, CC_MODEL, "child"),
        ({"parent_thread_id": "parent"}, CC_MODEL, "child"),
        ({"lifecycle_state": "archived"}, CC_MODEL, "archived"),
        ({"model": "unknown/model"}, "unknown/model", "unresolvable"),
    ),
    ids=("local-shared-model", "spawned-child", "parented-child", "archived", "unresolvable"),
)
def test_excluded_threads_never_receive_an_idle_marker(
    thread_kwargs, model, reason, monkeypatch,
) -> None:
    """Every no-fold exclusion is counted, not just the timed-out ones."""
    import infra.product_events as product_events

    emitted: list[tuple[str, str, str | None, dict | None]] = []
    monkeypatch.setattr(
        product_events, "emit_product_event",
        lambda category, action, *, thread_id=None, user_id=None, properties=None:
            emitted.append((category, action, thread_id, properties)),
    )

    db = _db()
    try:
        _thread(db, "parent")
        _thread(db, "thread-1", **thread_kwargs)
        _turn(db, "thread-1", "turn-1", model=model)

        settlement = _finish(db, "turn-1")

        assert _pending(db, "turn-1") == 0
        assert settlement.idle_checkpoint is None
        assert emitted == [
            ("idle_checkpoint", "excluded", "thread-1", {"reason": reason}),
        ]
    finally:
        db.close()


def test_root_harness_thread_marker_is_returned_for_arming() -> None:
    db = _db()
    try:
        _thread(db, "thread-1")
        _turn(db, "thread-1", "turn-1")

        settlement = _finish(db, "turn-1", ended_at="2026-01-01T00:00:07")

        assert _pending(db, "turn-1") == 1
        due = settlement.idle_checkpoint
        assert (due.turn_id, due.thread_id, due.ended_at) == (
            "turn-1", "thread-1", "2026-01-01T00:00:07",
        )
    finally:
        db.close()


@pytest.mark.parametrize("model", (CC_MODEL, EXTERNAL_MODEL))
def test_root_native_and_harness_threads_get_idle_markers(model: str) -> None:
    """A top-level External-native thread is armed like a harness thread.

    The blanket native exclusion is gone (#2283): only the shared local
    engine stays excluded, so an External API model gets the marker.
    """
    db = _db()
    try:
        _thread(db, "thread-1", model=model)
        _turn(db, "thread-1", "turn-1", model=model)

        settlement = _finish(db, "turn-1")

        assert _pending(db, "turn-1") == 1
        assert settlement.idle_checkpoint is not None
        assert settlement.idle_checkpoint.thread_id == "thread-1"
    finally:
        db.close()


def test_settlement_no_driver_will_arm_writes_no_marker_at_all() -> None:
    """A caller that discards its settlement leaves no dead marker behind."""
    db = _db()
    try:
        _thread(db, "thread-1")
        _turn(db, "thread-1", "turn-1")

        settlement = _finish(db, "turn-1", mark_idle_checkpoint=False)

        assert _pending(db, "turn-1") == 0
        assert settlement.idle_checkpoint is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_terminal_driver_arms_the_committed_marker(monkeypatch) -> None:
    from infra.thread_driver import TerminalResult, TerminalSettlement
    from infra.thread_execution import drive_terminal_settlement

    armed: list[object] = []
    monkeypatch.setattr("thread.idle_checkpoint.arm_idle_checkpoint", armed.append)
    due = _due()

    await drive_terminal_settlement(
        TerminalSettlement(TerminalResult.APPLIED, "thread-1", idle_checkpoint=due)
    )
    await drive_terminal_settlement(TerminalSettlement(TerminalResult.APPLIED, "thread-1"))

    assert armed == [due]


@pytest.mark.asyncio
async def test_new_input_cancels_the_timer_and_the_marker() -> None:
    import infra.idle_checkpoint_timers as idle_checkpoint_timers
    from infra.thread_driver import admit_input

    fired = []

    async def fire(due) -> None:
        fired.append(due)

    db = _db()
    try:
        _thread(db, "thread-1")
        _turn(db, "thread-1", "turn-1", status="completed", ended_at="t1", pending=1)
        idle_checkpoint_timers.arm(_due(), 0.02, fire)

        admission = admit_input(
            "thread-1", role="user", content="next", intent="apply_now",
            message_id="m-next", turn_id="turn-2", boot_epoch="boot",
            admitted_at="t2", db=db,
        )
        assert admission.batch is not None, "the input should start a Turn"
        await asyncio.sleep(0.06)

        assert _pending(db, "turn-1") == 0
        assert idle_checkpoint_timers.pending() == {}
        assert fired == []
    finally:
        db.close()


@pytest.mark.asyncio
async def test_worker_thread_cancellation_reaches_the_loop_timer() -> None:
    import infra.idle_checkpoint_timers as idle_checkpoint_timers

    fired = []

    async def fire(due) -> None:
        fired.append(due)

    idle_checkpoint_timers.arm(_due(), 0.05, fire)
    await asyncio.to_thread(idle_checkpoint_timers.cancel, "thread-1")
    await asyncio.sleep(0.1)

    assert fired == []


def test_cancel_all_drops_a_foreign_closed_loop_timer_untouched() -> None:
    """A timer armed under an already-closed loop must not reach gather.

    Regression for the L3 "Event loop is closed" failure: an earlier test's
    loop can leave an armed timer behind in this process-global registry;
    ``cancel_all`` must return only the current loop's own tasks so shutdown's
    ``asyncio.gather`` never touches a foreign, closed loop.
    """
    import infra.idle_checkpoint_timers as idle_checkpoint_timers

    async def fire(_due) -> None:
        pass

    async def arm_under_a_loop_that_will_close() -> None:
        idle_checkpoint_timers.arm(_due("thread-old"), 100.0, fire)

    asyncio.run(arm_under_a_loop_that_will_close())
    assert idle_checkpoint_timers.pending() == {"thread-old": _due("thread-old")}

    async def cancel_from_a_second_loop() -> tuple[int, list[object]]:
        idle_checkpoint_timers.arm(_due("thread-new"), 100.0, fire)
        tasks = idle_checkpoint_timers.cancel_all()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return len(tasks), results

    count, results = asyncio.run(cancel_from_a_second_loop())

    assert count == 1
    assert isinstance(results[0], asyncio.CancelledError)
    assert idle_checkpoint_timers.pending() == {}


@pytest.mark.asyncio
async def test_user_archive_clears_the_marker_and_cancels_the_timer() -> None:
    from core.thread_lifecycle import set_user_archive_state
    from infra.database import write_transaction
    import infra.idle_checkpoint_timers as idle_checkpoint_timers

    async def fire(_due) -> None:
        raise AssertionError("an archived thread must not fold")

    db = _db()
    try:
        _thread(db, "thread-1")
        _turn(db, "thread-1", "turn-1", status="completed", ended_at="t1", pending=1)
        idle_checkpoint_timers.arm(_due(), 0.02, fire)

        with write_transaction(db, scope="archive"):
            assert set_user_archive_state(db, "thread-1", archived=True)
        await asyncio.sleep(0.06)

        assert _pending(db, "turn-1") == 0
        assert idle_checkpoint_timers.pending() == {}
    finally:
        db.close()


def test_restart_keeps_warm_markers_and_clears_expired_ones_unfolded() -> None:
    from infra.idle_checkpoints import recover_idle_markers

    now = datetime(2026, 1, 1, 12, 0, 0)
    db = _db()
    try:
        _thread(db, "warm")
        _thread(db, "cold")
        _thread(db, "external-native", model=EXTERNAL_MODEL)
        _thread(db, "local-native", model=LOCAL_MODEL)
        _thread(db, "archived", lifecycle_state="archived")
        _turn(db, "warm", "warm-turn", status="completed", pending=1,
              ended_at=(now - timedelta(seconds=100)).isoformat())
        _turn(db, "cold", "cold-turn", status="completed", pending=1,
              ended_at=(now - timedelta(seconds=240)).isoformat())
        _turn(db, "external-native", "external-native-turn", status="completed",
              pending=1, ended_at=(now - timedelta(seconds=10)).isoformat(),
              model=EXTERNAL_MODEL)
        _turn(db, "local-native", "local-native-turn", status="completed", pending=1,
              ended_at=(now - timedelta(seconds=10)).isoformat(), model=LOCAL_MODEL)
        _turn(db, "archived", "archived-turn", status="completed", pending=1,
              ended_at=(now - timedelta(seconds=10)).isoformat())

        live = recover_idle_markers(now=now, db=db)

        assert [due.turn_id for due in live] == ["warm-turn", "external-native-turn"]
        assert _pending(db, "warm-turn") == 1
        assert _pending(db, "external-native-turn") == 1
        for turn_id in ("cold-turn", "local-native-turn", "archived-turn"):
            assert _pending(db, turn_id) == 0, turn_id
        assert db.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 0
    finally:
        db.close()


def test_recovery_reads_at_most_one_bounded_page_oldest_first(monkeypatch) -> None:
    """Boot work stays bounded no matter how many markers are pending.

    The marker backlog on the first boot after this change is unbounded --
    every marker written under the old rules is still pending -- and each
    candidate costs a threads lookup plus a full model resolution, so the
    page size is what keeps boot from scaling with the backlog.
    """
    from infra.idle_checkpoints import IDLE_RECOVER_LIMIT, recover_idle_markers

    now = datetime(2026, 1, 1, 12, 0, 0)
    total = IDLE_RECOVER_LIMIT + 5
    db = _db()
    resolved: list[str] = []
    try:
        for index in range(total):
            thread_id = f"thread-{index:03d}"
            _thread(db, thread_id)
            # Oldest first: index 0 ended longest ago, all still inside the
            # warm window so none of them is cleared as expired.
            _turn(db, thread_id, f"{thread_id}-turn", status="completed", pending=1,
                  ended_at=(now - timedelta(seconds=120 - index)).isoformat())

        import infra.idle_checkpoints as idle_checkpoints

        original = idle_checkpoints.idle_checkpoint_eligible

        def counting(connection, thread_id, model):
            resolved.append(thread_id)
            return original(connection, thread_id, model)

        monkeypatch.setattr(idle_checkpoints, "idle_checkpoint_eligible", counting)

        live = recover_idle_markers(now=now, db=db)

        assert len(live) == IDLE_RECOVER_LIMIT
        assert [due.turn_id for due in live] == [
            f"thread-{index:03d}-turn" for index in range(IDLE_RECOVER_LIMIT)
        ]
        # Per-row model resolution is paid only for the bounded page.
        assert len(resolved) == IDLE_RECOVER_LIMIT
        # Everything past the bound is left pending for a later boot, not lost.
        for index in range(IDLE_RECOVER_LIMIT, total):
            assert _pending(db, f"thread-{index:03d}-turn") == 1
    finally:
        db.close()


def test_recovery_finds_a_fresh_marker_behind_a_large_expired_backlog() -> None:
    """A pile of ancient markers must not starve a still-warm one out.

    Expiring an ancient marker needs no model resolution, so that clearing
    is unbounded and happens in one pass regardless of backlog size; only
    the bounded page of still-possibly-warm candidates pays resolution
    cost. A naive oldest-first LIMIT page would fill up entirely with
    ancient junk and never even look at a marker that ended moments ago.
    """
    from infra.idle_checkpoints import IDLE_RECOVER_LIMIT, recover_idle_markers

    now = datetime(2026, 1, 1, 12, 0, 0)
    ancient_count = IDLE_RECOVER_LIMIT + 5
    db = _db()
    try:
        for index in range(ancient_count):
            thread_id = f"ancient-{index:03d}"
            _thread(db, thread_id)
            _turn(db, thread_id, f"{thread_id}-turn", status="completed", pending=1,
                  ended_at=(now - timedelta(hours=1)).isoformat())
        _thread(db, "fresh")
        _turn(db, "fresh", "fresh-turn", status="completed", pending=1,
              ended_at=(now - timedelta(seconds=5)).isoformat())

        live = recover_idle_markers(now=now, db=db)

        assert [due.turn_id for due in live] == ["fresh-turn"]
        for index in range(ancient_count):
            assert _pending(db, f"ancient-{index:03d}-turn") == 0
    finally:
        db.close()


def test_recovery_counts_every_marker_it_clears_without_folding(monkeypatch) -> None:
    """A fold that never happens has to be visible after deploy."""
    import infra.product_events as product_events

    emitted: list[tuple[str, str, str | None, dict | None]] = []
    monkeypatch.setattr(
        product_events, "emit_product_event",
        lambda category, action, *, thread_id=None, user_id=None, properties=None:
            emitted.append((category, action, thread_id, properties)),
    )

    now = datetime(2026, 1, 1, 12, 0, 0)
    db = _db()
    try:
        from infra.idle_checkpoints import recover_idle_markers

        _thread(db, "cold")
        _thread(db, "archived", lifecycle_state="archived")
        _turn(db, "cold", "cold-turn", status="completed", pending=1,
              ended_at=(now - timedelta(seconds=240)).isoformat())
        _turn(db, "archived", "archived-turn", status="completed", pending=1,
              ended_at=(now - timedelta(seconds=10)).isoformat())

        assert recover_idle_markers(now=now, db=db) == ()

        assert sorted((thread_id, (properties or {}).get("reason"))
                      for category, action, thread_id, properties in emitted
                      if (category, action) == ("idle_checkpoint", "expired")) == [
            ("archived", "ineligible"),
            ("cold", "warm_window_passed"),
        ]
    finally:
        db.close()


def test_terminal_without_a_driver_asks_settlement_to_skip_marking(monkeypatch) -> None:
    """A settlement-discarding caller tells finish_turn not to write a marker."""
    import core.turn_core as turn_core
    from infra.thread_driver import TerminalResult, TerminalSettlement

    result = TerminalResult.APPLIED
    calls: list[dict] = []

    def fake_settlement(*args, **kwargs):
        calls.append(kwargs)
        return TerminalSettlement(result=result, thread_id="thread-1", idle_checkpoint=None)

    monkeypatch.setattr(turn_core, "persist_turn_settlement", fake_settlement)

    assert turn_core.persist_turn_terminal("turn-1", "completed", db=object()) is result
    assert len(calls) == 1
    assert calls[0]["mark_idle_checkpoint"] is False


@pytest.mark.asyncio
async def test_restart_rearms_each_recovered_marker(monkeypatch) -> None:
    from thread import idle_checkpoint

    live = (_due("a"), _due("b"))
    armed: list[object] = []

    async def recovered(function, *args, **kwargs):
        assert function.__name__ == "recover_idle_markers"
        return live

    monkeypatch.setattr(idle_checkpoint, "run_in_db", recovered)
    monkeypatch.setattr(idle_checkpoint, "arm_idle_checkpoint", armed.append)

    assert await idle_checkpoint.rearm_idle_checkpoints() == 2
    assert armed == list(live)
