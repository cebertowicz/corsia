"""Regression tests for the JSON-RPC result/error dispatch in
``CodexAppServerSession._handle_message``.

Guards the #515 regression: a misindented ``else`` paired the success branch
with ``if fut is not None and not fut.done():`` instead of ``if "error" in msg:``.
The effect was that a JSON-RPC success response ({"id": N, "result": {...}}, no
"error") never called ``fut.set_result(...)``, so the ``initialize`` request
future never resolved -> blocked until REQUEST_TIMEOUT -> fell back to
``codex exec`` (losing live-steer). These tests FAIL on the buggy nesting and
PASS once the ``else`` is re-paired with ``if "error" in msg:``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from codex_runtime_fixtures import declare_codex_tool_step
from subagents.codex_app_server import CodexAppServerSession
from subagents.codex_app_server_events import to_dynamic_tool_specs
from subagents.codex_app_server_errors import CodexAppServerError


def test_to_dynamic_tool_specs_flattens_and_skips_nameless():
    """OpenAI-nested defs flatten to codex DynamicToolSpec; a nameless spec is
    skipped (not crash), an already-flat spec passes through."""
    out = to_dynamic_tool_specs([
        {"type": "function", "function": {
            "name": "read_file", "description": "d",
            "parameters": {"type": "object"}}},
        {"type": "function", "function": {"description": "no name"}},
        {"type": "function", "name": "already_flat", "inputSchema": {}},
    ])
    names = [s.get("name") for s in out]
    assert names == ["read_file", "already_flat"]
    assert out[0] == {
        "type": "function",
        "name": "read_file",
        "description": "d",
        "inputSchema": {"type": "object"},
    }


def _make_session() -> CodexAppServerSession:
    """Build a session with no subprocess; only ``_pending`` is exercised."""
    return CodexAppServerSession(binary="codex", task=None)


def test_each_transport_gets_one_opaque_generation_uuid() -> None:
    from uuid import UUID

    first = _make_session()
    second = _make_session()

    assert str(UUID(first.transport_generation_id)) == first.transport_generation_id
    assert str(UUID(second.transport_generation_id)) == second.transport_generation_id
    assert first.transport_generation_id != second.transport_generation_id


def _activate_provider_turn(
    session: CodexAppServerSession,
    *,
    thread_id: str = "thread-1",
    turn_id: str = "turn-1",
) -> None:
    session.thread_id = thread_id
    session._track_active_turn(turn_id)


def _seed_dynamic_tool_thread(
    *,
    thread_id: str = "thread-1",
    turn_id: str = "turn-1",
):
    """Create the thread a declared dynamic tool call's Assistant Step lives in."""
    from core.turn_core import TurnIdentity
    from infra.database import get_db

    db = get_db()
    try:
        db.execute(
            "INSERT OR IGNORE INTO threads "
            "(id,user_id,model,created_at,updated_at) "
            "VALUES (?,?,?,'t','t')",
            (thread_id, "user-1", "codex/gpt-5.6-sol"),
        )
        db.commit()
    finally:
        db.close()
    return TurnIdentity(turn_id, 1)


def _isolate_app_server_settlement(monkeypatch) -> None:
    """Keep app-server unit tests below the separate live-runtime settlement seam."""
    async def settle(_session, _content, _reply) -> None:
        return None

    monkeypatch.setattr(
        "subagents.harness_tool_bridge.settle_codex_tool_result",
        settle,
    )


def test_detached_config_overrides_are_opt_in_and_exact() -> None:
    normal = CodexAppServerSession(binary="codex", task=None)
    detached = CodexAppServerSession(
        binary="codex",
        task=None,
        config_overrides=("include_environment_context=false",),
    )

    for session in (normal, detached):
        cmd = session.build_cmd()
        assert cmd.count("--strict-config") == 1
        assert cmd.count("tools.update_plan.enabled=false") == 1
        for control in (
            "features.sleep_tool=false",
            "features.goals=false",
            "tools.experimental_request_user_input.enabled=false",
            "orchestrator.skills.enabled=false",
        ):
            assert cmd.count(control) == 1
        assert cmd.count("skills.include_instructions=false") == 1
        assert cmd.count("skills.bundled.enabled=false") == 1
    assert detached.build_cmd()[-2:] == [
        "-c",
        "include_environment_context=false",
    ]


@pytest.mark.parametrize(
    "override",
    (
        "tools.update_plan.enabled=true",
        "features.sleep_tool=true",
        "features.goals=true",
        "tools.experimental_request_user_input.enabled=true",
        "orchestrator.skills.enabled=true",
    ),
)
def test_config_overrides_cannot_reenable_governed_codex_tools(override) -> None:
    session = CodexAppServerSession(
        binary="codex",
        task=None,
        config_overrides=(override,),
    )

    key = override.partition("=")[0]
    with pytest.raises(ValueError, match=key.replace(".", r"\.")):
        session.build_cmd()


async def test_exact_model_is_sent_on_fresh_and_continued_turn_start():
    """The discovered Codex id must survive every app-server request boundary."""
    calls: list[tuple[str, dict]] = []
    events: list[dict] = []

    async def on_event(event: dict) -> None:
        events.append(event)

    async def request(method: str, params: dict | None = None):
        calls.append((method, params or {}))
        if method in {"thread/start", "thread/resume"}:
            return {
                "thread": {"id": "thread-sol"},
                "model": "gpt-5.6-sol",
            }
        return {"turn": {"id": "turn-sol"}}

    fresh = CodexAppServerSession(
        binary="codex", task=None, model="gpt-5.6-sol", on_event=on_event,
    )
    fresh._request = request
    await fresh._start_or_resume_thread()
    assert await fresh.start_turn("hello")

    resumed = CodexAppServerSession(
        binary="codex", task=None, codex_thread_id="thread-sol",
        model="gpt-5.6-sol", on_event=on_event,
    )
    resumed._request = request
    await resumed._start_or_resume_thread()

    assert [
        (method, params["model"])
        for method, params in calls
        if method in ("thread/start", "thread/resume", "turn/start")
    ] == [
        ("thread/start", "gpt-5.6-sol"),
        ("turn/start", "gpt-5.6-sol"),
        ("thread/resume", "gpt-5.6-sol"),
    ]
    assert {
        "kind": "model_reported",
        "model": "gpt-5.6-sol",
    } in events


async def test_thread_started_event_carries_reported_rollout_path():
    """``thread.path`` from thread/start rides the thread_started event so the
    registry can record the rollout location at registration time; a response
    without it (or with a non-string) emits the id-only event unchanged."""
    events: list[dict] = []

    async def on_event(event: dict) -> None:
        events.append(event)

    responses = iter([
        {"thread": {
            "id": "thread-with-path",
            "path": "/runtime/home/sessions/2026/07/23/rollout-t.jsonl",
        }},
        {"thread": {"id": "thread-without-path"}},
        {"thread": {"id": "thread-bad-path", "path": ""}},
    ])

    async def request(method: str, params: dict | None = None):
        assert method == "thread/start"
        return next(responses)

    for _ in range(3):
        session = CodexAppServerSession(binary="codex", task=None, on_event=on_event)
        session._request = request
        await session._start_or_resume_thread()

    started = [ev for ev in events if ev.get("kind") == "thread_started"]
    assert started == [
        {
            "kind": "thread_started",
            "thread_id": "thread-with-path",
            "rollout_path": "/runtime/home/sessions/2026/07/23/rollout-t.jsonl",
        },
        {"kind": "thread_started", "thread_id": "thread-without-path"},
        {"kind": "thread_started", "thread_id": "thread-bad-path"},
    ]


