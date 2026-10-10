"""Working-process cap and silent idle retirement for Claude Code (#2064)."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from cc_runtime_fixtures import (  # noqa: F401
    _ready_count,
    _seed_parent as _seed_parent_row,
    _wait,
    cc_target,
    child_db,
    fake_claude,
    feed_with_persisted_input,
)

_NOW_ISO = "2026-09-18T12:00:00"


@pytest.fixture(autouse=True)
def _clean_live():
    from subagents.cc_runtime_state import _LIVE

    _LIVE.reset()
    yield
    _LIVE.reset()


@pytest.fixture
def caps(monkeypatch):
    """Pin the policy numbers so tests do not depend on the seed file."""
    values = {"idle": 600, "max_idle": 8, "cap": 2}
    monkeypatch.setattr(
        "infra.subagent_caps.get_idle_retire_after_seconds", lambda: values["idle"],
    )
    monkeypatch.setattr("infra.subagent_caps.get_max_idle_live", lambda: values["max_idle"])
    monkeypatch.setattr("infra.subagent_caps.get_max_concurrent_live", lambda: values["cap"])
    return values


def _now():
    from datetime import datetime

    return datetime.fromisoformat(_NOW_ISO)


def _handle(db, thread_id, *, idle_s=None, running=False, marker=False):
    """Hold one Claude Code handle whose last durable Turn ended ``idle_s`` ago."""
    from subagents.cc_runtime_state import _LIVE, _Live

    db.execute(
        "INSERT INTO threads (id,user_id,title,model,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (thread_id, "steve", f"Title {thread_id}", "claude-code/sonnet", "t", "t"),
    )
    if idle_s is not None or running:
        ended = None if running else (_now() - timedelta(seconds=idle_s)).isoformat()
        db.execute(
            "INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,"
            "started_at,ended_at,boot_epoch,idle_checkpoint_pending) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (f"turn-{thread_id}", thread_id, 0, "claude-code/sonnet",
             "running" if running else "completed", "t", ended, "boot",
             1 if marker else 0),
        )
    db.commit()
    live = _Live(session=None, parent_thread_id=None, name=thread_id)
    _LIVE.put(thread_id, live)
    return live


async def _pass():
    from subagents.cc_idle_retirement import retire_idle_once

    return await retire_idle_once(now=_now())


class TestWorkingCap:
    def test_idle_handles_beyond_cap_do_not_block_spawn(self, child_db, caps):
        from subagents.cc_runtime_helpers import _check_live_cap

        for index in range(3):
            _handle(child_db, f"idle-{index}", idle_s=5)
        _check_live_cap()

    def test_cap_many_working_handles_refuse_and_name_them(self, child_db, caps):
        from infra.subagent_caps import LiveSpawnCapExceeded
        from subagents.cc_runtime_helpers import _check_live_cap

        _handle(child_db, "aaaaaaaa-turn", idle_s=5).turn_active = True
        _handle(child_db, "bbbbbbbb-durable", running=True)
        _handle(child_db, "cccccccc-idle", idle_s=5)
        with pytest.raises(LiveSpawnCapExceeded) as refused:
            _check_live_cap()
        text = str(refused.value)
        assert refused.value.active == 2 and refused.value.cap == 2
        assert "2 Claude Code processes are working a Turn" in text
        assert "idle ones are not counted" in text
        assert 'aaaaaaaa "Title aaaaaaaa-turn"' in text
        assert 'bbbbbbbb "Title bbbbbbbb-durable"' in text
        assert "cccccccc" not in text

    def test_refusal_list_is_bounded(self):
        from infra.subagent_caps import LiveSpawnCapExceeded

        working = tuple((f"{i:08d}-x", f"T{i}") for i in range(7))
        text = str(LiveSpawnCapExceeded(cap=7, active=7, working=working))
        assert '00000004 "T4"' in text and "00000005" not in text
        assert "and 2 more" in text


class TestEligibility:
    @pytest.mark.asyncio
    async def test_retires_eligible_idle_handle(self, child_db, caps):
        from subagents.cc_runtime_state import _LIVE

        _handle(child_db, "old", idle_s=601)
        assert await _pass() == ("old",)
        assert _LIVE.get("old") is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("condition", [
        "turn_active", "durable_turn", "tool_tasks", "tool_steps",
        "pending_exit", "idle_marker", "active_checkpoint", "too_recent",
        "no_ended_turn",
    ])
    async def test_each_condition_blocks_retirement(
        self, child_db, caps, monkeypatch, condition,
    ):
        from subagents.cc_runtime_state import _LIVE

        live = _handle(
            child_db, "held",
            idle_s=None if condition == "no_ended_turn" else (
                5 if condition == "too_recent" else 601
            ),
            running=condition == "durable_turn",
            marker=condition == "idle_marker",
        )
        if condition == "turn_active":
            live.turn_active = True
        elif condition == "tool_tasks":
            live.admitted_tool_tasks.add(asyncio.get_running_loop().create_future())
        elif condition == "tool_steps":
            live.tool_step_ids["call-1"] = "step-1"
        elif condition == "pending_exit":
            live.pending_process_exit = object()
        elif condition == "active_checkpoint":
            monkeypatch.setattr(
                "infra.checkpoint_queries.active_for_thread",
                lambda thread_id, db=None: object(),
            )
        assert await _pass() == ()
        assert _LIVE.get("held") is live

    @pytest.mark.asyncio
    async def test_held_transition_lock_blocks_retirement(self, child_db, caps):
        from subagents.cc_runtime_state import _LIVE

        live = _handle(child_db, "locked", idle_s=601)
        async with _LIVE.lock("locked"):
            assert await _pass() == ()
        assert _LIVE.get("locked") is live

    @pytest.mark.asyncio
    async def test_handle_that_turns_active_after_selection_is_kept(self, child_db, caps):
        from subagents.cc_idle_retirement import _survey_for_retirement, retire_candidate
        from subagents.cc_runtime_state import _LIVE

        live = _handle(child_db, "raced", idle_s=601)
        _, _, _, candidates = await _survey_for_retirement()
        assert [c.thread_id for c in candidates] == ["raced"]
        live.turn_active = True
        assert await retire_candidate(candidates[0]) is False
        assert _LIVE.get("raced") is live

    @pytest.mark.asyncio
    async def test_turn_that_ran_after_selection_keeps_handle(self, child_db, caps, monkeypatch):
        from subagents.cc_idle_retirement import _survey_for_retirement, retire_candidate
        from subagents.cc_runtime_state import _LIVE
        from tests.event_loop_blocking import blocking_work_on_event_loop

        live = _handle(child_db, "raced", idle_s=601)
        _, _, _, candidates = await _survey_for_retirement()
        child_db.execute(
            "INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,"
            "started_at,ended_at,boot_epoch) VALUES "
            "('turn-2','raced',0,'claude-code/sonnet','completed','t',?, 'boot')",
            (_now().isoformat(),),
        )
        child_db.commit()
        with blocking_work_on_event_loop(monkeypatch) as blocking:
            assert await retire_candidate(candidates[0]) is False
        assert blocking == [], "\n".join(blocking)
        assert _LIVE.get("raced") is live


class TestIdleBound:
    @pytest.mark.asyncio
    async def test_bound_retires_longest_idle_eligible_first(self, child_db, caps):
        from subagents.cc_runtime_state import _LIVE

        caps["max_idle"] = 2
        _handle(child_db, "marked-oldest", idle_s=300, marker=True)
        _handle(child_db, "idle-30", idle_s=30)
        _handle(child_db, "idle-20", idle_s=20)
        _handle(child_db, "idle-10", idle_s=10)
        _handle(child_db, "working", idle_s=40).turn_active = True
        assert await _pass() == ("idle-30", "idle-20")
        assert set(_LIVE.ids()) == {"marked-oldest", "idle-10", "working"}

    @pytest.mark.asyncio
    async def test_unconfigured_policy_retires_nothing(self, child_db, caps):
        caps["idle"] = None
        _handle(child_db, "old", idle_s=601)
        assert await _pass() == ()


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_retirement_pass_keeps_sqlite_off_the_event_loop(
        self, child_db, caps, monkeypatch,
    ):
        from subagents.cc_idle_retirement import retire_idle_once
        from tests.event_loop_blocking import blocking_work_on_event_loop

        with blocking_work_on_event_loop(monkeypatch) as blocking:
            assert await retire_idle_once(now=_now()) == ()

        assert blocking == [], "\n".join(blocking)

    @pytest.mark.asyncio
    async def test_shutdown_cancels_the_pass(self, monkeypatch):
        from loguru import logger

        import app_lifecycle
        import subagents.cc_idle_retirement as retirement
        from app_startup_runtime import RUNTIME_LOOP_TASKS
        from infra import state

        # Shutdown joins every owned task on ``state``; a session-scoped app
        # started by an earlier test leaves its own there, on another loop.
        for name in (
            "_github_ownership_login_task",
            *(attr for _label, attr in RUNTIME_LOOP_TASKS),
        ):
            monkeypatch.setattr(state, name, None, raising=False)
        ran = asyncio.Event()

        async def once(**_kwargs):
            ran.set()
            return ()

        monkeypatch.setattr(retirement, "PASS_INTERVAL_S", 0)
        monkeypatch.setattr(retirement, "retire_idle_once", once)
        task = asyncio.create_task(retirement.idle_retirement_loop())
        monkeypatch.setattr(state, "_cc_idle_retirement_task", task, raising=False)
        await asyncio.wait_for(ran.wait(), 5)
        pending = await app_lifecycle._shutdown_owned_tasks([None] * 5, logger)
        assert pending is None and task.cancelled()


class TestSilentRetirement:
    @pytest.mark.asyncio
    async def test_retired_child_is_silent_and_cold_resumes(
        self, child_db, fake_claude, caps, monkeypatch, tmp_path,
    ):
        import subagents.cc_feed_transition as feed_transition
        import subagents.cc_runtime as rt
        from infra import state, thread_execution

        _seed_parent_row(child_db)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))
        monkeypatch.setattr(thread_execution, "drive_thread", _no_wake)
        casts: list[tuple[str, dict]] = []
        child_id = await rt.spawn(
            "parent", "first", binary=fake_claude, target=cc_target("sonnet"),
            broadcast=lambda sid, m: casts.append((sid, m)),
        )
        assert await _wait(lambda: (
            (live := rt._LIVE.get(child_id)) is not None
            and live.turn_terminal is not None and live.turn_terminal.is_set()
            and not live.turn_active
        ))
        live = rt._LIVE.get(child_id)
        assert live.user_driving  # a logical-close notice would reach the parent
        before = _durable_counts(child_db)
        emitted: list[tuple[str, dict]] = []
        monkeypatch.setattr(state, "broadcast_to_thread", lambda s, m: emitted.append((s, m)))
        casts.clear()

        from subagents.cc_idle_retirement import retire_idle_once

        later = _now() + timedelta(days=3650)
        assert await retire_idle_once(now=later) == (child_id,)
        assert rt._LIVE.get(child_id) is None
        assert live.session.returncode is not None
        assert _durable_counts(child_db) == before
        assert emitted == [] and casts == []

        resumed = []
        original = feed_transition.CCFeedTransition.resume

        async def spy(self):
            resumed.append(self.thread_id)
            return await original(self)

        monkeypatch.setattr(feed_transition.CCFeedTransition, "resume", spy)
        monkeypatch.setattr(
            "subagents.cc_runtime_resume._resolve_binary", lambda *_args: fake_claude,
        )
        did = await feed_with_persisted_input(
            child_id, "second", target=cc_target("sonnet"),
            turn_id="11111111-2222-4333-8444-555555555556",
        )
        assert did is not None and resumed == [child_id]
        assert rt._LIVE.get(child_id) is not live
        await rt.retire(child_id)


async def _no_wake(*_args, **_kwargs):
    return None


def _durable_counts(db) -> dict[str, int]:
    return {
        table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("messages", "active_turns", "checkpoints")
    }
