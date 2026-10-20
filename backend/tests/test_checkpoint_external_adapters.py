"""Claude Code and Codex disposable checkpoint adapter contracts."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from core.checkpoint_fork_kind import CHECKPOINT_KIND
from core import checkpoint_cc_commands

from core.checkpoint_types import ProviderCheckpointRequest
from history.types import HistoryBoundary
from llm.resolution import ExecutionTarget
from vendor_account_fixtures import default_receipt, seed_turn_on_default
from native_checkpoint_fixtures import cold_source, seed_vendor_checkpoint_thread


def _target(harness: str) -> ExecutionTarget:
    return ExecutionTarget(
        catalog_id=f"catalog-{harness}",
        harness=harness,
        provider=harness,
        backend="harness",
        transport_model=f"model-{harness}",
        expected_reported_model=f"reported-{harness}",
        context_window=262_144,
        available=True,
        availability_reason=None,
        display_name=harness,
        supports_reasoning=True,
        preserve_thinking_default=True,
    )


def _boundary() -> HistoryBoundary:
    return HistoryBoundary("thread-1", 4, 91, "message-91")


@pytest.fixture
def vendor_thread(isolated_db):
    seed_vendor_checkpoint_thread([_target("claude-code"), _target("codex")])


def _request(target, source, *, binary: str):
    return ProviderCheckpointRequest(
        checkpoint_id="checkpoint-1",
        thread_id="thread-1",
        target=target,
        instruction="Write the current checkpoint.",
        cold_source=cold_source() if source is None else None,
        reasoning_effort="high",
        working_dir="/work/project",
        binary=binary,
        warm_source=source,
    )


def test_cc_tool_boundary_prefix_stops_before_the_current_step() -> None:
    """The warm snapshot excludes the assistant record that owns the live tool."""
    from core import checkpoint_cc_boundary as boundary

    previous_result = {
        "type": "user",
        "message": {"content": [{
            "type": "tool_result",
            "tool_use_id": "toolu-old",
            "content": "previous result",
        }]},
    }
    current_step = {
        "type": "assistant",
        "uuid": "step-current",
        "message": {
            "id": "message-current",
            "content": [{"type": "tool_use", "id": "toolu-current", "name": "exec"}],
        },
    }
    transcript = b"".join(
        json.dumps(record, separators=(",", ":")).encode() + b"\n"
        for record in (previous_result, current_step)
    )

    durable_boundary = boundary.ClaudeTranscriptBoundary(
        role="tool", content="previous result", tool_call_id="toolu-old",
    )
    prefix_bytes = boundary.cc_tool_boundary_prefix_bytes(
        transcript, "toolu-current", durable_boundary,
    )

    assert prefix_bytes == len(
        json.dumps(previous_result, separators=(",", ":")).encode() + b"\n"
    )
    assert b"toolu-old" in transcript[:prefix_bytes]
    assert b"toolu-current" not in transcript[:prefix_bytes]
    assert boundary.cc_tool_boundary_prefix_bytes(
        b"{malformed}\n", "toolu-current", durable_boundary,
    ) is None


def _cc_written_user(uuid: str, text: str) -> dict:
    """The user record Claude Code 2.1.283 writes for one stream-json frame:
    the frame's own ``uuid`` verbatim, plus bookkeeping fields of its own."""
    return {
        "parentUuid": None, "isSidechain": False, "promptId": "prompt-1",
        "type": "user", "message": {"role": "user", "content": text},
        "uuid": uuid, "timestamp": "2026-09-28T11:40:00Z", "sessionId": "live-session",
    }


def _cc_frame_uuid(text: str, row_id: str | None) -> str:
    """The uuid CORSIA's frame carries, or one Claude assigns when it has none."""
    from subagents.cc_stream_frames import build_user_frame

    return json.loads(build_user_frame(text, input_id=row_id)).get("uuid") or "claude-assigned"


def test_cc_boundary_uses_exact_row_identity_not_duplicate_text() -> None:
    """Duplicate display text cannot choose a different canonical provider row."""
    from core import checkpoint_cc_boundary as boundary

    duplicate = _cc_written_user(_cc_frame_uuid("same visible text", "other-row"), "same visible text")
    through = _cc_written_user(_cc_frame_uuid("same visible text", "canonical-row"), "same visible text")
    current = {
        "type": "assistant",
        "message": {
            "id": "current-message",
            "content": [
                {"type": "thinking", "thinking": "private"},
                {"type": "text", "text": "working"},
                {"type": "tool_use", "id": "tool-current-1", "name": "read"},
                {"type": "tool_use", "id": "tool-current-2", "name": "read"},
            ],
        },
    }
    transcript = _rollout_bytes(duplicate, through, current)
    exact = boundary.ClaudeTranscriptBoundary(
        role="user",
        content="same visible text",
        message_id="canonical-row",
    )

    cut = boundary.cc_tool_boundary_prefix_bytes(
        transcript,
        "tool-current-2",
        exact,
        provider_session_id="live-session",
    )

    assert cut == len(_rollout_bytes(duplicate, through))
    assert b"tool-current-1" not in transcript[:cut]
    assert b"tool-current-2" not in transcript[:cut]


def test_cc_user_boundary_without_corsia_uuid_names_the_missing_row() -> None:
    """A frame sent without the anchor row id gets a Claude-assigned uuid, so
    the user boundary cannot be proved and the refusal says which check failed."""
    from core.checkpoint_cc_boundary import ClaudeTranscriptBoundary
    from core.checkpoint_cc_boundary_records import tool_prefix_from_stream

    current = {
        "type": "assistant",
        "message": {"id": "current", "content": [{"type": "tool_use", "id": "t1", "name": "read"}]},
    }
    anchor = ClaudeTranscriptBoundary(role="user", content="hello", message_id="anchor-row")
    unsent = _cc_written_user(_cc_frame_uuid("hello", None), "hello")

    assert tool_prefix_from_stream(
        io.BytesIO(_rollout_bytes(unsent, current)), "t1", anchor,
    ) == "boundary_not_found:user"
    fixed = io.BytesIO(_rollout_bytes(
        _cc_written_user(_cc_frame_uuid("hello", "anchor-row"), "hello"), current,
    ))
    assert tool_prefix_from_stream(fixed, "t1", anchor) == len(_rollout_bytes(
        _cc_written_user("anchor-row", "hello"),
    ))


def test_cc_tool_boundary_uses_call_identity_not_projected_result_text() -> None:
    """Media labels and invocation markers do not change tool-result identity."""
    from core import checkpoint_cc_boundary as boundary

    provider_group = {
        "type": "assistant",
        "message": {
            "id": "provider-step",
            "content": [
                {"type": "thinking", "thinking": "private reasoning"},
                {"type": "text", "text": "visible answer"},
                {"type": "tool_use", "id": "tool-first", "name": "read"},
                {"type": "tool_use", "id": "tool-previous", "name": "read"},
            ],
        },
    }
    through = {
        "type": "user",
        "message": {"content": [
            {
                "type": "tool_result",
                "tool_use_id": "tool-first",
                "content": "first result",
            },
            {
                "type": "tool_result",
                "tool_use_id": "tool-previous",
                "content": [
                    {"type": "text", "text": "projected result\n\n[CORSIA_TOOL_INVOCATION reference={}]"},
                    {"type": "image", "source": {"media_type": "image/png"}},
                ],
            },
        ]},
    }
    current = {
        "type": "assistant",
        "message": {
            "id": "current-message",
            "content": [{"type": "tool_use", "id": "tool-current", "name": "read"}],
        },
    }
    transcript = _rollout_bytes(provider_group, through, current)

    cut = boundary.cc_tool_boundary_prefix_bytes(
        transcript,
        "tool-current",
        boundary.ClaudeTranscriptBoundary(
            role="tool",
            content="canonical unprojected result",
            tool_call_id="tool-previous",
            message_id="canonical-result",
            assistant_step_id="canonical-step",
            tool_call_ids=("tool-first", "tool-previous"),
        ),
        provider_session_id="live-session",
    )

    assert cut == len(_rollout_bytes(provider_group, through))