async def test_detached_thread_and_turn_controls_are_opt_in_and_exact():
    calls: list[tuple[str, dict]] = []
    output_schema = {
        "type": "object",
        "properties": {"summary": {"type": "string"}},
        "required": ["summary"],
        "additionalProperties": False,
    }

    async def request(method: str, params: dict | None = None):
        calls.append((method, params or {}))
        if method == "thread/start":
            return {"thread": {"id": "detached-thread"}}
        return {"turn": {"id": "detached-turn"}}

    detached = CodexAppServerSession(
        binary="codex",
        task=None,
        working_dir="/tmp/empty-checkpoint-cwd",
        model="gpt-5.6-sol",
        ephemeral=True,
        allow_provider_model_fallback=False,
        developer_instructions="Return only the checkpoint schema.",
    )
    detached._request = request

    await detached._start_or_resume_thread()
    assert await detached.start_turn("quoted transcript", output_schema=output_schema)

    assert calls == [
        (
            "thread/start",
            {
                "cwd": "/tmp/empty-checkpoint-cwd",
                "model": "gpt-5.6-sol",
                "sandbox": "read-only",
                "approvalPolicy": "never",
                "environments": [],
                "ephemeral": True,
                "allowProviderModelFallback": False,
                "developerInstructions": "Return only the checkpoint schema.",
            },
        ),
        (
            "turn/start",
            {
                "threadId": "detached-thread",
                "input": [
                    {
                        "type": "text",
                        "text": "quoted transcript",
                        "text_elements": [],
                    }
                ],
                "environments": [],
                "model": "gpt-5.6-sol",
                "outputSchema": output_schema,
            },
        ),
    ]

    normal_calls: list[tuple[str, dict]] = []

    async def normal_request(method: str, params: dict | None = None):
        normal_calls.append((method, params or {}))
        return {"thread": {"id": "normal-thread"}}

    normal = CodexAppServerSession(binary="codex", task=None)
    normal._request = normal_request
    await normal._start_or_resume_thread()
    normal_start = normal_calls[0][1]
    assert "ephemeral" not in normal_start
    assert "allowProviderModelFallback" not in normal_start
    assert "developerInstructions" not in normal_start


def test_isolated_codex_home_overlays_a_copy_of_the_clean_environment(monkeypatch):
    import subagents.codex_env as codex_env

    clean_env = {
        "PATH": "/usr/bin",
        "CODEX_HOME": "/parent/home",
        "CODEX_SQLITE_HOME": "/parent/home",
    }
    isolated_homes = []

    def clean(*, source_home=None, isolated_home=None, govern_model_catalog=False):
        assert source_home is None
        isolated_homes.append(isolated_home)
        assert govern_model_catalog is True
        if isolated_home is None:
            return clean_env
        return {
            "PATH": "/usr/bin",
            "CODEX_HOME": str(isolated_home),
            "CODEX_SQLITE_HOME": str(isolated_home),
        }

    monkeypatch.setattr(
        codex_env,
        "clean_codex_app_server_env",
        clean,
    )

    normal = CodexAppServerSession(binary="codex", task=None)
    detached = CodexAppServerSession(
        binary="codex",
        task=None,
        codex_home="/tmp/isolated-codex-home",
    )

    assert normal.build_env() == clean_env
    assert normal.build_env() is not clean_env
    assert detached.build_env() == {
        "PATH": "/usr/bin",
        "CODEX_HOME": "/tmp/isolated-codex-home",
        "CODEX_SQLITE_HOME": "/tmp/isolated-codex-home",
    }
    assert clean_env["CODEX_HOME"] == "/parent/home"
    assert clean_env["CODEX_SQLITE_HOME"] == "/parent/home"
    assert isolated_homes == [None, None, Path("/tmp/isolated-codex-home")]


async def test_raw_message_observer_sees_unmodeled_items_and_server_requests():
    observed: list[dict] = []

    async def observe(message: dict) -> None:
        observed.append(message)

    session = CodexAppServerSession(
        binary="codex",
        task=None,
        on_raw_message=observe,
    )
    unknown_item = {
        "method": "item/started",
        "params": {"item": {"id": "file-1", "type": "fileChange"}},
    }
    approval_request = {
        "id": 19,
        "method": "item/fileChange/requestApproval",
        "params": {"itemId": "file-1"},
    }

    await session._handle_message(unknown_item)
    await session._handle_message(approval_request)

    assert observed == [unknown_item, approval_request]


async def test_raw_message_observer_failure_is_loud():
    async def reject(_message: dict) -> None:
        raise RuntimeError("detached checkpoint observed forbidden event")

    session = CodexAppServerSession(
        binary="codex",
        task=None,
        on_raw_message=reject,
    )

    with pytest.raises(RuntimeError, match="forbidden event"):
        await session._handle_message({
            "method": "item/started",
            "params": {"item": {"id": "web-1", "type": "webSearch"}},
        })


async def test_raw_message_observer_failure_kills_reader_and_rejects_pending():
    async def reject(_message: dict) -> None:
        raise RuntimeError("detached checkpoint observed forbidden event")

    class _Stdout:
        async def readline(self) -> bytes:
            return json.dumps({
                "method": "item/started",
                "params": {"item": {"id": "web-1", "type": "webSearch"}},
            }).encode() + b"\n"

    class _Proc:
        def __init__(self) -> None:
            self.stdout = _Stdout()
            self.returncode = None
            self.kill_calls = 0

        def kill(self) -> None:
            self.kill_calls += 1
            self.returncode = -9

    session = CodexAppServerSession(
        binary="codex",
        task=None,
        on_raw_message=reject,
    )
    proc = _Proc()
    session._proc = proc
    pending = asyncio.get_running_loop().create_future()
    session._pending[7] = pending

    await session._read_loop()

    assert proc.kill_calls == 1
    with pytest.raises(RuntimeError, match="exited before response"):
        pending.result()


@pytest.mark.parametrize("method", ["item/started", "item/completed"])
async def test_provider_native_item_violation_keeps_provider_turn_identity(
    method: str,
) -> None:
    session = _make_session()
    _activate_provider_turn(
        session,
        thread_id="provider-thread-1",
        turn_id="provider-turn-1",
    )
    emitted: list[dict] = []

    async def on_event(event: dict) -> None:
        emitted.append(event)

    session.set_callbacks(on_event=on_event)
    await session._handle_notification(
        method,
        {
            "threadId": "provider-thread-1",
            "turnId": "provider-turn-1",
            "item": {
                "id": "native-action-1",
                "type": "commandExecution",
                "command": "redacted by the normalizer",
            },
        },
    )

    assert emitted == [{
        "kind": "provider_tool_violation",
        "provider": "codex",
        "id": "native-action-1",
        "tool_type": "commandExecution",
        "phase": method.removeprefix("item/"),
        "provider_thread_id": "provider-thread-1",
        "provider_turn_id": "provider-turn-1",
    }]


@pytest.mark.parametrize("method", ["item/started", "item/completed"])
async def test_current_dynamic_tool_lifecycle_keeps_provider_turn_identity(
    method: str,
) -> None:
    session = _make_session()
    _activate_provider_turn(session)
    emitted: list[dict] = []
    session.set_callbacks(on_event=lambda event: _append_event(emitted, event))

    await session._handle_notification(
        method,
        {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "item": {
                "id": "call-1",
                "type": "dynamicToolCall",
                "tool": "read_file",
                "arguments": {"path": "AGENTS.md"},
                "result": "file contents",
                "success": True,
            },
        },
    )

    assert emitted == [{
        "kind": "tool_use" if method == "item/started" else "tool_result",
        **(
            {
                "id": "call-1",
                "name": "read_file",
                "input": {"path": "AGENTS.md"},
            }
            if method == "item/started"
            else {
                "tool_use_id": "call-1",
                "content": "file contents",
                "is_error": False,
            }
        ),
        "provider_thread_id": "thread-1",
        "provider_turn_id": "turn-1",
    }]


