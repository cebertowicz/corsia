"""Steady-state SQLite writes run on the DB pool, never the event-loop thread.

A write transaction opened on the loop thread freezes every request for as long as
it holds, and for up to ``busy_timeout`` behind another writer. Each case runs the
real call path inside a running loop with ``CORSIA_DB_LOOP_WRITE_STRICT=1``, which
makes any on-loop write transaction raise.
"""

from __future__ import annotations

import asyncio
from contextlib import closing, contextmanager
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cc_runtime_fixtures import (  # noqa: F401
    StubCCInvocation, cc_target, child_db, prepared_cc_input,
)
from codex_runtime_fixtures import _admit_active_turn, _seed_runtime_sessions, locked_spawn
from infra import database
from infra.db_loop_guard import LOOP_WRITE_STRICT_ENV
from subagents import cc_runtime_resume
from tests.vendor_account_fixtures import signed_in_default
from tests.test_plan_event_wait_boundary import (  # noqa: F401
    OPENED, REPO, _armed, _identity, _owner, _watching,
)


@pytest.mark.asyncio
async def test_delivered_event_resolves_its_watch_off_the_event_loop(
    isolated_db, monkeypatch,
):
    from events import delivery
    from events.identity import pr_external_id
    from tests.event_routing_helpers import register_pr

    db = database.get_db()
    try:
        actor = _owner(db)
        register_pr(REPO, 7, "owner")
        external_id = pr_external_id(REPO, 7)
        _plan, node = _watching(db, actor, external_id=external_id, event=OPENED)
        node_id = node.id
        assert _armed(db, node_id) == 1
    finally:
        db.close()

    monkeypatch.setattr(delivery, "deliver_to_thread", AsyncMock(return_value=True))
    monkeypatch.setenv(LOOP_WRITE_STRICT_ENV, "1")

    assert await delivery.deliver_and_resolve_to_thread(
        "owner", _identity(external_id), OPENED, "opened", {},
    ) is True

    db = database.get_db()
    try:
        assert _armed(db, node_id) == 0
    finally:
        db.close()


@pytest.mark.asyncio
async def test_failed_delivery_leaves_the_watch_armed(isolated_db, monkeypatch):
    from events import delivery
    from events.identity import pr_external_id
    from tests.event_routing_helpers import register_pr

    db = database.get_db()
    try:
        actor = _owner(db)
        register_pr(REPO, 7, "owner")
        external_id = pr_external_id(REPO, 7)
        _plan, node = _watching(db, actor, external_id=external_id, event=OPENED)
        node_id = node.id
    finally:
        db.close()

    monkeypatch.setattr(delivery, "deliver_to_thread", AsyncMock(return_value=False))
    monkeypatch.setenv(LOOP_WRITE_STRICT_ENV, "1")

    assert await delivery.deliver_and_resolve_to_thread(
        "owner", _identity(external_id), OPENED, "opened", {},
    ) is False

    db = database.get_db()
    try:
        assert _armed(db, node_id) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_codex_cold_spawn_persists_its_install_off_the_event_loop(
    child_db, monkeypatch,  # noqa: F811
):
    """A cold Codex spawn retains its exact delivery before transport launch."""
    from core.installed_context import InstalledDelivery, read_boundary
    from core.turn_core import TurnIdentity
    from llm.context_window import WindowFacts
    from subagents import codex_runtime, codex_spawn_preflight

    thread_id = "main-1"
    turn_id = "cold-turn"
    _seed_runtime_sessions(thread_id)
    signed_in_default("codex", db=child_db)  # the input is admitted on the signed-in Default
    _admit_active_turn(thread_id, turn_id=turn_id, content="literal boundary")
    with closing(sqlite3.connect(str(database.DB_PATH), timeout=0.05)) as reader:
        reader.row_factory = sqlite3.Row
        boundary_id, stored = read_boundary(thread_id, reader)
    assert boundary_id is not None
    assert stored is None
    delivery = InstalledDelivery(
        thread_id=thread_id,
        adoption="current-no-anchor",
        boundary_id=boundary_id,
        stored=stored,
        snapshot={
            "model": "codex/gpt-5-codex",
            "project_id": None,
            "execution_epoch": 0,
            "runtime_context_epoch": 0,
            "sources": [],
        },
    )
    reconstruction = SimpleNamespace(
        installed=delivery,
        task_text="literal cold task",
        turn_text="literal resumed task",
        history_items=(),
        checkpoint_text=None,
        snapshot=SimpleNamespace(to_dict=lambda: {"kind": "cold"}),
    )
    launched = AsyncMock(return_value=TurnIdentity("cold-spawn", 1))
    monkeypatch.setattr(
        codex_spawn_preflight,
        "build_codex_reconstruction",
        lambda **_kwargs: reconstruction,
    )
    monkeypatch.setattr(codex_runtime, "_spawn_app_server_turn", launched)
    monkeypatch.setattr(codex_runtime, "_spawn_binary", lambda binary, _db: binary)
    monkeypatch.setenv(LOOP_WRITE_STRICT_ENV, "1")

    try:
        result = await locked_spawn(
            codex_runtime._spawn_turn,
            thread_id,
            "caller text",
            successor_turn_id="cold-spawn",
            codex_thread_id=None,
            binary="codex",
            catalog_model_id="codex/gpt-5-codex",
            windows=WindowFacts(None, None),
            db=None,
        )

        assert result == TurnIdentity("cold-spawn", 1)
        assert launched.await_args.args[:2] == (thread_id, "literal cold task")
        with closing(sqlite3.connect(str(database.DB_PATH), timeout=0.05)) as reader:
            reader.row_factory = sqlite3.Row
            assert read_boundary(thread_id, reader) == (boundary_id, delivery.snapshot)
    finally:
        codex_runtime._LIVE.reset()