def test_cc_tool_boundary_prefix_stops_before_all_records_of_current_message() -> None:
    """One Claude assistant step may span thinking, text, and tool-use records."""
    from core import checkpoint_cc_boundary as boundary

    previous_result = {
        "type": "user",
        "message": {"content": [{
            "type": "tool_result",
            "tool_use_id": "toolu-old",
            "content": "previous result",
        }]},
    }
    current_thinking = {
        "type": "assistant",
        "uuid": "step-current",
        "message": {
            "id": "message-current",
            "content": [{"type": "thinking", "thinking": "current step thought"}],
        },
    }
    current_text = {
        "type": "assistant",
        "uuid": "step-current",
        "message": {
            "id": "message-current",
            "content": [{"type": "text", "text": "current step text"}],
        },
    }
    current_tool = {
        "type": "assistant",
        "uuid": "step-current",
        "message": {
            "id": "message-current",
            "content": [{"type": "tool_use", "id": "toolu-current", "name": "exec"}],
        },
    }
    records = (previous_result, current_thinking, current_text, current_tool)
    transcript = b"".join(
        json.dumps(record, separators=(",", ":")).encode() + b"\n"
        for record in records
    )

    durable_boundary = boundary.ClaudeTranscriptBoundary(
        role="tool", content="previous result", tool_call_id="toolu-old",
    )
    prefix_bytes = boundary.cc_tool_boundary_prefix_bytes(
        transcript, "toolu-current", durable_boundary,
    )

    assert prefix_bytes == len(
        json.dumps(previous_result, separators=(",", ":")).encode() + b"\n"
    )
    assert b"current step thought" not in transcript[:prefix_bytes]
    assert b"current step text" not in transcript[:prefix_bytes]
    assert b"toolu-current" not in transcript[:prefix_bytes]
    duplicate_tool = {
        **current_tool,
        "uuid": "duplicate-step",
    }
    ambiguous_transcript = b"".join(
        json.dumps(record, separators=(",", ":")).encode() + b"\n"
        for record in (previous_result, current_tool, duplicate_tool)
    )
    assert (
        boundary.cc_tool_boundary_prefix_bytes(
            ambiguous_transcript, "toolu-current", durable_boundary,
        )
        is None
    )


def test_cc_tool_boundary_path_streams_without_reading_the_full_transcript(
    tmp_path, monkeypatch,
) -> None:
    """The live-lock boundary proof must not materialize a session transcript."""
    from core import checkpoint_cc_boundary as boundary

    older_records = [
        {"type": "user", "message": {"content": f"older result {index}"}}
        for index in range(2_048)
    ]
    durable_record = {
        "type": "user",
        "message": {"content": [{
            "type": "tool_result",
            "tool_use_id": "toolu-old",
            "content": "durable result",
        }]},
    }
    current_record = {
        "type": "assistant",
        "message": {
            "id": "message-current",
            "content": [{"type": "tool_use", "id": "toolu-current", "name": "exec"}],
        },
    }
    prefix = b"".join(
        json.dumps(record, separators=(",", ":")).encode() + b"\n"
        for record in [*older_records, durable_record]
    )
    transcript = tmp_path / "source.jsonl"
    transcript.write_bytes(
        prefix + json.dumps(current_record, separators=(",", ":")).encode() + b"\n"
    )

    def _unexpected_full_read(_path):
        raise AssertionError("boundary proof must stream the transcript")

    monkeypatch.setattr(Path, "read_bytes", _unexpected_full_read)
    assert boundary._prefix_bytes_from_path(
        str(transcript),
        "toolu-current",
        boundary.ClaudeTranscriptBoundary(
            role="tool", content="durable result", tool_call_id="toolu-old",
        ),
    ) == len(prefix)



@pytest.mark.parametrize(
    ("previous", "durable_boundary"),
    (
        (
            {
                "type": "assistant",
                "message": {
                    "id": "previous-assistant",
                    "content": [{"type": "text", "text": "previous answer"}],
                },
            },
            ("assistant", "previous answer", None, "previous-assistant"),
        ),
        (
            {
                "type": "user",
                "uuid": "previous-user",
                "message": {"content": "follow-up input"},
            },
            ("user", "follow-up input", None, "previous-user"),
        ),
    ),
)
def test_cc_tool_boundary_prefix_proves_every_durable_boundary_role(
    previous, durable_boundary,
) -> None:
    """The frozen prefix ends at the exact user, assistant, or tool through row."""
    from core import checkpoint_cc_boundary as boundary

    current = {
        "type": "assistant",
        "message": {
            "id": "current-message",
            "content": [{"type": "tool_use", "id": "toolu-current", "name": "exec"}],
        },
    }
    transcript = b"".join(
        json.dumps(record, separators=(",", ":")).encode() + b"\n"
        for record in (previous, current)
    )
    role, content, tool_call_id, message_id = durable_boundary

    prefix_bytes = boundary.cc_tool_boundary_prefix_bytes(
        transcript,
        "toolu-current",
        boundary.ClaudeTranscriptBoundary(
            role, content, tool_call_id, message_id=message_id
        ),
    )

    assert prefix_bytes == len(
        json.dumps(previous, separators=(",", ":")).encode() + b"\n"
    )


@pytest.mark.asyncio
async def test_cc_durable_capture_survives_process_eviction(tmp_path, monkeypatch):
    """A missing in-memory CC process must not turn a resumable transcript cold."""
    from core import checkpoint_cc_fork as cc
    from subagents.cc_invocation import ClaudeInvocation

    transcript = tmp_path / "source.jsonl"
    transcript.write_bytes(b'{"type":"user","message":{"content":"hello"}}\n')
    seed_file = tmp_path / "seed.txt"
    seed_file.write_text("same seed")
    mcp_config = tmp_path / "tools.json"
    mcp_config.write_text("{}")
    invocation = ClaudeInvocation(
        binary="claude",
        permission_mode="bypassPermissions",
        session_id="source-session",
        seed_file=str(seed_file),
        mcp_config=str(mcp_config),
        working_dir=str(tmp_path),
        model="sonnet",
        effort="high",
    )
    monkeypatch.setattr(cc, "_live_identity", lambda _thread: _async_value(None))
    monkeypatch.setattr(
        cc,
        "_registered_identity",
        lambda _thread: ("source-session", invocation, None, str(transcript)),
    )
    source = await cc.capture_warm_source(
        "thread-1", _boundary(), target=_target("claude-code"), binary="claude"
    )
    assert source is not None
    assert (
        source.transcript_sha256 == hashlib.sha256(transcript.read_bytes()).hexdigest()
    )
    assert source.invocation == invocation


@pytest.mark.asyncio
async def test_cc_capture_refuses_a_short_proven_prefix(tmp_path, monkeypatch):
    from core import checkpoint_cc_fork as cc

    transcript = tmp_path / "source.jsonl"
    transcript.write_bytes(b"proven")
    monkeypatch.setattr(cc, "_live_identity", lambda _thread: _async_value(None))
    monkeypatch.setattr(
        cc,
        "_registered_identity",
        lambda _thread: ("source-session", object(), None, str(transcript)),
    )
    reasons: list[str] = []
    monkeypatch.setattr(
        cc,
        "warm_capture_refused",
        lambda _thread, _harness, reason: reasons.append(reason),
    )

    source = await cc.capture_warm_source(
        "thread-1",
        _boundary(),
        target=_target("claude-code"),
        binary="claude",
        prefix_bytes=len(b"proven") + 1,
    )

    assert source is None
    assert reasons == ["prefix_past_transcript"]