@pytest.mark.parametrize("method", ["item/started", "item/completed"])
@pytest.mark.parametrize(
    "provider_identity",
    [
        {"threadId": "thread-1", "turnId": "turn-stale"},
        {"threadId": "thread-stale", "turnId": "turn-1"},
        {},
    ],
    ids=["stale-turn", "stale-thread", "missing-identity"],
)
async def test_uncorrelated_dynamic_lifecycle_stops_before_callback(
    method: str,
    provider_identity: dict[str, str],
) -> None:
    session = _make_session()
    _activate_provider_turn(session)
    session._last_reasoning_summary_item_id = "reasoning-current"
    session._reasoning_summary_parts = {"reasoning-current": {0: "current"}}
    emitted: list[dict] = []
    session.set_callbacks(on_event=lambda event: _append_event(emitted, event))

    await session._handle_notification(
        method,
        {
            **provider_identity,
            "item": {
                "id": "call-stale",
                "type": "dynamicToolCall",
                "tool": "read_file",
                "arguments": {"path": "stale"},
                "result": "stale result",
                "success": True,
            },
        },
    )

    assert emitted == []
    assert session.active_turn_id == "turn-1"
    assert session._last_reasoning_summary_item_id == "reasoning-current"
    assert session._reasoning_summary_parts == {
        "reasoning-current": {0: "current"},
    }


async def _append_event(events: list[dict], event: dict) -> None:
    events.append(event)


async def test_provider_reroute_updates_reported_model_identity():
    session = _make_session()
    events: list[dict] = []

    async def on_event(event: dict) -> None:
        events.append(event)

    session.set_callbacks(on_event=on_event)
    await session._handle_notification(
        "model/rerouted",
        {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "fromModel": "gpt-5.6-sol",
            "toModel": "gpt-5.6-terra",
            "reason": "fallback",
        },
    )

    assert events == [{
        "kind": "model_reported",
        "model": "gpt-5.6-terra",
    }]


async def test_reasoning_summary_event_carries_typed_native_reference():
    session = _make_session()
    events: list[dict] = []

    async def on_event(event: dict) -> None:
        events.append(event)

    session.set_callbacks(on_event=on_event)
    await session._handle_notification(
        "item/reasoning/summaryTextDelta",
        {"itemId": "rs_123", "delta": "Consider the evidence."},
    )

    assert events == [{
        "kind": "step_reasoning",
        "text": "Consider the evidence.",
        "provenance": {
            "source_harness": "codex",
            "source_provider": "codex",
            "representation_kind": "codex_reasoning_summary",
            "native_reference": "rs_123",
        },
    }]


async def test_reasoning_completion_does_not_duplicate_streamed_summary():
    session = _make_session()
    events: list[dict] = []

    async def on_event(event: dict) -> None:
        events.append(event)

    session.set_callbacks(on_event=on_event)
    await session._handle_notification(
        "item/reasoning/summaryTextDelta",
        {
            "itemId": "rs_streamed",
            "summaryIndex": 0,
            "delta": "First part",
        },
    )
    await session._handle_notification(
        "item/reasoning/summaryTextDelta",
        {
            "itemId": "rs_streamed",
            "summaryIndex": 1,
            "delta": "Second part",
        },
    )
    await session._handle_notification(
        "item/completed",
        {"item": {
            "type": "reasoning",
            "id": "rs_streamed",
            "summary": ["First part", "Second part"],
            "content": [],
        }},
    )

    assert [event["text"] for event in events] == [
        "First part",
        "\n\nSecond part",
    ]


async def test_distinct_reasoning_items_preserve_summary_boundary():
    session = _make_session()
    events: list[dict] = []

    async def on_event(event: dict) -> None:
        events.append(event)

    session.set_callbacks(on_event=on_event)
    for item_id, text in (
        ("rs_first", "**First item**"),
        ("rs_second", "**Second item**"),
    ):
        await session._handle_notification(
            "item/reasoning/summaryTextDelta",
            {"itemId": item_id, "summaryIndex": 0, "delta": text},
        )
        await session._handle_notification(
            "item/completed",
            {"item": {
                "type": "reasoning",
                "id": item_id,
                "summary": [text],
                "content": [],
            }},
        )

    assert "".join(event["text"] for event in events) == (
        "**First item**\n\n**Second item**"
    )


async def test_reasoning_parser_state_clears_only_for_current_terminal():
    session = _make_session()

    async def on_event(_event: dict) -> None:
        return None

    session.set_callbacks(on_event=on_event)
    session._track_active_turn("turn-current")
    await session._handle_notification(
        "item/reasoning/summaryTextDelta",
        {"itemId": "rs_current", "summaryIndex": 0, "delta": "partial"},
    )
    await session._handle_notification(
        "turn/completed",
        {"turn": {"id": "turn-stale", "status": "failed"}},
    )
    assert session._reasoning_summary_parts == {"rs_current": {0: "partial"}}

    await session._handle_notification(
        "turn/completed",
        {"turn": {"id": "turn-current", "status": "failed"}},
    )
    assert session._reasoning_summary_parts == {}
    assert session._opaque_reasoning_references == set()
    assert session._last_reasoning_summary_item_id is None


async def test_reasoning_completion_is_summary_fallback_and_preserves_parts():
    session = _make_session()
    events: list[dict] = []

    async def on_event(event: dict) -> None:
        events.append(event)

    session.set_callbacks(on_event=on_event)
    await session._handle_notification(
        "item/completed",
        {"item": {
            "type": "reasoning",
            "id": "rs_fallback",
            "summary": ["First part", "Second part"],
            "content": [],
        }},
    )

    assert events == [{
        "kind": "step_reasoning",
        "text": "First part\n\nSecond part",
        "provenance": {
            "source_harness": "codex",
            "source_provider": "codex",
            "representation_kind": "codex_reasoning_summary",
            "native_reference": "rs_fallback",
        },
    }]


async def test_raw_reasoning_delta_is_opaque_provenance_not_prose():
    session = _make_session()
    events: list[dict] = []

    async def on_event(event: dict) -> None:
        events.append(event)

    session.set_callbacks(on_event=on_event)
    await session._handle_notification(
        "item/reasoning/textDelta",
        {
            "itemId": "rs_private",
            "contentIndex": 0,
            "delta": "provider-private-content",
        },
    )

    assert events == [{
        "kind": "step_reasoning",
        "text": "",
        "provenance": {
            "source_harness": "codex",
            "source_provider": "codex",
            "representation_kind": "opaque",
            "native_reference": "rs_private",
        },
    }]


async def test_empty_reasoning_completion_retains_opaque_reference():
    session = _make_session()
    events: list[dict] = []

    async def on_event(event: dict) -> None:
        events.append(event)

    session.set_callbacks(on_event=on_event)
    await session._handle_notification(
        "item/completed",
        {"item": {
            "type": "reasoning",
            "id": "rs_opaque",
            "summary": [],
            "content": [],
        }},
    )

    assert events == [{
        "kind": "step_reasoning",
        "text": "",
        "provenance": {
            "source_harness": "codex",
            "source_provider": "codex",
            "representation_kind": "opaque",
            "native_reference": "rs_opaque",
        },
    }]


async def test_turn_volume_is_the_delta_of_the_thread_cumulative_counter():
    """``tokenUsage.total`` accumulates over the thread, so a turn is its delta.

    Recording ``total`` itself would re-bill every earlier turn on the thread.
    The two updates below mirror a real sequence: ``total`` advances by exactly
    the reported ``last`` (42_114 + 23_587 == 65_701), proving it is cumulative.
    """
    from infra.provider_usage import TokenUsage

    session = _make_session()
    events: list[dict] = []

    async def on_event(event: dict) -> None:
        events.append(event)

    session.set_callbacks(on_event=on_event)
    session._track_active_turn("turn-1")
    for total_input, total_cached, last_input, last_cached in (
        (42_114, 20_224, 23_298, 18_304),
        (65_701, 22_144, 23_587, 1_920),
    ):
        await session._handle_notification(
            "thread/tokenUsage/updated",
            {
                "turnId": "turn-1",
                "tokenUsage": {
                    "total": {
                        "inputTokens": total_input,
                        "cachedInputTokens": total_cached,
                    },
                    "last": {
                        "inputTokens": last_input,
                        "cachedInputTokens": last_cached,
                    },
                    "modelContextWindow": 258_400,
                },
            },
        )
    await session._handle_notification(
        "turn/completed",
        {"turn": {"id": "turn-1", "status": "completed"}},
    )

    # The thread stood at 18_816 input / 1_920 cached before this turn, so the
    # turn itself processed the remainder -- not the 65_701 cumulative figure.
    assert events[-1]["token_volume"] == TokenUsage(
        input_uncached=(65_701 - 22_144) - (18_816 - 1_920),
        cache_read=22_144 - 1_920,
        # This payload carries no outputTokens, so output stays unknown
        # rather than being asserted as a measured zero.
        unreported=frozenset({"output"}),
    )
    assert events[-1]["token_volume"].total == 46_885


