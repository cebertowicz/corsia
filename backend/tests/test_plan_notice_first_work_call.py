"""The plan-upkeep line rides the Turn's first work call, not its first call.

A Turn that opens with a Plans tool (directly or through the ``tool_call``
wrapper) still gets the line, once, on its first call that is not one: both the
native seam (``core.turns_steps``) and the harness bridge
(``subagents.harness_tool_bridge``) are proven here, with real Plans state.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.plan_notice import first_work_call
from tests.plan_close_next_helpers import owner_plan, task
from tests.test_plan_reconcile_release import db, seed_threads  # noqa: F401 - fixture

MARK = "[CORSIA plan notice]"


def _call(call_id: str, name: str, arguments: dict | None = None) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments or {})},
    }


def _wrapped(call_id: str, target: str) -> dict:
    return _call(call_id, "mcp__corsia__tool_call", {"name": target, "arguments": {}})


def _declare(db, seq: int, calls: list[dict], turn_id: str = "turn-1") -> None:
    metadata = {"turn_id": turn_id, "llm_tool_calls": calls}
    db.execute(
        """INSERT INTO messages (id, thread_id, role, content, metadata, created_at, seq)
           VALUES (?, 'owner', 'assistant', '', ?, 't', ?)""",
        (uuid.uuid4().hex, json.dumps(metadata), seq),
    )
    db.commit()


def test_the_first_work_call_skips_plans_tools_direct_and_wrapped(db):
    _declare(db, 1, [_call("a", "plan_read"), _wrapped("b", "plan_update")])
    _declare(db, 2, [_call("c", "mcp__corsia__plan_decision"), _call("d", "bash")])
    _declare(db, 3, [_call("e", "read_file")])

    assert first_work_call(db, "owner", "turn-1") == "d"


def test_a_wrapper_around_a_work_tool_is_a_work_call(db):
    _declare(db, 1, [_call("a", "plan_read"), _wrapped("b", "bash")])

    assert first_work_call(db, "owner", "turn-1") == "b"


def test_a_turn_of_only_plans_calls_has_no_work_call(db):
    _declare(db, 1, [_call("a", "plan_read"), _wrapped("b", "plan_ensure")])

    assert first_work_call(db, "owner", "turn-1") is None


def _scene(connection, *, selected: bool = False):
    seed_threads(connection)
    plan = owner_plan(connection)
    brief = task(connection, plan.id, "Draft the brief")
    if selected:
        from tests.plan_close_next_helpers import select

        select(connection, plan.id, brief.id)
    return brief


def _declared(connection, batches: list[list[dict]]) -> None:
    for seq, calls in enumerate(batches, start=1):
        _declare(connection, seq, calls)


async def _native(batch: list[dict], monkeypatch) -> list[str]:
    from core import turns_steps
    from core.context_builder import ContextBuilder

    monkeypatch.setattr(
        "core.tool_result_projection.render_tool_result_for_model",
        lambda _thread_id, canonical, budget_tokens: canonical.content,
    )
    monkeypatch.setattr(turns_steps, "_native_tool_media", lambda *_a, **_k: None)
    results = []
    for call in batch:
        function = call["function"]
        results.append((
            SimpleNamespace(
                id=call["id"], name=function["name"],
                arguments=json.loads(function["arguments"]),
            ),
            "result\n",
            SimpleNamespace(role="tool", content="result\n", turn_id="turn-1"),
        ))
    messages = await turns_steps._append_tool_results(
        SimpleNamespace(context=ContextBuilder(workspace=Path("/tmp")), workspace=Path("/tmp")),
        [], results, "owner", 512, "anthropic/selected",
    )
    return [message["content"] for message in messages]


async def _bridge(batch: list[dict], monkeypatch) -> list[str]:
    from subagents.harness_tool_bridge import _with_notice_and_marker
    from subagents.harness_tool_publication import _foreground_result
    from tools.recorded_call import CompletedToolCall

    async def persist_delivery(**_kwargs):
        return SimpleNamespace(
            invocation_id="inv", assistant_step_id="step",
            render_for_model=lambda **_kw: "result\n",
        )

    async def media_delivery(_delivery, _model):
        return None

    texts = []
    for call in batch:
        function = call["function"]
        texts.append(await _foreground_result(
            CompletedToolCall(
                name=function["name"], result="result\n", invocation_id="inv",
                arguments=json.loads(function["arguments"]),
                provider_tool_call_id=call["id"],
            ),
            thread_id="owner", turn_identity=SimpleNamespace(turn_id="turn-1"),
            model=None, persist_delivery=persist_delivery,
            media_delivery=media_delivery, decorate_result=_with_notice_and_marker,
        ))
    return texts


SEAMS = pytest.mark.parametrize("seam", [_native, _bridge], ids=["native", "bridge"])


def _setup(isolated_db, batches):
    from infra.database import get_db

    connection = get_db()
    brief = _scene(connection, selected=True)
    _declared(connection, batches)
    connection.close()
    return brief


@pytest.mark.asyncio
@SEAMS
@pytest.mark.parametrize("opener", [
    _call("c1", "plan_read"), _wrapped("c1", "plan_read"),
], ids=["direct", "wrapped"])
async def test_a_work_call_after_a_plans_call_carries_the_line_and_the_plans_call_none(
    isolated_db, monkeypatch, seam, opener,
):
    batch = [opener, _call("c2", "bash")]
    _setup(isolated_db, [batch])

    texts = await seam(batch, monkeypatch)

    assert MARK not in texts[0]
    assert MARK in texts[1]


@pytest.mark.asyncio
@SEAMS
async def test_a_work_call_in_a_later_batch_than_the_plans_call_carries_the_line(
    isolated_db, monkeypatch, seam,
):
    first, second = [_wrapped("c1", "plan_read")], [_call("c2", "bash")]
    _setup(isolated_db, [first, second])

    assert MARK not in (await seam(first, monkeypatch))[0]
    assert MARK in (await seam(second, monkeypatch))[0]


@pytest.mark.asyncio
@SEAMS
async def test_a_parallel_batch_of_two_work_calls_carries_the_line_once(
    isolated_db, monkeypatch, seam,
):
    batch = [_call("c1", "bash"), _call("c2", "read_file")]
    _setup(isolated_db, [batch])

    texts = await seam(batch, monkeypatch)

    assert [MARK in text for text in texts] == [True, False]


@pytest.mark.asyncio
@SEAMS
async def test_a_third_work_call_in_a_later_batch_carries_no_line(
    isolated_db, monkeypatch, seam,
):
    first, second = [_call("c1", "bash"), _call("c2", "read_file")], [_call("c3", "bash")]
    _setup(isolated_db, [first, second])

    assert [MARK in text for text in await seam(first, monkeypatch)] == [True, False]
    assert MARK not in (await seam(second, monkeypatch))[0]


@pytest.mark.asyncio
@SEAMS
async def test_plans_tool_results_carry_no_line_even_when_first_in_the_turn(
    isolated_db, monkeypatch, seam,
):
    batch = [_call("c1", "plan_update"), _wrapped("c2", "plan_ensure")]
    _setup(isolated_db, [batch])

    assert all(MARK not in text for text in await seam(batch, monkeypatch))


def test_a_result_ruled_out_by_the_batch_lookup_opens_no_connection(monkeypatch):
    from core import plan_notice as module

    def refuse():
        raise AssertionError("a ruled-out result must not open a connection")

    monkeypatch.setattr(module, "get_db", refuse)

    assert module.plan_notice(
        thread_id="owner", turn_id="turn-1", tool_call_id="c2", tool_name="bash",
        first_work_call_id="c1",
    ) is None