@pytest.mark.asyncio
async def test_cc_registered_capture_fails_closed_after_real_cleanup(tmp_path, monkeypatch):
    """A cleaned registered process cannot warm with only part of its launch."""
    from core import checkpoint_cc_fork as cc
    from subagents import cc_destination
    from subagents.cc_invocation import ClaudeInvocation
    from subagents.cc_runtime_quiescence import cleanup_ephemeral_live

    transcript = tmp_path / "source.jsonl"
    transcript.write_bytes(b'{"type":"user","message":{"content":"hello"}}\n')
    seed_file = tmp_path / "seed.txt"
    seed_file.write_text("same seed")
    mcp_config = tmp_path / "tools.json"
    mcp_config.write_text("{}")
    invocation = ClaudeInvocation(
        binary="claude",
        permission_mode="bypassPermissions",
        session_id="source-session",
        seed_file=str(seed_file),
        mcp_config=str(mcp_config),
        working_dir=str(tmp_path),
        model="sonnet",
        effort="high",
    )
    live = SimpleNamespace(
        session=SimpleNamespace(mcp_config=str(mcp_config)),
        tool_config="",
        tool_registry=object(),
        seed_file=str(seed_file),
    )
    cleanup_ephemeral_live(live)
    assert not seed_file.exists()
    assert not mcp_config.exists()
    assert live.session.mcp_config is None
    monkeypatch.setattr(cc, "_live_identity", lambda _thread: _async_value(None))
    monkeypatch.setattr(
        cc,
        "_registered_identity",
        lambda _thread: ("source-session", invocation, None, str(transcript)),
    )
    monkeypatch.setattr(
        cc_destination,
        "provider_resume_seed",
        lambda *_args: ("source-session", "same seed"),
    )

    source = await cc.capture_warm_source(
        "thread-1", _boundary(), target=_target("claude-code"), binary="claude"
    )

    assert source is None


@pytest.mark.asyncio
async def test_cc_warm_capture_uses_the_already_locked_live_handle(
    tmp_path, monkeypatch,
):
    """Pressure capture must not reacquire CC's non-reentrant live lock."""
    from core import checkpoint_cc_fork as cc
    from subagents.cc_invocation import ClaudeInvocation
    from subagents.cc_runtime_state import _LIVE

    transcript = tmp_path / "source.jsonl"
    transcript.write_bytes(b'{"type":"user","message":{"content":"hello"}}\n')
    seed_file = tmp_path / "seed.txt"
    seed_file.write_text("seed")
    mcp_config = tmp_path / "tools.json"
    mcp_config.write_text("{}")
    invocation = ClaudeInvocation(
        binary="claude",
        permission_mode="bypassPermissions",
        resume_session_id="source-session",
        seed_file=str(seed_file),
        mcp_config=str(mcp_config),
        working_dir=str(tmp_path),
        model="sonnet",
        effort="high",
    )
    live = SimpleNamespace(
        session=SimpleNamespace(invocation=lambda: invocation),
        claude_dir=str(tmp_path),
    )
    monkeypatch.setattr(
        cc,
        "_transcript_path",
        lambda provider_id, claude_dir: (
            str(transcript)
            if provider_id == "source-session" and claude_dir == str(tmp_path)
            else None
        ),
    )

    async with _LIVE.lock("thread-1"):
        source = await asyncio.wait_for(
            cc.capture_warm_source(
                "thread-1",
                _boundary(),
                target=_target("claude-code"),
                binary="claude",
                live=live,
            ),
            timeout=0.1,
        )

    assert source is not None
    assert source.provider_session_id == "source-session"
    assert source.claude_dir == str(tmp_path)


@pytest.mark.asyncio
async def test_cc_initial_boundary_capture_does_not_reacquire_the_transition_lock():
    """The initial-boundary capture runs inside the CC feed, which already holds
    ``_LIVE.lock(thread_id)``. Re-acquiring that non-reentrant lock deadlocked every
    over-window Claude Code thread in production on 2026-09-16."""
    from core import checkpoint_cc_boundary as cc_boundary
    from subagents.cc_runtime_state import _LIVE

    live = SimpleNamespace(session=None, claude_dir=None, tool_step_ids={})
    context = SimpleNamespace(
        target=_target("claude-code"),
        boundary=_boundary(),
        binary="claude",
        warm_boundary=cc_boundary.ClaudeTranscriptBoundary(role="user", content="input"),
    )
    _LIVE.reset()
    _LIVE.put("thread-1", live)
    try:
        async with _LIVE.lock("thread-1"):
            result = await asyncio.wait_for(
                cc_boundary.capture_boundary_warm_source(context, live, "thread-1"),
                timeout=0.5,
            )
        assert result is None  # a bare handle has no provider identity to freeze
        assert _LIVE.get("thread-1") is live
    finally:
        _LIVE.reset()


@pytest.mark.asyncio
async def test_cc_tool_boundary_capture_freezes_matching_row_and_live_launch(
    tmp_path, monkeypatch,
):
    """The idle CC lock captures only the durable prefix and exact launch shape."""
    from core import checkpoint_cc_boundary as cc_boundary
    from core import checkpoint_cc_fork as cc
    from subagents.cc_invocation import ClaudeInvocation
    from subagents.cc_runtime_state import _LIVE

    previous = {
        "type": "user",
        "message": {"content": [{
            "type": "tool_result",
            "tool_use_id": "toolu-old",
            "content": "previous result",
        }]},
    }
    current = {
        "type": "assistant",
        "uuid": "current-step",
        "message": {
            "id": "current-message",
            "content": [{"type": "tool_use", "id": "toolu-current", "name": "exec"}],
        },
    }
    transcript = tmp_path / "source.jsonl"
    transcript.write_bytes(
        b"".join(
            json.dumps(record, separators=(",", ":")).encode() + b"\n"
            for record in (previous, current)
        )
        + json.dumps({"type": "system", "padding": "x" * (4 * 1024 * 1024)}).encode()
        + b"\n"
    )
    assert transcript.stat().st_size > 4 * 1024 * 1024
    seed_file = tmp_path / "seed.txt"
    seed_file.write_text("same seed")
    mcp_config = tmp_path / "tools.json"
    mcp_config.write_text("{}")
    invocation = ClaudeInvocation(
        binary="claude",
        permission_mode="bypassPermissions",
        resume_session_id="source-session",
        seed_file=str(seed_file),
        mcp_config=str(mcp_config),
        working_dir=str(tmp_path),
        model="sonnet",
        effort="high",
    )
    live = SimpleNamespace(
        session=SimpleNamespace(invocation=lambda: invocation),
        claude_dir=str(tmp_path),
        tool_step_ids={},
    )
    boundary = _boundary()
    context = SimpleNamespace(
        target=_target("claude-code"),
        boundary=boundary,
        binary="claude",
        warm_boundary=cc_boundary.ClaudeTranscriptBoundary(
            role="tool", content="previous result", tool_call_id="toolu-old",
        ),
    )
    monkeypatch.setattr(
        cc,
        "_transcript_path",
        lambda provider_id, claude_dir: (
            str(transcript)
            if provider_id == "source-session" and claude_dir == str(tmp_path)
            else None
        ),
    )
    from loguru import logger

    reasons: list[str] = []
    sink = logger.add(
        lambda message: reasons.append(str(message).rsplit(": ", 1)[-1].strip()),
        level="INFO",
        format="{message}",
        filter=lambda record: "warm capture refused" in record["message"],
    )

    async def refusal(tool_call_id: str = "toolu-current") -> str:
        reasons.clear()
        assert await cc_boundary.capture_tool_boundary_warm_source(
            context, live, "thread-1", tool_call_id,
        ) is None
        assert len(reasons) == 1
        return reasons[0]

    _LIVE.reset()
    _LIVE.put("thread-1", live)
    try:
        source = await cc_boundary.capture_tool_boundary_warm_source(
            context, live, "thread-1", "toolu-current",
        )
        assert source is not None
        assert reasons == []
        assert source.boundary == boundary
        assert source.invocation == invocation
        assert source.transcript_bytes == len(
            json.dumps(previous, separators=(",", ":")).encode() + b"\n"
        )
        snapshot_id, snapshot = cc._snapshot(source)
        try:
            assert snapshot.read_bytes() == transcript.read_bytes()[:source.transcript_bytes]
            command = checkpoint_cc_commands.warm_command(
                source,
                _request(_target("claude-code"), source, binary="claude"),
                CHECKPOINT_KIND, snapshot_id,
            )
            assert command[command.index("--append-system-prompt-file") + 1] == str(seed_file)
            assert command[command.index("--mcp-config") + 1] == str(mcp_config)
        finally:
            snapshot.unlink()
        context.warm_boundary = cc_boundary.ClaudeTranscriptBoundary(
            role="tool", content="different durable result", tool_call_id="toolu-other",
        )
        assert await refusal() == "boundary_not_found:tool"
        context.warm_boundary = cc_boundary.ClaudeTranscriptBoundary(
            role="tool", content="previous result", tool_call_id="toolu-old",
        )
        assert await refusal("toolu-unknown") == "current_call_not_found"
        mcp_config.unlink()
        assert await refusal() == "launch_files_missing"
        mcp_config.write_text("{}")
        live.tool_step_ids["toolu-current"] = "step-current"
        assert await refusal() == "tool_calls_outstanding"
        _LIVE.put("thread-1", SimpleNamespace(tool_step_ids={}))
        assert await refusal() == "live_changed"
    finally:
        logger.remove(sink)
        _LIVE.reset()