async def test_turn_volume_is_unmeasured_without_a_cumulative_counter():
    """No ``total`` means no volume claim -- the terminal says so explicitly."""
    session = _make_session()
    events: list[dict] = []

    async def on_event(event: dict) -> None:
        events.append(event)

    session.set_callbacks(on_event=on_event)
    session._track_active_turn("turn-no-total")
    await session._handle_notification(
        "thread/tokenUsage/updated",
        {
            "turnId": "turn-no-total",
            "tokenUsage": {
                "last": {"inputTokens": 8_192, "cachedInputTokens": 4_096},
                "modelContextWindow": 258_400,
            },
        },
    )
    await session._handle_notification(
        "turn/completed",
        {"turn": {"id": "turn-no-total", "status": "completed"}},
    )

    assert events[-1]["token_volume"] is None


async def test_final_token_usage_is_emitted_once_and_purged_on_success():
    session = _make_session()
    events: list[dict] = []

    async def on_event(event: dict) -> None:
        events.append(event)

    session.set_callbacks(on_event=on_event)
    session._track_active_turn("turn-1")
    await session._handle_notification(
        "thread/tokenUsage/updated",
        {
            "turnId": "turn-1",
            "tokenUsage": {
                "total": {"inputTokens": 42_114, "cachedInputTokens": 20_224},
                "last": {"inputTokens": 23_298, "cachedInputTokens": 18_304},
                "modelContextWindow": 258_400,
            },
        },
    )
    await session._handle_notification(
        "thread/tokenUsage/updated",
        {
            "turnId": "turn-1",
            "tokenUsage": {
                "total": {"inputTokens": 65_701, "cachedInputTokens": 22_144},
                "last": {"inputTokens": 23_587, "cachedInputTokens": 1_920},
                "modelContextWindow": 258_400,
            },
        },
    )
    await session._handle_notification(
        "turn/completed",
        {"turn": {"id": "turn-1", "status": "completed"}},
    )

    assert events[-1]["usage"] == {
        "input_tokens": 23_587,
        "cached_input_tokens": 1_920,
        "model_context_window": 258_400,
    }
    assert "turn-1" not in session._usage_by_turn


@pytest.mark.parametrize("event_shape", ["notification", "item"])
async def test_uncorrelated_compaction_breach_preserves_active_usage(event_shape):
    session = _make_session()
    events: list[dict] = []

    async def on_event(event: dict) -> None:
        events.append(event)

    session.set_callbacks(on_event=on_event)
    session.thread_id = "thread-active"
    session._track_active_turn("turn-active")
    session._usage_by_turn["turn-active"] = {
        "last": {"inputTokens": 222_222},
        "modelContextWindow": 258_400,
    }

    if event_shape == "notification":
        await session._handle_notification(
            "thread/compacted",
            {"threadId": "thread-stale", "turnId": "turn-active"},
        )
    else:
        await session._handle_notification(
            "item/completed",
            {
                "threadId": "thread-stale",
                "turnId": "turn-active",
                "item": {"id": "compact-stale", "type": "contextCompaction"},
            },
        )

    assert "turn-active" in session._usage_by_turn
    assert events[-1]["kind"] == "provider_compaction_breach"


@pytest.mark.parametrize("terminal_form", ["error", "turn/completed"])
async def test_failed_terminal_emits_and_purges_last_observed_usage(terminal_form):
    session = _make_session()
    events: list[dict] = []

    async def on_event(event: dict) -> None:
        events.append(event)

    session.set_callbacks(on_event=on_event)
    session._track_active_turn("turn-failed")
    await session._handle_notification(
        "thread/tokenUsage/updated",
        {
            "turnId": "turn-failed",
            "tokenUsage": {
                "last": {"inputTokens": 8_192, "cachedInputTokens": 4_096},
                "modelContextWindow": 258_400,
            },
        },
    )
    if terminal_form == "error":
        await session._handle_notification(
            "error",
            {
                "turnId": "turn-failed",
                "willRetry": False,
                "error": {"message": "provider failed"},
            },
        )
    else:
        await session._handle_notification(
            "turn/completed",
            {"turn": {
                "id": "turn-failed",
                "status": "failed",
                "error": {"message": "provider failed"},
            }},
        )

    assert events[-1]["kind"] == "turn_failed"
    assert events[-1]["usage"] == {
        "input_tokens": 8_192,
        "cached_input_tokens": 4_096,
        "model_context_window": 258_400,
    }
    assert "turn-failed" not in session._usage_by_turn


async def test_result_response_resolves_pending_future():
    """A JSON-RPC success response must resolve the pending future with its result."""
    session = _make_session()
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    session._pending[7] = fut

    await session._handle_message({"id": 7, "result": {"ok": True}})

    assert fut.done()
    assert fut.result() == {"ok": True}
    # The pending entry is consumed.
    assert 7 not in session._pending


async def test_turn_start_response_owns_identity_before_buffered_native_item():
    """A buffered native item must not outrun turn identity correlation."""
    session = _make_session()
    session.thread_id = "provider-thread-1"
    emitted: list[dict] = []

    async def on_event(event: dict) -> None:
        emitted.append(event)

    session.set_callbacks(on_event=on_event)
    future: asyncio.Future = asyncio.get_running_loop().create_future()
    session._pending[11] = future
    session._pending_methods[11] = "turn/start"

    await session._handle_message({
        "id": 11,
        "result": {"turn": {"id": "provider-turn-1"}},
    })
    await session._handle_message({
        "method": "item/started",
        "params": {
            "threadId": "provider-thread-1",
            "turnId": "provider-turn-1",
            "item": {
                "id": "native-collab-1",
                "type": "collabAgentToolCall",
                "tool": "spawnAgent",
            },
        },
    })

    assert session.active_turn_id == "provider-turn-1"
    assert emitted == [{
        "kind": "provider_tool_violation",
        "provider": "codex",
        "id": "native-collab-1",
        "tool_type": "collabAgentToolCall",
        "phase": "started",
        "provider_thread_id": "provider-thread-1",
        "provider_turn_id": "provider-turn-1",
    }]


async def test_result_response_with_none_result_resolves():
    """A success response with a null/absent result resolves to ``None``."""
    session = _make_session()
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    session._pending[3] = fut

    await session._handle_message({"id": 3, "result": None})

    assert fut.done()
    assert fut.result() is None


async def test_error_response_rejects_pending_future():
    """A JSON-RPC error response must reject the future with CodexAppServerError."""
    session = _make_session()
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    session._pending[9] = fut

    err = {"code": -32000, "message": "boom"}
    await session._handle_message({"id": 9, "error": err})

    assert fut.done()
    with pytest.raises(CodexAppServerError) as excinfo:
        fut.result()
    assert excinfo.value.payload == err
    assert 9 not in session._pending


async def test_request_records_provider_bound_json_rpc_payload():
    """Source-proof captures the exact JSON-RPC request sent to the app server."""
    session = _make_session()

    class _Stdin:
        def __init__(self):
            self.writes: list[bytes] = []

        def write(self, data: bytes) -> None:
            self.writes.append(data)

        async def drain(self) -> None:
            return None

    class _Proc:
        def __init__(self):
            self.stdin = _Stdin()
            self.returncode = None

    session._proc = _Proc()
    task = asyncio.create_task(
        session._request("thread/start", {"cwd": "/work/project"})
    )
    await asyncio.sleep(0)

    assert session.request_log == [{
        "id": 1,
        "method": "thread/start",
        "params": {"cwd": "/work/project"},
    }]

    fut = session._pending[1]
    fut.set_result({"thread": {"id": "thread-1"}})
    assert await task == {"thread": {"id": "thread-1"}}