@pytest.mark.asyncio
async def test_cc_start_records_its_facts_and_identity_off_the_event_loop(
    child_db, tmp_path, monkeypatch,  # noqa: F811
):
    """A cold Claude Code start's seed, install receipt and session identity leave the loop."""
    from registry.listing import find_for_thread
    from subagents import cc_destination, cc_runtime, cc_startup_process
    from subagents.cc_runtime_quiescence import cleanup_ephemeral_live

    child_db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('main','user','claude-code/sonnet','t','t')"
    )
    child_db.commit()

    class Session(StubCCInvocation):
        returncode = None

        def __init__(self, **kwargs):
            self.session_id = kwargs["session_id"]
            self.mcp_config = kwargs["mcp_config"]

        async def start(self):
            return None

        async def terminate(self):
            self.returncode = 0

    monkeypatch.setattr(cc_startup_process, "CCStreamSession", Session)
    monkeypatch.setattr(cc_destination, "_main_working_dir", lambda *_args: str(tmp_path))
    monkeypatch.setattr("subagents.cc_runtime_helpers._validate_working_dir", lambda *_args: None)
    target = cc_target("sonnet")
    prepared = await prepared_cc_input("main", "hello", target)
    lock = cc_runtime._LIVE.lock("main")
    await lock.acquire()
    monkeypatch.setenv(LOOP_WRITE_STRICT_ENV, "1")
    try:
        live = await cc_runtime_resume.start_cc_for_row(
            "main", target=target, binary="unused", current_user_text="hello",
            prepared_context=prepared, claude_dir=str(tmp_path / "claude"),
            broadcast=lambda *_args: None, surface_exit_error_fn=AsyncMock(),
            finalize_fn=cc_runtime._finalize, held_lock=lock,
        )
        assert live is not None
        assert cc_runtime._LIVE.get("main") is live
        with closing(sqlite3.connect(str(database.DB_PATH), timeout=0.05)) as reader:
            reader.row_factory = sqlite3.Row
            identity = find_for_thread("main", "claude-code", "session", db=reader)
            assert identity["external_id"] == live.session.session_id
            assert reader.execute(
                "SELECT COUNT(*) FROM messages WHERE thread_id='main' "
                "AND json_extract(metadata,'$.type')='seed'"
            ).fetchone()[0] == 1
    finally:
        lock.release()
        live = cc_runtime._LIVE.pop("main", None)
        if live is not None:
            cleanup_ephemeral_live(live, thread_id="main")


@pytest.mark.asyncio
async def test_host_access_review_usage_is_written_off_the_event_loop(
    isolated_db, monkeypatch,
):
    from host_access import reviewer
    from tests.event_routing_helpers import mk_session
    from tests.test_host_access_reviewer import (
        _application, _application_output, _target, _telemetry,
    )

    mk_session("root-1")
    target = _target("claude-code")
    application = _application()

    async def invoke(target, **kwargs):
        kwargs["telemetry_sink"](_telemetry(
            target, prompt_tokens=5, completion_tokens=2, total_tokens=7,
            cost_usd=0.001, cost_source="provider_reported", usage_available=True,
        ))
        return kwargs["validator"](_application_output())

    monkeypatch.setenv(LOOP_WRITE_STRICT_ENV, "1")
    result = await reviewer.review_application(
        target, application, binary="/bin/claude", invoke=invoke,
        usage_thread_id="root-1",
    )

    assert result.allowed
    db = database.get_db()
    try:
        rows = db.execute(
            "SELECT source FROM llm_usage WHERE thread_id='root-1'"
        ).fetchall()
    finally:
        db.close()
    assert [row["source"] for row in rows] == ["host_access_application_review"]