@pytest.mark.asyncio
async def test_cc_preserves_launch_and_accepts_error_envelope_body(
    tmp_path, monkeypatch, vendor_thread
):
    """CC keeps cwd/model/effort/tools and returns a body even on is_error."""
    from core import checkpoint_cc_fork as cc
    from subagents.cc_invocation import ClaudeInvocation

    transcript = tmp_path / "source.jsonl"
    transcript.write_text('{"type":"user"}\n')
    mcp_config = tmp_path / "tools.json"
    mcp_config.write_text("{}")
    invocation = ClaudeInvocation(
        binary="claude",
        permission_mode="bypassPermissions",
        session_id="source-session",
        mcp_config=str(mcp_config),
        working_dir=str(tmp_path),
        model="sonnet",
        effort="high",
    )
    source = cc.CCWarmSource(
        "thread-1",
        _boundary(),
        "source-session",
        str(transcript),
        transcript.stat().st_size,
        hashlib.sha256(transcript.read_bytes()).hexdigest(),
        invocation,
        None,
        vendor_account_receipt=default_receipt("claude-code"),
    )
    record = {
        "type": "result",
        "is_error": True,
        "result": "usable checkpoint",
        "usage": {"input_tokens": 80, "output_tokens": 20},
    }
    process = FakeProcess((json.dumps(record) + "\n").encode(), returncode=1)
    observed = {}

    async def spawn(argv, *, cwd, env):
        observed.update(argv=argv, cwd=cwd, env=env)
        snapshot_id = argv[argv.index("--resume") + 1]
        snapshot = transcript.with_name(f"{snapshot_id}.jsonl")
        observed["snapshot"] = snapshot.read_bytes()
        return process

    monkeypatch.setattr(cc, "_spawn_process", spawn)
    result = await cc.invoke_checkpoint(
        _request(_target("claude-code"), source, binary="claude"), asyncio.Event(), kind=CHECKPOINT_KIND
    )
    assert result.raw_body == "usable checkpoint"
    assert observed["snapshot"] == transcript.read_bytes()
    assert observed["cwd"] == str(tmp_path)
    assert "--max-turns" not in observed["argv"]
    for expected in (
        "--fork-session",
        "--model",
        "sonnet",
        "--effort",
        "high",
        "--mcp-config",
        str(mcp_config),
    ):
        assert expected in observed["argv"]


def _rollout_bytes(*records: dict) -> bytes:
    return b"".join(
        json.dumps(record, separators=(",", ":")).encode() + b"\n"
        for record in records
    )


def _token_count(*, complete: bool = True) -> dict:
    totals = {"input_tokens": 12, "cached_input_tokens": 3, "output_tokens": 4}
    if not complete:
        totals.pop("output_tokens")
    return {
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {"total_token_usage": totals},
        },
    }


def _codex_tool_step(tool_call_id: str, call_id: str) -> tuple[dict, ...]:
    return (
        {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {"type": "Reasoning", "id": f"reason-{tool_call_id}"},
            },
        },
        {
            "type": "response_item",
            "payload": {"type": "reasoning", "id": f"reason-{tool_call_id}"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "id": f"call-item-{tool_call_id}",
                "call_id": call_id,
                "name": "exec",
                "status": "completed",
            },
        },
        {"type": "token_usage_record", "payload": {}},
        {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {"type": "CommandExecution", "id": tool_call_id},
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "id": f"output-{tool_call_id}",
                "call_id": call_id,
            },
        },
        _token_count(),
    )


def _codex_message_step(role: str, text: str) -> tuple[dict, ...]:
    item = (
        {"type": "agentMessage", "id": f"{role}-{text}", "text": text}
        if role == "assistant"
        else {
            "type": "userMessage",
            "id": f"{role}-{text}",
            "content": [{"type": "text", "text": text}],
        }
    )
    return (
        {
            "type": "event_msg",
            "payload": {"type": "item_completed", "item": item},
        },
        _token_count(),
    )


def _codex_live_turn(anchor_row_id: str) -> tuple[dict, ...]:
    """The rollout codex-cli 0.157.1 wrote for one turn started with
    ``clientUserMessageId``: PascalCase items, the user item keeping our id as
    ``client_id`` beside its own, and no receipt between user and assistant."""
    return (
        {"type": "event_msg", "payload": {"type": "item_completed", "item": {
            "type": "UserMessage", "id": "codex-user-1", "client_id": anchor_row_id,
            "content": [{"type": "text", "text": "hello"}],
        }}},
        {"type": "response_item", "payload": {
            "type": "message", "role": "user", "id": "msg_user_1",
            "content": [{"type": "input_text", "text": "hello"}],
        }},
        {"type": "event_msg", "payload": {"type": "item_completed", "item": {
            "type": "AgentMessage", "id": "codex-agent-1", "text": "hi there",
        }}},
        {"type": "response_item", "payload": {
            "type": "message", "role": "assistant", "id": "codex-agent-1",
            "content": [{"type": "output_text", "text": "hi there"}],
        }},
        _token_count(),
    )