async def test_turn_start_cancel_before_write_is_unaccepted_and_cleans_request():
    session = _make_session()
    session.thread_id = "thread-1"
    observed: list[str] = []

    class _Stdin:
        def write(self, _data: bytes) -> None:
            observed.append("write")

        async def drain(self) -> None:
            observed.append("drain")

    class _Proc:
        stdin = _Stdin()
        returncode = None

    session._proc = _Proc()
    await session._write_lock.acquire()
    task = asyncio.create_task(session.start_turn(
        "causal input",
        on_write_observed=lambda: observed.append("observed"),
        on_drain_confirmed=lambda: observed.append("confirmed"),
    ))
    await asyncio.sleep(0)
    task.cancel()
    session._write_lock.release()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert observed == []
    assert session._pending == {}


async def test_turn_start_cancel_after_write_returns_without_waiting_for_drain():
    session = _make_session()
    session.thread_id = "thread-1"
    written = asyncio.Event()
    never_drains = asyncio.Event()
    observed: list[str] = []

    class _Stdin:
        def write(self, _data: bytes) -> None:
            written.set()

        async def drain(self) -> None:
            await never_drains.wait()

    class _Proc:
        stdin = _Stdin()
        returncode = None

    session._proc = _Proc()
    task = asyncio.create_task(session.start_turn(
        "causal input",
        on_write_observed=lambda: observed.append("observed"),
        on_drain_confirmed=lambda: observed.append("confirmed"),
    ))
    await written.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=0.1)
    assert observed == ["observed"]
    assert session._pending == {}


async def test_reader_exit_rejects_written_turn_start_without_request_timeout():
    session = _make_session()
    session.thread_id = "thread-1"
    written = asyncio.Event()
    eof = asyncio.Event()

    class _Stdin:
        def write(self, _data: bytes) -> None:
            written.set()

        async def drain(self) -> None:
            return None

    class _Stdout:
        async def readline(self) -> bytes:
            await eof.wait()
            return b""

    class _Proc:
        stdin = _Stdin()
        stdout = _Stdout()
        returncode = None

    session._proc = _Proc()
    request = asyncio.create_task(session.start_turn("causal input"))
    await written.wait()
    reader = asyncio.create_task(session._read_loop())
    eof.set()

    with pytest.raises(RuntimeError, match="exited before response"):
        await asyncio.wait_for(request, timeout=0.1)
    await reader
    assert session._pending == {}


async def test_wait_reader_drained_accepts_already_cancelled_reader():
    session = _make_session()
    reader = asyncio.create_task(asyncio.Event().wait())
    reader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reader
    session._reader = reader

    await session.wait_reader_drained()


async def test_wait_reader_drained_preserves_caller_cancellation_and_reader():
    session = _make_session()
    release_reader = asyncio.Event()
    reader = asyncio.create_task(release_reader.wait())
    session._reader = reader
    waiter = asyncio.create_task(session.wait_reader_drained())
    await asyncio.sleep(0)

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert reader.done() is False

    release_reader.set()
    await reader


async def test_model_list_collects_every_page_with_provider_cursors():
    session = _make_session()
    calls: list[tuple[str, dict]] = []

    async def _request(method: str, params: dict | None = None):
        calls.append((method, params or {}))
        cursor = (params or {}).get("cursor")
        if cursor is None:
            return {
                "data": [{"id": "gpt-5.6-sol", "model": "gpt-5.6-sol"}],
                "nextCursor": "page-2",
            }
        return {
            "data": [{"id": "gpt-5.6-terra", "model": "gpt-5.6-terra"}],
            "nextCursor": None,
        }

    session._request = _request

    result = await session.list_models()

    assert [row["id"] for row in result["data"]] == [
        "gpt-5.6-sol",
        "gpt-5.6-terra",
    ]
    assert result["nextCursor"] is None
    assert calls == [
        ("model/list", {"includeHidden": False, "limit": 100}),
        ("model/list", {"includeHidden": False, "limit": 100, "cursor": "page-2"}),
    ]