async def _launch_over_prior_identity(
    child_db, tmp_path, monkeypatch, *, wrap_install, predecessor=None,
):
    """Start a Claude Code row that already has a registered session, on a non-owned DB.

    ``wrap_install(real, lock, loop)`` returns the stand-in for
    ``install_starting_identity`` that runs on the DB pool, so the registration
    commits off the loop while the launch is suspended on it. ``predecessor`` is a
    live handle the launch replaces mid-Turn.
    """
    from registry.service import register_cc_session
    from subagents import cc_destination, cc_runtime, cc_startup_process

    child_db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('main','user','claude-code/sonnet','t','t')"
    )
    child_db.commit()
    register_cc_session("main", "prior-session", title="prior")

    class Session(StubCCInvocation):
        returncode = None

        def __init__(self, **kwargs):
            self.session_id = kwargs["session_id"]
            self.mcp_config = kwargs["mcp_config"]

        async def start(self):
            raise AssertionError("a launch that lost its place must not start the process")

        async def terminate(self):
            self.returncode = 0

    monkeypatch.setattr(cc_startup_process, "CCStreamSession", Session)
    monkeypatch.setattr(cc_destination, "_main_working_dir", lambda *_args: str(tmp_path))
    monkeypatch.setattr("subagents.cc_runtime_helpers._validate_working_dir", lambda *_args: None)
    target = cc_target("sonnet")
    prepared = await prepared_cc_input("main", "hello", target, allow_provider_resume=False)
    lock = cc_runtime._LIVE.lock("main")
    await lock.acquire()
    monkeypatch.setattr(
        cc_startup_process, "install_starting_identity",
        wrap_install(cc_startup_process.install_starting_identity, lock, asyncio.get_running_loop()),
    )
    monkeypatch.setenv(LOOP_WRITE_STRICT_ENV, "1")
    replacing = {}
    if predecessor is not None:
        cc_runtime._LIVE.put("main", predecessor)
        replacing = dict(
            expected_live=predecessor, continuing_identity=predecessor.corsia_turn,
            continuing_wait_scope=predecessor.checkpoint_wait_scope,
        )
    launch = asyncio.create_task(cc_runtime_resume.start_cc_for_row(
        "main", target=target, binary="unused", current_user_text="hello",
        prepared_context=prepared, claude_dir=str(tmp_path / "claude"),
        broadcast=lambda *_args: None, surface_exit_error_fn=AsyncMock(),
        finalize_fn=cc_runtime._finalize, held_lock=lock, **replacing,
    ))
    return launch, lock


def _registered_session() -> str:
    from registry.listing import find_for_thread

    with closing(sqlite3.connect(str(database.DB_PATH), timeout=0.05)) as reader:
        reader.row_factory = sqlite3.Row
        return find_for_thread("main", "claude-code", "session", db=reader)["external_id"]


def _cleanup_main_live():
    from subagents import cc_runtime
    from subagents.cc_runtime_quiescence import cleanup_ephemeral_live

    live = cc_runtime._LIVE.pop("main", None)
    if live is not None:
        cleanup_ephemeral_live(live, thread_id="main")


@pytest.mark.asyncio
async def test_launch_that_loses_its_place_during_the_registration_write_restores_the_prior_identity(
    child_db, tmp_path, monkeypatch,  # noqa: F811
):
    """The write commits on the pool, then the post-write check fails: the registration is undone."""
    from subagents import cc_runtime
    from subagents.live_registry import StaleTransition

    def wrap(real, lock, loop):
        def install(thread_id, context, session, db):
            previous = real(thread_id, context, session, db)
            # The registration is committed; the held lock goes stale before the launch resumes.
            loop.call_soon_threadsafe(lock.release)
            return previous
        return install

    launch, lock = await _launch_over_prior_identity(
        child_db, tmp_path, monkeypatch, wrap_install=wrap,
    )
    try:
        with pytest.raises(StaleTransition):
            await asyncio.wait_for(launch, timeout=5)
        assert cc_runtime._LIVE.get("main") is None
        assert _registered_session() == "prior-session"
    finally:
        if lock.locked():
            lock.release()
        _cleanup_main_live()