def test_codex_live_rollout_shape_matches_pascal_items_and_client_id() -> None:
    """A real-shape rollout proves the assistant cut; a user-row cut finds the
    anchor by ``client_id`` but still refuses for want of a receipt."""
    from llm.codex_rollout_boundary import CodexRolloutBoundary, codex_rollout_boundary_cut

    turn = _codex_live_turn("anchor-row")
    next_turn = _codex_live_turn("next-row")[:1]
    rollout = _rollout_bytes(*turn, *next_turn)

    assistant = CodexRolloutBoundary(
        role="assistant", content="hi there", message_id="row-a",
        provider_message_id="codex-agent-1",
    )
    assert codex_rollout_boundary_cut(io.BytesIO(rollout), assistant) == len(
        _rollout_bytes(*turn)
    )
    user = CodexRolloutBoundary(role="user", content="hello", message_id="anchor-row")
    assert codex_rollout_boundary_cut(io.BytesIO(rollout), user) == "receipt_missing:user"
    unsent = CodexRolloutBoundary(role="user", content="hello", message_id="other-row")
    assert codex_rollout_boundary_cut(
        io.BytesIO(_rollout_bytes(*turn)), unsent,
    ) == "boundary_not_found:user"

def test_codex_tool_boundary_prefix_ends_after_previous_group_output() -> None:
    from llm.codex_rollout_boundary import (
        CodexRolloutBoundary,
        codex_rollout_boundary_prefix_bytes,
    )

    previous = _codex_tool_step("exec-previous", "call-previous")
    current = _codex_tool_step("exec-current", "call-current")
    rollout = _rollout_bytes(*previous, *current[:3])

    cut = codex_rollout_boundary_prefix_bytes(
        io.BytesIO(rollout),
        CodexRolloutBoundary(
            role="tool",
            content="",
            tool_call_id="exec-previous",
        ),
    )

    assert cut == len(_rollout_bytes(*previous))
    assert rollout[:cut].endswith(_rollout_bytes(previous[-1]))
    assert rollout[cut:] == _rollout_bytes(*current[:3])


def test_codex_tool_boundary_prefix_cuts_before_settled_suffix() -> None:
    from llm.codex_rollout_boundary import (
        CodexRolloutBoundary,
        codex_rollout_boundary_prefix_bytes,
    )

    boundary = _codex_tool_step("exec-boundary", "call-boundary")
    settled_one = _codex_tool_step("exec-settled-one", "call-settled-one")
    settled_two = _codex_tool_step("exec-settled-two", "call-settled-two")
    settled_three = _codex_tool_step("exec-settled-three", "call-settled-three")
    blocked = _codex_tool_step("exec-blocked", "call-blocked")[:3]
    rollout = _rollout_bytes(
        *boundary,
        *settled_one,
        *settled_two,
        *settled_three,
        *blocked,
    )

    cut = codex_rollout_boundary_prefix_bytes(
        io.BytesIO(rollout),
        CodexRolloutBoundary(
            role="tool",
            content="",
            tool_call_id="exec-boundary",
        ),
    )

    assert cut == len(_rollout_bytes(*boundary))
    assert rollout[:cut].endswith(_rollout_bytes(boundary[-1]))
    assert rollout[cut:].startswith(_rollout_bytes(settled_one[0]))


def test_codex_rollout_cut_ignores_a_large_history_head(tmp_path, monkeypatch) -> None:
    from llm import codex_rollout_boundary as rollout_boundary
    from llm.codex_rollout_boundary import CodexRolloutBoundary

    through = _codex_message_step("assistant", "recorded boundary")
    current = _codex_tool_step("exec-current", "call-current")[:3]
    history_line = _rollout_bytes({
        "type": "response_item",
        "payload": {"type": "reasoning", "text": "x" * 256},
    })
    rollout = tmp_path / "large-rollout.jsonl"
    with rollout.open("wb") as stream:
        for _ in range(20_000):
            stream.write(history_line)
        stream.write(_rollout_bytes(*through, *current))

    parsed = 0
    strict_json_loads = rollout_boundary._strict_json_loads

    def count_parsed_records(line: bytes):
        nonlocal parsed
        parsed += 1
        return strict_json_loads(line)

    monkeypatch.setattr(rollout_boundary, "_strict_json_loads", count_parsed_records)
    with rollout.open("rb") as stream:
        cut = rollout_boundary.codex_rollout_boundary_prefix_bytes(
            stream,
            CodexRolloutBoundary(
                role="assistant",
                content="recorded boundary",
                message_id="assistant-recorded boundary",
            ),
        )

    assert cut == 20_000 * len(history_line) + len(_rollout_bytes(*through))
    assert parsed < 100


@pytest.mark.parametrize("role", ("assistant", "tool"))
def test_codex_rollout_cut_names_a_boundary_outside_the_bounded_tail(
    monkeypatch, role: str,
) -> None:
    from llm import codex_rollout_boundary as rollout_boundary
    from llm.codex_rollout_boundary import CodexRolloutBoundary, codex_rollout_boundary_cut

    tail = _codex_tool_step("tool-current", "call-current")[:3]
    monkeypatch.setattr(
        rollout_boundary, "_ROLLOUT_TAIL_SCAN_BYTES", len(_rollout_bytes(*tail)),
    )
    if role == "assistant":
        through = _codex_message_step("assistant", "outside tail")
        boundary = CodexRolloutBoundary(
            role=role, content="outside tail", message_id="assistant-outside tail",
        )
    else:
        through = _codex_tool_step("tool-outside-tail", "call-outside-tail")
        boundary = CodexRolloutBoundary(
            role=role, content="", tool_call_id="tool-outside-tail",
        )

    assert codex_rollout_boundary_cut(
        io.BytesIO(_rollout_bytes(*through, *tail)), boundary,
    ) == f"boundary_outside_tail:{role}"


@pytest.mark.parametrize("role", ("assistant", "tool"))
def test_codex_rollout_cut_names_a_missing_boundary_after_full_scan(
    monkeypatch, role: str,
) -> None:
    from llm import codex_rollout_boundary as rollout_boundary
    from llm.codex_rollout_boundary import CodexRolloutBoundary, codex_rollout_boundary_cut

    rollout = _rollout_bytes(*_codex_tool_step("tool-current", "call-current")[:3])
    monkeypatch.setattr(rollout_boundary, "_ROLLOUT_TAIL_SCAN_BYTES", len(rollout))
    if role == "assistant":
        boundary = CodexRolloutBoundary(
            role=role, content="missing", message_id="assistant-missing",
        )
    else:
        boundary = CodexRolloutBoundary(
            role=role, content="", tool_call_id="tool-missing",
        )

    assert codex_rollout_boundary_cut(
        io.BytesIO(rollout), boundary,
    ) == f"boundary_not_found:{role}"


def test_codex_boundary_uses_exact_item_identity_not_duplicate_text() -> None:
    from llm.codex_rollout_boundary import (
        CodexRolloutBoundary,
        codex_rollout_boundary_prefix_bytes,
    )

    through = _codex_message_step("assistant", "same visible text")
    through[0]["payload"]["item"]["id"] = "canonical-agent-item"
    duplicate = _codex_message_step("assistant", "same visible text")
    duplicate[0]["payload"]["item"]["id"] = "other-agent-item"
    rollout = _rollout_bytes(*through, *duplicate)

    cut = codex_rollout_boundary_prefix_bytes(
        io.BytesIO(rollout),
        CodexRolloutBoundary(
            role="assistant",
            content="same visible text",
            message_id="canonical-agent-item",
        ),
    )

    assert cut == len(_rollout_bytes(*through))