async def test_dynamic_tools_register_on_thread_start_not_initialize():
    """CORSIA-owned dynamic tools must register on thread/start (codex
    DynamicToolSpec shape), not on initialize. initialize only advertises the
    capability bool. The prior contract (tools on initialize) never surfaced
    them to the model — codex reads dynamicTools on thread/start."""
    session = CodexAppServerSession(
        binary="codex",
        task=None,
        working_dir="/work/project",
        dynamic_tools=(
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read a file",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ),
    )
    calls: list[tuple[str, dict]] = []
    notifications: list[tuple[str, dict]] = []

    async def _request(method: str, params: dict | None = None):
        calls.append((method, params or {}))
        if method == "thread/start":
            return {"thread": {"id": "thread-1"}}
        return {}

    async def _notify(method: str, params: dict):
        notifications.append((method, params))

    session._request = _request
    session._notify = _notify

    await session._initialize()
    await session._start_or_resume_thread()

    # initialize: capability advertised, but NO tool list, NO fictional
    # disabledFeatures, and NOT sandbox/approvalPolicy (InitializeParams only
    # accepts capabilities + clientInfo; those are ignored there).
    init = next(p for m, p in calls if m == "initialize")
    assert init["capabilities"]["experimentalApi"] is True
    assert init["capabilities"]["dynamicTools"] is True
    assert "dynamicTools" not in init
    assert "disabledFeatures" not in init
    assert "sandbox" not in init
    assert "approvalPolicy" not in init
    assert "environments" not in init

    # thread/start: the flattened codex DynamicToolSpec shape, plus the
    # sandbox + approvalPolicy that gate native tools (bare SandboxMode enum).
    start = next(p for m, p in calls if m == "thread/start")
    assert start["sandbox"] == "read-only"
    assert start["approvalPolicy"] == "never"
    assert start["environments"] == []
    spec = start["dynamicTools"][0]
    assert spec == {
        "type": "function",
        "name": "read_file",
        "description": "Read a file",
        "inputSchema": {"type": "object", "properties": {}},
    }
    assert notifications == [("initialized", {})]


async def test_persisted_provider_handle_resumes_with_local_dynamic_tool_callbacks(
    isolated_db,
    monkeypatch,
):
    """Resume restores provider tools while CORSIA keeps their local callbacks."""

    class _Registry:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        def get(self, _name):
            return None

        async def execute(self, name: str, params: dict) -> str:
            self.calls.append((name, params))
            return "file contents"

    registry = _Registry()
    identity = _seed_dynamic_tool_thread()
    _isolate_app_server_settlement(monkeypatch)
    session = CodexAppServerSession(
        binary="codex",
        task=None,
        codex_thread_id="thread-existing",
        working_dir="/work/project",
        model="gpt-5.6-sol",
        corsia_thread_id="thread-1",
        corsia_turn_identity=identity,
        dynamic_tools=(
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read a file",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ),
        tool_registry=registry,
    )
    declare_codex_tool_step(session, "call-1", arguments={"path": "AGENTS.md"})
    calls: list[tuple[str, dict]] = []
    responses: list[tuple[int, dict | None, dict | None]] = []

    async def _request(method: str, params: dict | None = None):
        calls.append((method, params or {}))
        if method == "initialize":
            return {}
        return {"thread": {"id": "thread-existing"}, "model": "gpt-5.6-sol"}

    async def _respond(msg_id: int, *, result=None, error=None):
        responses.append((msg_id, result, error))

    async def _notify(_method: str, _params: dict) -> None:
        return None

    session._request = _request
    session._notify = _notify
    session._respond = _respond

    await session._initialize()
    await session._start_or_resume_thread()

    initialize = calls[0]
    assert initialize[0] == "initialize"
    assert initialize[1]["capabilities"]["dynamicTools"] is True
    method, params = calls[1]
    assert method == "thread/resume"
    assert params == {
        "threadId": "thread-existing",
        "cwd": "/work/project",
        "model": "gpt-5.6-sol",
        "sandbox": "read-only",
        "approvalPolicy": "never",
        "excludeTurns": True,
    }
    assert "dynamicTools" not in params

    _activate_provider_turn(session, thread_id="thread-existing")
    await session._handle_server_request({
        "id": 11,
        "method": "item/tool/call",
        "params": {
            "tool": "read_file",
            "arguments": {"path": "AGENTS.md"},
            "callId": "call-1",
            "threadId": "thread-existing",
            "turnId": "turn-1",
        },
    })

    assert registry.calls == [("read_file", {"path": "AGENTS.md"})]
    assert responses[0][1]["success"] is True


async def test_resume_refusal_fails_without_starting_a_replacement_thread():
    session = CodexAppServerSession(
        binary="codex",
        task=None,
        codex_thread_id="thread-missing-rollout",
        dynamic_tools=({"name": "read_file"},),
    )
    calls: list[str] = []

    async def _request(method: str, _params: dict | None = None):
        calls.append(method)
        raise CodexAppServerError("no rollout found for thread id thread-missing-rollout")

    session._request = _request

    with pytest.raises(CodexAppServerError, match="no rollout found"):
        await session._start_or_resume_thread()

    assert calls == ["thread/resume"]
    assert session.thread_id == "thread-missing-rollout"


async def test_dynamic_tool_request_executes_through_registry(
    isolated_db,
    monkeypatch,
):
    class _Registry:
        def __init__(self):
            self.calls: list[tuple[str, dict]] = []

        def get(self, _name):
            return None

        async def execute(self, name: str, params: dict) -> str:
            self.calls.append((name, params))
            return "file contents"

    registry = _Registry()
    identity = _seed_dynamic_tool_thread()
    _isolate_app_server_settlement(monkeypatch)
    session = CodexAppServerSession(
        binary="codex",
        task=None,
        tool_registry=registry,
        corsia_thread_id="thread-1",
        corsia_turn_identity=identity,
    )
    declare_codex_tool_step(session, "call-1", arguments={"path": "AGENTS.md"})
    _activate_provider_turn(session)
    responses: list[tuple[int, dict | None, dict | None]] = []

    async def _respond(msg_id: int, *, result=None, error=None):
        responses.append((msg_id, result, error))

    session._respond = _respond

    await session._handle_server_request({
        "id": 11,
        "method": "item/tool/call",
        "params": {
            "tool": "read_file",
            "arguments": {"path": "AGENTS.md"},
            "callId": "call-1",
            "threadId": "thread-1",
            "turnId": "turn-1",
        },
    })

    assert registry.calls == [("read_file", {"path": "AGENTS.md"})]
    # Codex DynamicToolCallResponse shape, not {content, is_error}.
    assert responses == [(
        11,
        {
            "contentItems": [{
                "type": "inputText",
                "text": (
                    "file contents\n\n"
                    '[CORSIA_TOOL_INVOCATION reference={"invocation_id":"call-1"}]'
                ),
            }],
            "success": True,
        },
        None,
    )]


@pytest.mark.parametrize(
    "provider_identity",
    [
        {"threadId": "thread-1", "turnId": "turn-stale"},
        {"threadId": "thread-stale", "turnId": "turn-1"},
        {},
    ],
    ids=["stale-turn", "stale-thread", "missing-identity"],
)
async def test_uncorrelated_dynamic_tool_request_fails_before_execution(
    provider_identity: dict[str, str],
) -> None:
    class _Registry:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        def get(self, _name):
            return None

        async def execute(self, name: str, params: dict) -> str:
            self.calls.append((name, params))
            return "must not execute"

    registry = _Registry()
    session = CodexAppServerSession(
        binary="codex",
        task=None,
        tool_registry=registry,
    )
    _activate_provider_turn(session)
    responses: list[tuple[int, dict | None, dict | None]] = []

    async def respond(msg_id: int, *, result=None, error=None) -> None:
        responses.append((msg_id, result, error))

    session._respond = respond

    await session._handle_server_request({
        "id": 12,
        "method": "item/tool/call",
        "params": {
            **provider_identity,
            "tool": "read_file",
            "arguments": {"path": "AGENTS.md"},
            "callId": "call-stale",
        },
    })

    assert registry.calls == []
    assert session.active_turn_id == "turn-1"
    assert session._dynamic_tool_requests_idle.is_set()
    assert responses[0][0] == 12
    assert responses[0][1]["success"] is False
    assert responses[0][2] is None


async def test_late_turn_started_cannot_rebind_dynamic_request_to_successor() -> None:
    """A stale lifecycle notification cannot make turn A current after turn B."""

    class _Registry:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        def get(self, _name):
            return None

        async def execute(self, name: str, params: dict) -> str:
            self.calls.append((name, params))
            return "must not execute"

    registry = _Registry()
    session = CodexAppServerSession(
        binary="codex",
        task=None,
        tool_registry=registry,
    )
    _activate_provider_turn(
        session,
        thread_id="thread-1",
        turn_id="turn-B",
    )
    responses: list[tuple[int, dict | None, dict | None]] = []

    async def respond(msg_id: int, *, result=None, error=None) -> None:
        responses.append((msg_id, result, error))

    session._respond = respond

    await session._handle_notification(
        "turn/started",
        {"threadId": "thread-1", "turn": {"id": "turn-A"}},
    )
    await session._handle_server_request({
        "id": 13,
        "method": "item/tool/call",
        "params": {
            "threadId": "thread-1",
            "turnId": "turn-A",
            "tool": "read_file",
            "arguments": {"path": "stale"},
            "callId": "call-A",
        },
    })

    assert session.thread_id == "thread-1"
    assert session.active_turn_id == "turn-B"
    assert registry.calls == []
    assert responses == [(
        13,
        {
            "contentItems": [{
                "type": "inputText",
                "text": (
                    "Error: Codex dynamic tool request does not belong to "
                    "the active provider turn"
                ),
            }],
            "success": False,
        },
        None,
    )]


async def test_provider_lifecycle_cannot_replace_active_identity() -> None:
    """Provider notifications observe identity; request responses own it."""
    session = _make_session()
    _activate_provider_turn(
        session,
        thread_id="thread-B",
        turn_id="turn-B",
    )

    await session._handle_notification(
        "thread/started",
        {"thread": {"id": "thread-A"}},
    )
    await session._handle_notification(
        "turn/started",
        {"threadId": "thread-A", "turn": {"id": "turn-A"}},
    )

    assert session.thread_id == "thread-B"
    assert session.active_turn_id == "turn-B"


def test_item_tool_call_is_recognized_as_dynamic_tool_request():
    """Codex 0.142.x sends dynamic tool calls as item/tool/call; the matcher
    MUST recognize it or the call is misrouted to the approval branch and
    never executed (the model then falls back to native shell)."""
    from subagents import codex_app_server_events as ev
    assert ev.dynamic_tool_request("item/tool/call") is True
    assert ev.dynamic_tool_request("item/commandExecution/requestApproval") is False


async def test_dynamic_tool_error_result_reports_success_false(
    isolated_db,
    monkeypatch,
):
    """A tool result starting with 'Error' maps to success=False so codex
    surfaces the failure instead of treating garbage as a valid result."""
    class _Registry:
        def get(self, _name):
            return None

        async def execute(self, name: str, params: dict) -> str:
            return "Error: no such file"

    identity = _seed_dynamic_tool_thread()
    _isolate_app_server_settlement(monkeypatch)
    session = CodexAppServerSession(
        binary="codex",
        task=None,
        tool_registry=_Registry(),
        corsia_thread_id="thread-1",
        corsia_turn_identity=identity,
    )
    declare_codex_tool_step(session, "call-error", arguments={"path": "x"})
    _activate_provider_turn(session)
    responses: list[tuple[int, dict | None, dict | None]] = []

    async def _respond(msg_id: int, *, result=None, error=None):
        responses.append((msg_id, result, error))

    session._respond = _respond
    await session._handle_server_request({
        "id": 5,
        "method": "item/tool/call",
        "params": {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "callId": "call-error",
            "tool": "read_file",
            "arguments": {"path": "x"},
        },
    })
    assert responses == [(
        5,
        {
            "contentItems": [{
                "type": "inputText",
                "text": (
                    "Error: no such file\n\n"
                    '[CORSIA_TOOL_INVOCATION reference={"invocation_id":"call-error"}]'
                ),
            }],
            "success": False,
        },
        None,
    )]


async def test_rejected_dynamic_tool_request_does_not_open_result_debt():
    """A late rejected request does not disturb the request-idle fence."""
    session = CodexAppServerSession(
        binary="codex", task=None, tool_registry=object(),
    )
    _activate_provider_turn(session)
    responses: list[dict] = []

    async def respond(_msg_id: int, *, result=None, error=None):
        assert error is None
        responses.append(result)

    session._respond = respond
    session.revoke_dynamic_tool_admission()

    await session._handle_server_request({
        "id": 70,
        "method": "item/tool/call",
        "params": {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "callId": "call-late",
            "tool": "read_file",
            "arguments": {"path": "late"},
        },
    })

    assert responses[0]["success"] is False
    assert responses[0]["contentItems"][0]["text"] == (
        "Error: This Turn is being stopped; end your turn. Do not retry."
    )
    await asyncio.wait_for(
        session.wait_dynamic_tool_requests_idle(), timeout=0.1,
    )


async def test_dynamic_handler_failure_closes_request_debt():
    """A handler that has exited cannot remain as phantom admitted work."""
    class _Registry:
        def get(self, _name):
            return None

        async def execute(self, _name: str, _params: dict) -> str:
            raise RuntimeError("tool handler failed")

    session = CodexAppServerSession(
        binary="codex", task=None, tool_registry=_Registry(),
    )
    _activate_provider_turn(session)
    declare_codex_tool_step(session, "call-handler-failed", persist=False)

    with pytest.raises(RuntimeError, match="tool handler failed"):
        await session._handle_server_request({
            "id": 71,
            "method": "item/tool/call",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "callId": "call-handler-failed",
                "tool": "read_file",
                "arguments": {"path": "x"},
            },
        })

    await asyncio.wait_for(
        session.wait_dynamic_tool_requests_idle(), timeout=0.1,
    )


async def test_dynamic_response_write_failure_closes_request_debt(
    isolated_db,
    monkeypatch,
):
    """A request handler that exited cannot leave impossible local debt."""
    class _Registry:
        def get(self, _name):
            return None

        async def execute(self, _name: str, _params: dict) -> str:
            return "actual result"

    identity = _seed_dynamic_tool_thread()
    _isolate_app_server_settlement(monkeypatch)
    session = CodexAppServerSession(
        binary="codex",
        task=None,
        tool_registry=_Registry(),
        corsia_thread_id="thread-1",
        corsia_turn_identity=identity,
    )
    declare_codex_tool_step(session, "call-response-failed", arguments={"path": "x"})
    _activate_provider_turn(session)

    async def respond(_msg_id: int, *, result=None, error=None):
        assert result == {
            "contentItems": [{
                "type": "inputText",
                "text": (
                    "actual result\n\n"
                    "[CORSIA_TOOL_INVOCATION "
                    'reference={"invocation_id":"call-response-failed"}]'
                ),
            }],
            "success": True,
        }
        assert error is None
        raise BrokenPipeError("Codex response pipe closed")

    session._respond = respond

    with pytest.raises(BrokenPipeError, match="response pipe closed"):
        await session._handle_server_request({
            "id": 71,
            "method": "item/tool/call",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "callId": "call-response-failed",
                "tool": "read_file",
                "arguments": {"path": "x"},
            },
        })

    await asyncio.wait_for(
        session.wait_dynamic_tool_requests_idle(), timeout=0.1,
    )