@pytest.mark.asyncio
async def test_launch_whose_predecessor_retires_during_the_registration_write_restores_the_prior_identity(
    child_db, tmp_path, monkeypatch,  # noqa: F811
):
    """Only the post-write predecessor check catches this: the lock and generation stay valid."""
    from core.turn_core import TurnIdentity
    from subagents import cc_runtime
    from subagents.cc_startup_process import CCReplacementPredecessorChanged
    from subagents.cc_runtime_state import _Live, activate_bound_turn

    identity = TurnIdentity("turn-retiring", 7)
    old = _Live(session=SimpleNamespace(), parent_thread_id=None, name="old", corsia_turn=identity)
    assert activate_bound_turn(old, identity)

    def wrap(real, lock, loop):
        def install(thread_id, context, session, db):
            previous = real(thread_id, context, session, db)
            loop.call_soon_threadsafe(setattr, old, "turn_active", False)  # the Turn ends
            return previous
        return install

    launch, lock = await _launch_over_prior_identity(
        child_db, tmp_path, monkeypatch, wrap_install=wrap, predecessor=old,
    )
    try:
        with pytest.raises(CCReplacementPredecessorChanged):
            await asyncio.wait_for(launch, timeout=5)
        assert cc_runtime._LIVE.get("main") is old
        assert _registered_session() == "prior-session"
    finally:
        if lock.locked():
            lock.release()
        _cleanup_main_live()


@pytest.mark.asyncio
async def test_launch_cancelled_while_the_registration_write_is_in_flight_restores_the_prior_identity(
    child_db, tmp_path, monkeypatch,  # noqa: F811
):
    """Cancellation joins the worker; the registration that landed is undone, never left stale."""
    import threading

    from subagents import cc_runtime

    committed, proceed = threading.Event(), threading.Event()

    def wrap(real, lock, loop):
        def install(thread_id, context, session, db):
            previous = real(thread_id, context, session, db)
            committed.set()
            assert proceed.wait(5)
            return previous
        return install

    launch, lock = await _launch_over_prior_identity(
        child_db, tmp_path, monkeypatch, wrap_install=wrap,
    )
    try:
        assert await asyncio.to_thread(committed.wait, 5)
        assert _registered_session() != "prior-session"  # the write has really landed
        launch.cancel()
        proceed.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(launch, timeout=5)
        assert cc_runtime._LIVE.get("main") is None
        assert _registered_session() == "prior-session"
    finally:
        proceed.set()
        if lock.locked():
            lock.release()
        _cleanup_main_live()


def test_a_watch_resolved_by_another_delivery_after_the_read_is_not_resolved_twice(
    isolated_db, monkeypatch,
):
    """Two deliveries match one watch; the second reads it armed, then loses it.

    The armed read runs outside the write, so the second delivery's write finds
    the watch already resolved. It must leave no second ``watch_resolved`` event
    and re-derive nothing.
    """
    from automation import plan_liveness, watches
    from events.identity import pr_external_id
    from plans import admission, service, watch_operations, watch_resolution
    from tests.event_routing_helpers import register_pr

    db = database.get_db()
    try:
        actor = _owner(db)
        register_pr(REPO, 7, "owner")
        external_id = pr_external_id(REPO, 7)
        _plan, node = _watching(db, actor, external_id=external_id, event=OPENED)
        node_id = node.id
        watch_id = db.execute(
            "SELECT id FROM expected_event_watches WHERE plan_node_id=?", (node_id,),
        ).fetchone()["id"]
    finally:
        db.close()

    real_write = service._write

    @contextmanager
    def rival_resolves_first(connection):
        # The other delivery's whole resolution lands between our read and write.
        other = database.get_db()
        try:
            with real_write(other):
                assert watches.resolve_watch(watch_id, db=other) is True
                watch_operations.record_resolved(
                    other, node_id, watch_id=watch_id, expected_event=OPENED,
                    actor_id=None,
                )
        finally:
            other.close()
        with real_write(connection):
            yield

    reconciled, rederived = [], []
    monkeypatch.setattr(
        admission, "reconcile_plan", lambda *a, **k: reconciled.append(a),
    )
    monkeypatch.setattr(
        plan_liveness, "on_turn_complete", lambda *a, **k: rederived.append(a),
    )
    monkeypatch.setattr(service, "_write", rival_resolves_first)

    assert watch_resolution.resolve_delivered_event(
        _identity(external_id), OPENED, recipient="owner",
    ) == []

    db = database.get_db()
    try:
        events = db.execute(
            "SELECT COUNT(*) FROM plan_node_events WHERE node_id=? "
            "AND event='watch_resolved'", (node_id,),
        ).fetchone()[0]
    finally:
        db.close()
    assert events == 1
    assert reconciled == []
    assert rederived == []