def test_codex_boundary_skips_a_token_usage_record_before_the_receipt() -> None:
    """0.155.1 order: message -> token_usage_record -> token_count -> next."""
    from llm.codex_rollout_boundary import (
        CodexRolloutBoundary,
        codex_rollout_boundary_prefix_bytes,
    )

    text = "0.155 boundary"
    match = _codex_message_step("assistant", text)[0]
    tail = (match, {"type": "token_usage_record", "payload": {}}, _token_count())
    current = _codex_tool_step("exec-current", "call-current")[:3]
    rollout = _rollout_bytes(*tail, *current)

    cut = codex_rollout_boundary_prefix_bytes(
        io.BytesIO(rollout),
        CodexRolloutBoundary(
            role="assistant",
            content=text,
            message_id=f"assistant-{text}",
        ),
    )

    assert cut == len(_rollout_bytes(*tail))
    assert rollout[cut:] == _rollout_bytes(*current)


def test_codex_boundary_token_usage_record_without_token_count_stays_cold() -> None:
    """A token_usage_record with no following token_count proves nothing."""
    from llm.codex_rollout_boundary import (
        CodexRolloutBoundary,
        codex_rollout_boundary_prefix_bytes,
    )

    text = "unproven boundary"
    match = _codex_message_step("assistant", text)[0]
    rollout = _rollout_bytes(
        match,
        {"type": "token_usage_record", "payload": {}},
        {"type": "response_item", "payload": {"type": "reasoning", "id": "next"}},
    )

    cut = codex_rollout_boundary_prefix_bytes(
        io.BytesIO(rollout),
        CodexRolloutBoundary(
            role="assistant",
            content=text,
            message_id=f"assistant-{text}",
        ),
    )

    assert cut is None


def test_codex_boundary_returns_a_post_receipt_token_usage_record_offset() -> None:
    """A token_usage_record after the receipt still terminates the scan there."""
    from llm.codex_rollout_boundary import (
        CodexRolloutBoundary,
        codex_rollout_boundary_prefix_bytes,
    )

    text = "post-receipt boundary"
    match = _codex_message_step("assistant", text)[0]
    tail = (match, _token_count())
    trailing_usage = {"type": "token_usage_record", "payload": {}}
    rollout = _rollout_bytes(*tail, trailing_usage)

    cut = codex_rollout_boundary_prefix_bytes(
        io.BytesIO(rollout),
        CodexRolloutBoundary(
            role="assistant",
            content=text,
            message_id=f"assistant-{text}",
        ),
    )

    assert cut == len(_rollout_bytes(*tail))
    assert rollout[cut:] == _rollout_bytes(trailing_usage)


def test_codex_tool_boundary_skips_a_token_usage_record_before_the_receipt() -> None:
    """0.155.1 tool order: output -> token_usage_record -> token_count -> next call."""
    from llm.codex_rollout_boundary import (
        CodexRolloutBoundary,
        codex_rollout_boundary_prefix_bytes,
    )

    boundary_step = _codex_tool_step("exec-boundary", "call-boundary")
    boundary = (
        *boundary_step[:-1],
        {"type": "token_usage_record", "payload": {}},
        boundary_step[-1],
    )
    next_call = {
        "type": "response_item",
        "payload": {
            "type": "custom_tool_call",
            "id": "call-item-next",
            "call_id": "call-next",
            "name": "exec",
            "status": "completed",
        },
    }
    rollout = _rollout_bytes(*boundary, next_call)

    cut = codex_rollout_boundary_prefix_bytes(
        io.BytesIO(rollout),
        CodexRolloutBoundary(
            role="tool",
            content="",
            tool_call_id="exec-boundary",
        ),
    )

    assert cut == len(_rollout_bytes(*boundary))
    assert rollout[cut:] == _rollout_bytes(next_call)


@pytest.mark.parametrize("role", ("assistant", "user"))
def test_codex_rollout_cut_ends_at_the_recorded_non_tool_through_row(
    role: str,
) -> None:
    from llm.codex_rollout_boundary import (
        CodexRolloutBoundary,
        codex_rollout_boundary_prefix_bytes,
    )

    text = f"recorded {role} row"
    through = _codex_message_step(role, text)
    current = _codex_tool_step("exec-current", "call-current")
    rollout = _rollout_bytes(*through, *current[:3])

    cut = codex_rollout_boundary_prefix_bytes(
        io.BytesIO(rollout),
        CodexRolloutBoundary(
            role=role,
            content=text,
            message_id=f"{role}-{text}",
        ),
    )

    assert cut == len(_rollout_bytes(*through))
    assert rollout[:cut].endswith(_rollout_bytes(through[-1]))
    assert rollout[cut:] == _rollout_bytes(*current[:3])


@pytest.mark.parametrize(
    ("rollout", "expected_tool_call_id"),
    (
        pytest.param(
            _rollout_bytes(*_codex_tool_step("exec-other", "call-other")),
            "exec-missing",
            id="missing-id",
        ),
        pytest.param(
            _rollout_bytes(*_codex_tool_step("exec-previous", "call-previous"))
            + b"{malformed\n",
            "exec-previous",
            id="malformed-record",
        ),
        pytest.param(
            _rollout_bytes(
                *_codex_tool_step("exec-previous", "call-previous")[:-1],
                *_codex_tool_step("exec-current", "call-current")[:3],
            ),
            "exec-previous",
            id="missing-trailing-token-count",
        ),
        pytest.param(
            _rollout_bytes(
                *_codex_tool_step("exec-previous", "call-previous")[:-1],
                _token_count(complete=False),
                *_codex_tool_step("exec-current", "call-current")[:3],
            ),
            "exec-previous",
            id="incomplete-trailing-token-count",
        ),
        pytest.param(
            _rollout_bytes(*_codex_tool_step("exec-current", "call-current")),
            "exec-current",
            id="expected-output-is-current-step",
        ),
    ),
)
def test_codex_tool_boundary_prefix_fails_closed(
    rollout: bytes,
    expected_tool_call_id: str,
) -> None:
    from llm.codex_rollout_boundary import (
        CodexRolloutBoundary,
        codex_rollout_boundary_prefix_bytes,
    )

    assert codex_rollout_boundary_prefix_bytes(
        io.BytesIO(rollout),
        CodexRolloutBoundary(
            role="tool",
            content="",
            tool_call_id=expected_tool_call_id,
        ),
    ) is None


@pytest.mark.asyncio
async def test_codex_durable_capture_and_exact_rollout_resume(tmp_path, monkeypatch, vendor_thread):
    """Codex resumes a frozen durable rollout after its process was evicted."""
    from core import checkpoint_codex_fork as codex

    home = tmp_path / "codex-home"
    monkeypatch.setattr("llm.codex_rollout_files.default_codex_sessions_root", lambda: home / "sessions")
    rollout = home / "sessions" / "2026" / "08" / "26" / "rollout-source.jsonl"
    rollout.parent.mkdir(parents=True)
    provider_id = "11111111-1111-4111-8111-111111111111"
    rollout.write_text("\n".join((
        json.dumps({
            "type": "session_meta",
            "payload": {"id": provider_id, "cwd": str(tmp_path)},
        }),
        json.dumps({
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": {
                    "input_tokens": 1_000,
                    "cached_input_tokens": 800,
                    "output_tokens": 100,
                }},
            },
        }),
        "",
    )))
    (home / "config.toml").write_text('model_verbosity = "low"\n')
    from core.checkpoint_codex_root import RegisteredRollout

    receipt = seed_turn_on_default("thread-1", _target("codex").catalog_id)
    monkeypatch.setattr(
        codex,
        "_registered_rollout",
        lambda _thread: RegisteredRollout(provider_id, rollout, "high", receipt),
    )
    source = await codex.capture_warm_source(
        "thread-1", _boundary(), target=_target("codex"), binary="codex"
    )
    assert source is not None
    assert receipt is not None and source.vendor_account_receipt == receipt

    from infra.provider_usage import TokenUsage

    observed = {}

    class Session:
        def __init__(self, **kwargs):
            observed.update(kwargs)
            self.on_event = kwargs["on_event"]

        async def start(self):
            copied = next(
                (Path(observed["codex_home"]) / "sessions").rglob("*.jsonl")
            )
            observed["rollout"] = copied.read_bytes()
            await self.on_event({
                "kind": "assistant_text", "text": "codex checkpoint",
            })
            await self.on_event({
                "kind": "result",
                "token_volume": TokenUsage(
                    input_uncached=100, cache_read=200, output=40,
                ),
            })
            return True

        async def terminate(self):
            return None

    monkeypatch.setattr(
        "subagents.codex_app_server.CodexAppServerSession", Session,
    )
    result = await codex.invoke_checkpoint(
        _request(_target("codex"), source, binary="codex"), asyncio.Event(), kind=CHECKPOINT_KIND
    )
    assert result.raw_body == "codex checkpoint"
    assert result.usage == {
        "prompt_tokens": 100,
        "cache_read_tokens": 200,
        "cache_creation_tokens": 0,
        "completion_tokens": 40,
        "effective_input_tokens": 300,
        "total_tokens": 340,
    }
    assert observed["rollout"] == rollout.read_bytes()
    assert observed["working_dir"] == str(tmp_path)
    assert observed["codex_thread_id"] == provider_id
    assert observed["model"] == "model-codex"
    assert observed["effort"] == "high"