async def test_dynamic_tool_reader_preserves_admitted_result_before_terminal(
    isolated_db,
    monkeypatch,
    tmp_path,
):
    """Revocation preserves admitted results and rejects queued late work.

    This drives the real stdout reader and JSON-RPC dispatcher.  The confirmed
    interrupt may resolve only after both tool results crossed the event
    persistence callback and the reader then processed ``turn/completed``.
    """
    events: list[str] = []
    execute_started = asyncio.Event()
    release_execute = asyncio.Event()
    durable_steps: dict[str, str] = {}
    tools_settled = asyncio.Event()
    tools_settled.set()

    class _Registry:
        def __init__(self):
            self.calls: list[tuple[str, dict]] = []

        def get(self, _name):
            return None

        async def execute(self, name: str, params: dict) -> str:
            self.calls.append((name, params))
            events.append("admitted_execute_started")
            execute_started.set()
            await release_execute.wait()
            events.append("admitted_execute_returned")
            return "actual admitted result"

    class _Stdout:
        def __init__(self):
            self.lines: asyncio.Queue[bytes] = asyncio.Queue()

        async def readline(self) -> bytes:
            return await self.lines.get()

        def send(self, payload: dict) -> None:
            self.lines.put_nowait((json.dumps(payload) + "\n").encode())

        def close(self) -> None:
            self.lines.put_nowait(b"")

    class _Stdin:
        def __init__(self):
            self.messages: asyncio.Queue[dict] = asyncio.Queue()

        def write(self, payload: bytes) -> None:
            message = json.loads(payload)
            if message.get("id") in (71, 72):
                events.append(f"response_{message['id']}_written")
            self.messages.put_nowait(message)

        async def drain(self) -> None:
            return None

    class _Proc:
        def __init__(self):
            self.stdout = _Stdout()
            self.stdin = _Stdin()
            self.returncode = None

        async def wait(self):
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9

    registry = _Registry()
    proc = _Proc()
    identity = _seed_dynamic_tool_thread()
    _isolate_app_server_settlement(monkeypatch)
    session: CodexAppServerSession

    async def on_event(event: dict) -> None:
        kind = event.get("kind")
        tool_id = event.get("id") or event.get("tool_use_id")
        if kind == "tool_use":
            events.append(f"tool_start_{tool_id}_persisted")
            durable_steps[tool_id] = declare_codex_tool_step(
                session, tool_id, arguments=event.get("input") or {},
            )
            tools_settled.clear()
        elif kind == "tool_result":
            events.append(f"tool_result_{tool_id}_persisted")
            durable_steps.pop(tool_id, None)
            if not durable_steps:
                tools_settled.set()
        elif kind == "result":
            assert durable_steps == {}
            assert tools_settled.is_set()
            events.append("turn_terminal_persisted")

    session = CodexAppServerSession(
        binary="codex",
        task=None,
        codex_thread_id="thread-1",
        tool_registry=registry,
        on_event=on_event,
        corsia_thread_id="thread-1",
        corsia_turn_identity=identity,
    )
    session._proc = proc
    session._reader = asyncio.create_task(session._read_loop())
    session._track_active_turn("turn-1")

    # Stock Codex appends the exact response receipt after the pre-Turn cursor.
    rollout = tmp_path / "rollout.jsonl"
    rollout.touch()
    session._tool_requests.rollout_path = str(rollout)
    await session._tool_requests.prepare_turn()
    records = [
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-1"}},
        {"type": "response_item", "payload": {"type": "function_call", "call_id": "call-1"}},
        {"type": "token_usage_record", "payload": {
            "thread_id": "thread-1", "turn_id": "turn-1", "response_id": "response-1",
            "usage": {"input_tokens": 100, "output_tokens": 10},
            "thread_token_usage": {"input_tokens": 1000, "output_tokens": 100},
        }},
    ]
    rollout.write_text("".join(json.dumps(record) + "\n" for record in records))

    proc.stdout.send({
        "method": "item/started",
        "params": {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "item": {
                "id": "call-1", "type": "dynamicToolCall",
                "tool": "read_file", "arguments": {"path": "one"},
            },
        },
    })
    proc.stdout.send({
        "id": 71,
        "method": "item/tool/call",
        "params": {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "callId": "call-1", "tool": "read_file",
            "arguments": {"path": "one"},
        },
    })
    await asyncio.wait_for(execute_started.wait(), timeout=1)

    session.revoke_dynamic_tool_admission()
    proc.stdout.send({
        "id": 72,
        "method": "item/tool/call",
        "params": {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "callId": "call-2", "tool": "read_file",
            "arguments": {"path": "two"},
        },
    })
    interrupt_task = asyncio.create_task(session.interrupt(timeout=1.0))
    release_execute.set()

    interrupt_request: dict | None = None
    tool_responses: dict[int, dict] = {}
    while interrupt_request is None or len(tool_responses) < 2:
        message = await asyncio.wait_for(proc.stdin.messages.get(), timeout=1.0)
        if message.get("method") == "turn/interrupt":
            interrupt_request = message
        elif message.get("id") in (71, 72):
            tool_responses[message["id"]] = message["result"]

    assert registry.calls == [("read_file", {"path": "one"})]
    assert tool_responses[71]["success"] is True
    assert tool_responses[71]["contentItems"][0]["text"] == (
        "actual admitted result\n\n"
        '[CORSIA_TOOL_INVOCATION reference={"invocation_id":"call-1"}]'
    )
    assert tool_responses[72]["success"] is False
    assert tool_responses[72]["contentItems"][0]["text"] == (
        "Error: This Turn is being stopped; end your turn. Do not retry."
    )
    assert not interrupt_task.done()

    proc.stdout.send({"id": interrupt_request["id"], "result": {}})
    proc.stdout.send({
        "method": "item/completed",
        "params": {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "item": {
                "id": "call-1", "type": "dynamicToolCall",
                "result": "actual admitted result", "success": True,
            },
        },
    })
    proc.stdout.send({
        "method": "turn/completed",
        "params": {"turn": {"id": "turn-1", "status": "interrupted"}},
    })

    assert await asyncio.wait_for(interrupt_task, timeout=1.0) is True
    assert events.index("admitted_execute_returned") < events.index(
        "tool_result_call-1_persisted"
    )
    assert "tool_start_call-2_persisted" not in events
    assert "tool_result_call-2_persisted" not in events
    assert events.index("tool_result_call-1_persisted") < events.index(
        "turn_terminal_persisted"
    )
    assert tools_settled.is_set()
    await asyncio.wait_for(
        session.wait_dynamic_tool_requests_idle(), timeout=0.1,
    )

    proc.stdout.close()
    await asyncio.wait_for(session._reader, timeout=1.0)


