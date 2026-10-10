"""Unit tests for the unified live-process registry (Stage 1 substrate)."""

from __future__ import annotations

import ast
import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest

from core.turn_core import TurnIdentity
from subagents.live_registry import LiveBase, LiveRegistry, StaleTransition


@dataclass
class _CCLike(LiveBase):
    seed_file: str = ""
    model: str | None = None


@dataclass
class _CodexLike(LiveBase):
    thread_id: str = ""


def test_put_get_contains_pop_roundtrip():
    reg: LiveRegistry[_CCLike] = LiveRegistry()
    assert "s1" not in reg and reg.get("s1") is None
    h = _CCLike(session=object(), parent_thread_id="p", name="child", seed_file="/x")
    reg.put("s1", h)
    assert "s1" in reg
    assert reg.get("s1") is h
    assert reg.pop("s1") is h
    assert "s1" not in reg and reg.pop("s1") is None


def test_lock_is_stable_per_session_and_distinct_across_sessions():
    reg: LiveRegistry[_CodexLike] = LiveRegistry()
    a1 = reg.lock("a")
    a2 = reg.lock("a")
    b1 = reg.lock("b")
    assert a1 is a2  # same lock object for the same session
    assert a1 is not b1  # distinct per session


def test_existing_lock_lookup_does_not_create_a_lock():
    reg: LiveRegistry[_CodexLike] = LiveRegistry()

    assert reg.existing_lock("missing") is None

    lock = reg.lock("present")
    assert reg.existing_lock("present") is lock


def test_put_replaces_with_the_exact_new_handle():
    reg: LiveRegistry[_CCLike] = LiveRegistry()
    first = _CCLike(session=object(), parent_thread_id="p", name="first")
    replacement = _CCLike(
        session=object(), parent_thread_id="p", name="replacement",
    )

    reg.put("s1", first)
    reg.put("s1", replacement)

    assert reg.get("s1") is replacement
    assert reg.pop("s1") is replacement


def test_explicit_collection_queries_and_fixture_reset():
    reg: LiveRegistry[_CCLike] = LiveRegistry()
    first = _CCLike(session=object(), parent_thread_id="p", name="first")
    second = _CCLike(session=object(), parent_thread_id="p", name="second")
    reg.put("s1", first)
    reg.put("s2", second)
    reg.lock("s1")

    assert reg.count() == 2
    assert reg.ids() == ("s1", "s2")

    reg.reset()

    assert reg.count() == 0
    assert reg.ids() == ()
    assert reg.existing_lock("s1") is None


def test_subclass_carries_provider_fields_and_shared_buffers():
    h = _CodexLike(session=None, parent_thread_id=None, name="n", thread_id="t-1")
    h.thinking_buf += "hmm"
    h.turn_has_assistant_row = True
    assert h.thread_id == "t-1"  # provider-specific
    assert h.thinking_buf == "hmm" and h.turn_has_assistant_row  # shared base


@pytest.mark.asyncio
async def test_lock_serializes_transitions_for_one_session():
    reg: LiveRegistry[_CCLike] = LiveRegistry()
    order: list[str] = []

    async def crit(tag: str):
        async with reg.lock("s"):
            order.append(f"{tag}:enter")
            await asyncio.sleep(0)
            order.append(f"{tag}:exit")

    await asyncio.gather(crit("A"), crit("B"))
    # whichever ran first fully completes before the other enters
    assert order in (
        ["A:enter", "A:exit", "B:enter", "B:exit"],
        ["B:enter", "B:exit", "A:enter", "A:exit"],
    )


@pytest.mark.asyncio
async def test_publish_requires_the_current_acquired_lock_generation():
    reg: LiveRegistry[_CCLike] = LiveRegistry()
    handle = _CCLike(session=object(), parent_thread_id=None, name="h")
    lock = reg.lock("s1")

    with pytest.raises(StaleTransition):
        reg.publish("s1", handle, held=lock)
    with pytest.raises(StaleTransition):
        reg.publish("s1", handle, held=asyncio.Lock())
    assert reg.get("s1") is None

    async with lock:
        assert reg.holds("s1", lock)
        reg.publish("s1", handle, held=lock)
    assert reg.get("s1") is handle


@pytest.mark.asyncio
async def test_abandon_retires_a_held_generation_and_detaches_its_turn():
    reg: LiveRegistry[_CCLike] = LiveRegistry()
    handle = _CCLike(session=object(), parent_thread_id=None, name="h")
    handle.corsia_turn = TurnIdentity("turn-1", 1)
    stalled = reg.lock("s1")
    await stalled.acquire()
    reg.publish("s1", handle, held=stalled)

    assert reg.abandon("s1", "turn-2") is None
    assert reg.get("s1") is handle
    assert reg.abandon("s1", "turn-1") is handle

    assert reg.get("s1") is None
    # The harness caller releases the checkpoint wait it knows how to stop.
    assert not handle.checkpoint_wait_scope.stop_requested
    successor = reg.lock("s1")
    assert successor is not stalled
    assert reg.existing_lock("s1") is successor
    async with successor:
        with pytest.raises(StaleTransition):
            reg.publish("s1", handle, held=stalled)
        fresh = _CCLike(session=None, parent_thread_id=None, name="n")
        reg.publish("s1", fresh, held=successor)
    assert reg.get("s1") is fresh


@pytest.mark.asyncio
async def test_abandon_wakes_every_waiter_on_the_stalled_generation_to_fail():
    reg: LiveRegistry[_CCLike] = LiveRegistry()
    stalled = reg.lock("s1")
    await stalled.acquire()
    entered: list[str] = []

    async def transition(tag: str) -> None:
        async with reg.lock("s1"):
            entered.append(tag)

    waiters = [asyncio.create_task(transition(tag)) for tag in ("a", "b")]
    await asyncio.sleep(0)
    assert not any(waiter.done() for waiter in waiters)

    reg.abandon("s1", "turn-1")
    done, pending = await asyncio.wait(waiters, timeout=1)
    assert not pending
    assert all(isinstance(task.exception(), StaleTransition) for task in done)
    assert entered == []
    with pytest.raises(StaleTransition):
        await stalled.acquire()
    # Abandon released the generation for its stalled holder.
    stalled.release()
    successor = reg.lock("s1")
    await asyncio.wait_for(transition("fresh"), 1)
    assert entered == ["fresh"]
    with pytest.raises(RuntimeError, match="not acquired"):
        successor.release()


def test_abandon_keeps_an_unheld_lock_so_waiters_stay_current():
    reg: LiveRegistry[_CCLike] = LiveRegistry()
    idle = reg.lock("s1")

    assert reg.abandon("s1", "turn-1") is None
    assert reg.existing_lock("s1") is idle


def test_production_code_never_registers_handles_with_put():
    backend = Path(__file__).resolve().parents[1]
    offenders = []
    for path in backend.rglob("*.py"):
        relative = path.relative_to(backend)
        if relative.parts[0] in {"tests", ".venv", "venv"} or "site-packages" in relative.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "put"
                and ast.unparse(node.func.value).endswith("_LIVE")
            ):
                offenders.append(f"{relative}:{node.lineno}")
    assert offenders == [], "publish(held=...) is the only production registration"