@pytest.mark.asyncio
async def test_codex_warm_capture_yields_during_rollout_discovery(
    tmp_path, monkeypatch,
) -> None:
    """Rollout registration and path discovery stay off the event loop."""
    from core import checkpoint_codex_fork as codex

    rollout = tmp_path / "sessions" / "2026" / "09" / "04" / "rollout.jsonl"
    rollout.parent.mkdir(parents=True)
    provider_id = "11111111-1111-4111-8111-111111111111"
    rollout.write_text(json.dumps({
        "type": "session_meta",
        "payload": {"id": provider_id, "cwd": str(tmp_path)},
    }) + "\n")
    started = threading.Event()
    release = threading.Event()

    def blocked_registration(_thread_id):
        started.set()
        assert release.wait(timeout=1)
        from core.checkpoint_codex_root import RegisteredRollout

        return RegisteredRollout(provider_id, rollout, "high", None)

    monkeypatch.setattr(codex, "_registered_rollout", blocked_registration)
    timer = threading.Timer(0.5, release.set)
    timer.start()
    capture = asyncio.create_task(codex.capture_warm_source(
        "thread-1", _boundary(), target=_target("codex"), binary="codex",
    ))
    try:
        while not started.is_set():
            await asyncio.sleep(0)
        assert not capture.done()
        release.set()
        assert await capture is not None
    finally:
        release.set()
        timer.cancel()


@pytest.mark.asyncio
async def test_codex_cold_writer_thread_arms_managed_tool_receipts(tmp_path, vendor_thread):
    """The cold writer's fresh thread must yield the rollout its receipts read.

    Codex reports no ``thread.path`` for an ephemeral thread/start (verified on
    codex-cli 0.154.0), and managed tool receipts are read from that rollout.
    Disposability comes from the writer's temporary CODEX_HOME, not from
    ``ephemeral`` (#1934).
    """
    from core import checkpoint_codex_fork as codex

    request = replace(
        _request(_target("codex"), None, binary="codex"),
        tool_registry=object(), dynamic_tool_specs=({
            "type": "function", "name": "write_file",
            "description": "write", "inputSchema": {"type": "object"},
        },),
    )
    isolated_home = tmp_path / "isolated"
    from core.checkpoint_account import admit_checkpoint
    from connections.account_operation import prepare_operation_home

    request = prepare_operation_home(admit_checkpoint(request), isolated_home, kind='checkpoint')
    session = codex._checkpoint_session(
        request, None, source_home=tmp_path / "source", isolated_home=isolated_home,
        cwd=str(tmp_path), on_event=None, on_exit=None,
        checkpoint_tool_result_fits=lambda *_args: True,
        kind=CHECKPOINT_KIND,
    )
    rollout = isolated_home / "sessions" / "rollout-writer.jsonl"

    async def app_server(method, params):
        assert method == "thread/start"
        thread = {"id": "writer-thread", "ephemeral": params.get("ephemeral") is True}
        if not thread["ephemeral"]:
            thread["path"] = str(rollout)
        return {"thread": thread}

    session._request = app_server
    await session._start_or_resume_thread()

    await session._tool_requests.prepare_turn()
    assert session._tool_requests.rollout_path == str(rollout)


@pytest.mark.asyncio
async def test_codex_cold_checkpoint_starts_callback_session_in_disposable_home(
    tmp_path, monkeypatch, vendor_thread
):
    """Cold checkpoint uses the callback-capable session in a temporary home."""
    from core import checkpoint_codex_fork as codex

    observed = {}

    class Session:
        def __init__(self, **kwargs):
            observed.update(kwargs)
            self.on_event = kwargs["on_event"]

        async def start(self):
            await self.on_event({"kind": "assistant_text", "text": "cold checkpoint"})
            await self.on_event({"kind": "result", "token_volume": None})
            return True

        async def terminate(self):
            return None

    monkeypatch.setattr(
        "subagents.codex_app_server.CodexAppServerSession", Session,
    )

    result = await codex.invoke_checkpoint(
        _request(_target("codex"), None, binary="codex"), asyncio.Event(), kind=CHECKPOINT_KIND
    )

    assert result.raw_body == "cold checkpoint"
    assert observed["codex_thread_id"] is None
    assert observed["ephemeral"] is False
    assert Path(observed["codex_home"]).name.startswith("corsia-codex-checkpoint-")
    assert not Path(observed["codex_home"]).exists()  # Removed with its rollout.
    assert "cold history" in observed["task"]


@pytest.mark.asyncio
async def test_codex_checkpoint_uses_callback_capable_app_server(
    tmp_path, monkeypatch, vendor_thread,
):
    """Checkpoint tools must execute through the ordinary Codex callback bridge."""
    from core import checkpoint_codex_fork as codex
    from infra.provider_usage import TokenUsage

    home = tmp_path / "home"
    monkeypatch.setattr("llm.codex_rollout_files.default_codex_sessions_root", lambda: home / "sessions")
    rollout = home / "sessions" / "rollout.jsonl"
    rollout.parent.mkdir(parents=True)
    provider_id = "33333333-3333-4333-8333-333333333333"
    rollout.write_text(json.dumps({
        "type": "session_meta",
        "payload": {"id": provider_id, "cwd": str(tmp_path)},
    }) + "\n")
    source = codex.CodexWarmSource(
        "thread-1",
        _boundary(),
        provider_id,
        str(rollout),
        rollout.stat().st_size,
        hashlib.sha256(rollout.read_bytes()).hexdigest(),
        str(home / "sessions"),
        str(tmp_path),
        "high",
        vendor_account_receipt=default_receipt("codex"),
    )
    observed = {}

    class Session:
        def __init__(self, **kwargs):
            observed.update(kwargs)
            self.on_event = kwargs["on_event"]
            self.terminated = 0

        async def start(self):
            await self.on_event({"kind": "assistant_text", "text": "checkpoint body"})
            await self.on_event({
                "kind": "result",
                "token_volume": TokenUsage(
                    input_uncached=300,
                    cache_read=200,
                    output=40,
                ),
            })
            return True

        async def terminate(self):
            self.terminated += 1

    registry = object()
    definitions = ({
        "type": "function",
        "function": {
            "name": "continuity_edit",
            "description": "edit continuity",
            "parameters": {"type": "object"},
        },
    },)
    monkeypatch.setattr("subagents.codex_app_server.CodexAppServerSession", Session)
    monkeypatch.setattr(
        "subagents.harness_tool_bridge.build_tool_registry",
        lambda _working_dir: registry,
    )
    monkeypatch.setattr(
        "subagents.harness_tool_bridge.dynamic_tool_definitions",
        lambda _registry: definitions,
    )
    result = await codex.invoke_checkpoint(
        _request(_target("codex"), source, binary="codex"), asyncio.Event(), kind=CHECKPOINT_KIND
    )

    assert result.raw_body == "checkpoint body"
    assert result.usage == {
        "prompt_tokens": 300,
        "cache_read_tokens": 200,
        "cache_creation_tokens": 0,
        "completion_tokens": 40,
        "effective_input_tokens": 500,
        "total_tokens": 540,
    }
    assert observed["checkpoint_id"] == "checkpoint-1"
    assert observed["corsia_thread_id"] == "thread-1"
    assert observed["codex_thread_id"] == provider_id
    assert observed["source_codex_home"] == str(home)
    assert observed["tool_registry"] is registry
    assert observed["dynamic_tools"] == definitions