async def test_start_turn_injects_reconstructed_history_before_current_input():
    """Reconstructed CORSIA history must reach Codex before the live turn starts."""
    session = _make_session()
    session.thread_id = "thread-1"
    calls: list[tuple[str, dict]] = []
    history_items = (
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "prior request"}],
        },
    )

    async def _request(method: str, params: dict | None = None):
        calls.append((method, params or {}))
        if method == "turn/start":
            return {"turn": {"id": "turn-1"}}
        return {}

    session._request = _request

    assert await session.start_turn("current request", history_items=history_items)
    assert calls == [
        (
            "thread/inject_items",
            {"threadId": "thread-1", "items": list(history_items)},
        ),
        (
            "turn/start",
            {
                "threadId": "thread-1",
                "input": [
                    {
                        "type": "text",
                        "text": "current request",
                        "text_elements": [],
                    }
                ],
                "environments": [],
            },
        ),
    ]


async def test_compact_notification_emits_compaction_breach():
    session = _make_session()
    events: list[dict] = []

    async def on_event(ev: dict) -> None:
        events.append(ev)

    session.set_callbacks(on_event=on_event)

    await session._handle_notification(
        "thread/compacted",
        {"threadId": "provider-thread-1", "turnId": "provider-turn-1"},
    )

    assert events == [{
        "kind": "provider_compaction_breach",
        "source": "json-rpc-notification",
        "provider_thread_id": "provider-thread-1",
        "provider_turn_id": "provider-turn-1",
        "provider_item_id": None,
    }]


async def test_current_context_compaction_item_preserves_envelope_identity():
    session = _make_session()
    events: list[dict] = []

    async def on_event(ev: dict) -> None:
        events.append(ev)

    session.set_callbacks(on_event=on_event)

    await session._handle_notification(
        "item/completed",
        {
            "threadId": "provider-thread-1",
            "turnId": "provider-turn-1",
            "completedAtMs": 123,
            "item": {"id": "item-compact", "type": "contextCompaction"},
        },
    )

    assert events == [{
        "kind": "provider_compaction_breach",
        "source": "item/completed",
        "provider_thread_id": "provider-thread-1",
        "provider_turn_id": "provider-turn-1",
        "provider_item_id": "item-compact",
    }]


async def test_opaque_compaction_content_is_not_labeled_a_readable_summary():
    session = _make_session()
    events: list[dict] = []

    async def on_event(ev: dict) -> None:
        events.append(ev)

    session.set_callbacks(on_event=on_event)

    await session._handle_notification(
        "item/completed",
        {
            "threadId": "provider-thread-1",
            "turnId": "provider-turn-1",
            "item": {
                "id": "item-context-compact",
                "type": "context_compaction",
                "encrypted_content": "opaque-provider-state",
            },
        },
    )

    assert events == [{
        "kind": "provider_compaction_breach",
        "source": "item/completed",
        "provider_thread_id": "provider-thread-1",
        "provider_turn_id": "provider-turn-1",
        "provider_item_id": "item-context-compact",
    }]


async def test_unrelated_notification_name_containing_compact_is_not_a_boundary():
    session = _make_session()
    events: list[dict] = []

    async def on_event(ev: dict) -> None:
        events.append(ev)

    session.set_callbacks(on_event=on_event)

    await session._handle_notification(
        "thread/compactionSettingsUpdated",
        {"threadId": "provider-thread-1"},
    )

    assert events == []


async def test_current_schema_context_compaction_item_emits_breach():
    session = _make_session()
    events: list[dict] = []

    async def on_event(ev: dict) -> None:
        events.append(ev)

    session.set_callbacks(on_event=on_event)

    await session._handle_notification(
        "item/completed",
        {
            "threadId": "provider-thread-2",
            "turnId": "provider-turn-2",
            "item": {
                "id": "item-thread-context-compact",
                "type": "contextCompaction",
            },
        },
    )

    assert events == [{
        "kind": "provider_compaction_breach",
        "source": "item/completed",
        "provider_thread_id": "provider-thread-2",
        "provider_turn_id": "provider-turn-2",
        "provider_item_id": "item-thread-context-compact",
    }]


async def test_turn_steer_uses_request_deadline_without_shortening_other_requests(monkeypatch):
    from subagents import codex_app_server

    session = _make_session()
    session.thread_id = "thread-1"
    session._track_active_turn("turn-1")
    observed_timeouts: list[float] = []
    real_timeout = asyncio.timeout

    class _Stdin:
        def write(self, _payload: bytes) -> None:
            pass

        async def drain(self) -> None:
            pass

    class _Proc:
        stdin = _Stdin()
        returncode = None

        def kill(self):
            self.returncode = -9

    def timeout(delay):
        observed_timeouts.append(delay)
        return real_timeout(0)

    session._proc = _Proc()
    monkeypatch.setattr(codex_app_server.asyncio, "timeout", timeout)

    with pytest.raises(CodexAppServerError, match="turn/steer"):
        await session.steer("queued user input")

    assert observed_timeouts == [codex_app_server.STEER_REQUEST_TIMEOUT]
    assert codex_app_server.STEER_REQUEST_TIMEOUT < codex_app_server.REQUEST_TIMEOUT
    assert session.request_log[-1]["method"] == "turn/steer"
    assert session._pending == {}


async def test_turn_steer_rechecks_dynamic_tool_boundary_at_the_pipe_write():
    session = _make_session()
    session.thread_id = "thread-1"
    session._track_active_turn("turn-1")
    written: list[bytes] = []

    class _Stdin:
        def write(self, payload: bytes) -> None:
            written.append(payload)

        async def drain(self) -> None:
            pass

    class _Proc:
        stdin = _Stdin()
        returncode = None

    session._proc = _Proc()
    await session._write_lock.acquire()
    steer = asyncio.create_task(session.steer("queued user input"))
    await asyncio.sleep(0)
    session._dynamic_tool_requests_idle.clear()
    session._write_lock.release()

    assert await asyncio.wait_for(steer, timeout=0.1) is False
    assert written == []
    assert session.request_log == []
    assert session._pending == {}