@pytest.mark.asyncio
async def test_checkpoint_session_runs_dynamic_tool_without_foreground_step(
    tmp_path, monkeypatch, vendor_thread,
) -> None:
    """The real checkpoint session keeps tool calls outside foreground lineage."""
    from unittest.mock import AsyncMock

    from core import checkpoint_codex_fork as codex

    request = replace(
        _request(_target("codex"), None, binary="codex"),
        tool_registry=object(), dynamic_tool_specs=(),
    )
    from core.checkpoint_account import admit_checkpoint
    from connections.account_operation import prepare_operation_home

    request = prepare_operation_home(admit_checkpoint(request), tmp_path / "isolated", kind='checkpoint')
    session = codex._checkpoint_session(
        request, None,
        source_home=tmp_path / "source", isolated_home=tmp_path / "isolated",
        cwd=str(tmp_path), on_event=None, on_exit=None,
        kind=CHECKPOINT_KIND,
    )
    execute = AsyncMock(return_value="checkpoint tool result")
    monkeypatch.setattr("subagents.harness_tool_bridge.execute_tool", execute)

    assert session.declared_tool_step_ids == {}
    result, error = await session._handle_dynamic_tool_request({
        "tool": "continuity_edit", "arguments": {}, "callId": "checkpoint-call",
    })

    assert error is None and result["success"] is True
    call = execute.await_args
    assert call is not None
    assert call.kwargs["assistant_step_id"] is None
    assert call.kwargs["checkpoint_id"] == "checkpoint-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["cc", "codex"])
async def test_checkpoint_cancel_terminates_exact_provider_handle(
    adapter, tmp_path, monkeypatch, vendor_thread
):
    """Stop terminates the running process and no elapsed deadline fires."""
    process = None
    sessions = []
    # Cancel only once the provider handle exists; a fixed sleep raced it on a
    # loaded runner and cancelled before any handle was created.
    started = asyncio.Event()
    if adapter == "cc":
        from core import checkpoint_cc_fork as module
        from subagents.cc_invocation import ClaudeInvocation

        transcript = tmp_path / "source.jsonl"
        transcript.write_text('{"type":"user"}\n')
        invocation = ClaudeInvocation(
            binary="harness",
            permission_mode="bypassPermissions",
            session_id="source",
            working_dir=str(tmp_path),
            model="model",
            effort="high",
        )
        source = module.CCWarmSource(
            "thread-1",
            _boundary(),
            "source",
            str(transcript),
            transcript.stat().st_size,
            hashlib.sha256(transcript.read_bytes()).hexdigest(),
            invocation,
            None,
            vendor_account_receipt=default_receipt("claude-code"),
        )
        request = _request(_target("claude-code"), source, binary="harness")
        process = WaitingProcess()

        def _spawn(*_a, **_kw):
            started.set()
            return _async_value(process)

        monkeypatch.setattr(module, "_spawn_process", _spawn)
    else:
        from core import checkpoint_codex_fork as module

        home = tmp_path / "home"
        monkeypatch.setattr("llm.codex_rollout_files.default_codex_sessions_root", lambda: home / "sessions")
        rollout = home / "sessions" / "rollout.jsonl"
        rollout.parent.mkdir(parents=True)
        provider_id = "22222222-2222-4222-8222-222222222222"
        rollout.write_text(
            json.dumps(
                {
                    "type": "session_meta",
                    "payload": {"id": provider_id, "cwd": str(tmp_path)},
                }
            )
            + "\n"
        )
        source = module.CodexWarmSource(
            "thread-1",
            _boundary(),
            provider_id,
            str(rollout),
            rollout.stat().st_size,
            hashlib.sha256(rollout.read_bytes()).hexdigest(),
            str(home / "sessions"),
            str(tmp_path),
            "high",
            vendor_account_receipt=default_receipt("codex"),
        )
        request = _request(_target("codex"), source, binary="harness")

        class Session:
            def __init__(self, **_kwargs):
                self.terminated = 0
                sessions.append(self)

            async def start(self):
                started.set()
                await asyncio.Future()

            async def terminate(self):
                self.terminated += 1

        monkeypatch.setattr(
            "subagents.codex_app_server.CodexAppServerSession", Session,
        )
        monkeypatch.setattr(
            "subagents.harness_tool_bridge.build_tool_registry", lambda _cwd: object(),
        )
        monkeypatch.setattr(
            "subagents.harness_tool_bridge.dynamic_tool_definitions",
            lambda _registry: (),
        )

    cancel = asyncio.Event()
    operation = asyncio.create_task(module.invoke_checkpoint(request, cancel, kind=CHECKPOINT_KIND))
    await asyncio.wait_for(started.wait(), timeout=30)
    assert not operation.done()
    cancel.set()
    with pytest.raises(asyncio.CancelledError):
        await operation
    if adapter == "cc":
        assert process is not None and process.terminated == 1
    else:
        assert len(sessions) == 1 and sessions[0].terminated == 1


async def _async_value(value):
    return value


class FakeProcess:
    def __init__(self, stdout: bytes, *, returncode: int = 0, stderr=b"provider stderr", context_model="reported-claude-code"):
        self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
        self.stdin = self
        self._stdout, self._stderr, self._exitcode = stdout, stderr, returncode
        self._done = asyncio.Event()
        self.returncode = None
        self.terminated = 0
        self.communicated_input = None
        self.context_model = context_model

    def write(self, data):
        self.communicated_input = data
        frame = json.loads(data)
        if frame.get("type") == "control_request":
            self.stdout.feed_data(json.dumps({"type": "control_response", "response": {
                "subtype": "success", "request_id": frame["request_id"], "response": {
                    "totalTokens": 1000, "maxTokens": 262144, "model": self.context_model,
                    "categories": [{"name": "MCP tools", "kind": "used", "tokens": 50}],
                },
            }}).encode() + b"\n")

    async def drain(self):
        pass

    def close(self):
        self._finish(self._exitcode)

    def _finish(self, code):
        if self.returncode is not None:
            return
        self.returncode = code
        self.stdout.feed_data(self._stdout)
        self.stderr.feed_data(self._stderr)
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self._done.set()

    def terminate(self):
        self.terminated += 1
        self._finish(-15)

    async def wait(self):
        await self._done.wait()
        return self.returncode


class WaitingProcess(FakeProcess):
    def __init__(self):
        super().__init__(b"", returncode=0)
        self.returncode = None
        self._done = asyncio.Event()

    def close(self):
        pass  # Closing stdin must not finish this intentionally waiting process.
