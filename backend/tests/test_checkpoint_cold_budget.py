"""Cold construction cannot guess away capacity or omit the writer's tools."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from core.checkpoint_fork_kind import CHECKPOINT_KIND

from core.checkpoint_fit import ColdFoldInputTooLarge
from core.checkpoint_render import build_cold_checkpoint_admission
from history.types import (
    PORTABLE_HISTORY_FORMAT_VERSION,
    HistoryBoundary,
    PortableHistoryProjection,
    PortableMessage,
    PortableTurn,
)
from llm.resolution import ExecutionTarget
from llm.router import LLMResponse


def _admission(monkeypatch, harness: str, *, window: int = 131_072):
    """Keep real history construction; isolate only the continuity DB read."""
    monkeypatch.setattr("infra.continuity_store.active_entries", lambda *_: {})
    boundary = HistoryBoundary("cold-budget-thread", 1, 1, "source-message")
    message = PortableMessage(
        message_id="source-message",
        seq=1,
        turn_id="source-turn",
        role="user",
        content="The synthetic review is complete. No external action is authorized.",
        created_at="2026-09-11T00:00:00Z",
    )
    projection = PortableHistoryProjection(
        format_version=PORTABLE_HISTORY_FORMAT_VERSION,
        thread_id=boundary.thread_id,
        history_epoch=1,
        start_exclusive=None,
        sealed_boundary=boundary,
        turns=(PortableTurn("source-turn", "completed", (message,), boundary),),
        full_raw_replay_available=True,
    )
    target = ExecutionTarget(
        catalog_id=f"cold-budget-{harness}",
        harness=harness,
        provider=harness,
        backend="harness" if harness != "native" else "local",
        transport_model="haiku" if harness == "claude-code" else "test-model",
        expected_reported_model=(
            "claude-haiku-4-5-20251001" if harness == "claude-code" else None
        ),
        context_window=window,
        available=True,
        availability_reason=None,
        display_name=harness,
        supports_reasoning=False,
    )
    return build_cold_checkpoint_admission(
        checkpoint_id="cold-budget-checkpoint",
        thread_id=boundary.thread_id,
        trigger="manual",
        source_target=target,
        writer_target=target,
        boundary=boundary,
        previous=None,
        projection=projection,
        working_dir="/work/project",
        reasoning_effort=None,
        binary=None if harness == "native" else "claude",
        db=object(),
    )


@pytest.fixture
def seeded_cc_thread(isolated_db, monkeypatch):
    """The durable thread and account the real paid-write admission reads."""
    from native_checkpoint_fixtures import seed_vendor_checkpoint_thread

    target = _admission(monkeypatch, "claude-code").provider_request.target
    seed_vendor_checkpoint_thread([target], thread_id="cold-budget-thread")


def test_haiku_small_source_reaches_writer_surface_admission(monkeypatch):
    """A guessed 131k envelope must not reject before the actual writer loads."""
    admission = _admission(monkeypatch, "claude-code")

    assert admission.provider_request.target.context_window == 131_072
    assert admission.request.through_message_id == "source-message"


def test_cold_prior_carries_narrative_without_continuity_bodies() -> None:
    from core.checkpoint_render import _cold_prior_narrative

    previous = SimpleNamespace(
        raw_result="prior narrative",
        error=None,
        continuity_snapshot_json='[{"body":"SECRET CONTINUITY"}]',
    )

    rendered = _cold_prior_narrative(previous)

    assert "prior narrative" in rendered
    assert "SECRET CONTINUITY" not in rendered


@pytest.mark.asyncio
async def test_native_oversized_tool_surface_never_reaches_provider(monkeypatch):
    """Ignoring tool schemas during cold fit must not permit an oversized write."""
    from core import checkpoint_native_fork as native

    definition = {
        "type": "function",
        "function": {
            # The one tool a cold fork ever advertises (#2116), so the
            # selector keeps it and its bloated schema must still be counted.
            "name": "continuity_edit",
            "description": "X" * 150_000,
            "parameters": {"type": "object", "properties": {}},
        },
    }
    tools = SimpleNamespace(get_definitions=lambda: [definition])
    writes: list[dict] = []

    async def get_agent():
        return SimpleNamespace(max_iterations=1, tools=tools)

    async def completion(**kwargs):
        writes.append(kwargs)
        return LLMResponse(content="synthetic checkpoint", usage={})

    monkeypatch.setattr(native, "_get_agent", get_agent)
    monkeypatch.setattr(native, "_provider_params", lambda _: {"model": "test-model"})
    monkeypatch.setattr(native, "_chat_completion", completion)
    admission = _admission(monkeypatch, "native")
    refused = False
    try:
        await native.invoke_checkpoint(admission.provider_request, asyncio.Event(), kind=CHECKPOINT_KIND)
    except ColdFoldInputTooLarge:
        refused = True

    assert writes == [], "cold fit omitted the tool surface and wrote to the provider"
    assert refused, "an impossible cold writer must report explicit construction refusal"


class _ColdClaudeProcess:
    """Exercise real adapter framing against a controlled CLI protocol peer."""

    def __init__(self, *, model="claude-haiku-4-5-20251001", tokens=25_685, respond=True, mcp_tokens=17_169, init_mcp_status="connected", mcp_status="connected"):
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self._done = asyncio.Event()
        self.stdin = self
        self.returncode = None
        self.model = model
        self.tokens = tokens
        self.respond = respond
        self.mcp_tokens = mcp_tokens
        self.init_mcp_status = init_mcp_status
        self.mcp_status = mcp_status
        self.context_requested = asyncio.Event()
        self.user_inputs: list[dict] = []
        self.terminated = False

    def write(self, data):
        request = json.loads(data)
        if request.get("type") == "user":
            assert self.context_requested.is_set(), "generation preceded writer admission"
            self.user_inputs.append(request)
            return
        if request["request"]["subtype"] == "mcp_status":
            self.stdout.feed_data((json.dumps({"type": "control_response", "response": {"request_id": request["request_id"], "subtype": "success", "response": {"mcpServers": [{"name": "corsia", "status": self.mcp_status}]}}}) + "\n").encode())
            return
        assert request["request"]["subtype"] == "get_context_usage"
        self.context_requested.set()
        if self.init_mcp_status is not None:
            self.stdout.feed_data((json.dumps({"type": "system", "subtype": "init", "mcp_servers": [{"name": "corsia", "status": self.init_mcp_status}]}) + "\n").encode())
        if self.respond:
            self.stdout.feed_data((json.dumps({
                "type": "control_response",
                "response": {
                    "request_id": request["request_id"],
                    "subtype": "success",
                    "response": {
                        "model": self.model,
                        "totalTokens": self.tokens,
                        "maxTokens": 200_000,
                        "rawMaxTokens": 200_000,
                        "categories": [{"name": "MCP tools", "kind": "used", "tokens": self.mcp_tokens}],
                    },
                },
            }) + "\n").encode())

    async def drain(self):
        return None

    def close(self):
        self.returncode = 0
        self.stdout.feed_data(json.dumps({
            "type": "result", "result": "checkpoint body", "is_error": False,
            "usage": {"input_tokens": 25_900, "output_tokens": 20},
        }).encode())
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self._done.set()

    def terminate(self):
        self.terminated = True
        self.returncode = -15
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self._done.set()

    async def wait(self):
        await self._done.wait()
        return self.returncode


def _claude_process(monkeypatch, **values):
    from core import checkpoint_cc_fork as cc

    process = _ColdClaudeProcess(**values)
    launches = []

    async def spawn(argv, *, cwd, env):
        launches.append(argv)
        assert argv[argv.index("--input-format") + 1] == "stream-json"
        return process

    monkeypatch.setattr(cc, "_spawn_process", spawn)
    return cc, process, launches


@pytest.mark.asyncio
@pytest.mark.usefixtures("seeded_cc_thread")
async def test_cc_reads_its_loaded_surface_before_one_generation(monkeypatch):
    """Skipping same-process prefix admission must not produce a user write."""
    cc, process, launches = _claude_process(monkeypatch)
    admission = _admission(monkeypatch, "claude-code")
    included = []

    async def record_input(first_seq):
        assert process.user_inputs == []
        included.append(first_seq)

    result = await cc.invoke_checkpoint(
        admission.provider_request, asyncio.Event(), on_cold_input=record_input, kind=CHECKPOINT_KIND,
    )

    assert len(launches) == 1
    assert len(process.user_inputs) == 1
    assert "synthetic review is complete" in process.user_inputs[0]["message"]["content"]
    assert result.raw_body == "checkpoint body"
    assert included == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize("values", [
    {"model": "wrong-model"},
    {"tokens": "25685"},
    {"tokens": 108_135},
])
@pytest.mark.usefixtures("seeded_cc_thread")
async def test_cc_refuses_unproved_or_oversized_prefix_without_generation(monkeypatch, values):
    """Missing identity/count proof or insufficient room cannot trigger input."""
    from core.checkpoint_types import ProviderCheckpointRejected

    cc, process, _launches = _claude_process(monkeypatch, **values)
    admission = _admission(monkeypatch, "claude-code")
    with pytest.raises((ProviderCheckpointRejected, ColdFoldInputTooLarge)):
        await cc.invoke_checkpoint(admission.provider_request, asyncio.Event(), kind=CHECKPOINT_KIND)
    assert process.user_inputs == []
    assert process.terminated


def test_cold_input_metadata_precedes_capture_and_retains_installed_winner_identity():
    """Deferred fitting must neither lose its source range nor duplicate a winner."""
    from infra import checkpoint_queries, checkpoints as store
    from test_checkpoint_store import _capture, _db, _install, _request, _seed_thread

    db = _db()
    try:
        _seed_thread(db, "cold-owner")
        request = replace(_request("cold-owner", "cold-op"), mode="cold", first_included_seq=None)
        store.create_running(request, db=db)
        store.admit_cold_input("cold-op", first_included_seq=2, db=db)
        assert checkpoint_queries.read("cold-op", db=db).first_included_seq == 2
        _capture(db, "cold-op")
        installed = _install(db, "cold-op")
        assert store.create_running(request, db=db) == installed
        with pytest.raises(store.CheckpointConflict):
            store.create_running(replace(request, first_included_seq=1), db=db)
    finally:
        db.close()


@pytest.mark.parametrize("mutation", ["cancelled", "stale_epoch", "already_admitted", "past_boundary"])
def test_cold_input_cannot_cross_its_existing_checkpoint_owner(mutation):
    """Late/stale admission cannot overwrite another state or extend coverage."""
    from infra import checkpoint_queries, checkpoints as store
    from test_checkpoint_store import _db, _request, _seed_thread

    db = _db()
    try:
        _seed_thread(db, "cold-owner")
        request = replace(_request("cold-owner", "cold-op"), mode="cold", first_included_seq=None)
        store.create_running(request, db=db)
        if mutation == "cancelled":
            store.cancel("cold-op", ended_at="t2", db=db)
        elif mutation == "stale_epoch":
            db.execute("UPDATE threads SET history_epoch=1 WHERE id='cold-owner'")
            db.commit()
        elif mutation == "already_admitted":
            store.admit_cold_input("cold-op", first_included_seq=1, db=db)
        with pytest.raises(store.CheckpointTransitionError):
            store.admit_cold_input(
                "cold-op", first_included_seq=4 if mutation == "past_boundary" else 2, db=db,
            )
        assert checkpoint_queries.read("cold-op", db=db).first_included_seq == (
            1 if mutation == "already_admitted" else None
        )
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.usefixtures("seeded_cc_thread")
async def test_cc_cancel_while_reading_prefix_terminates_only_its_writer(monkeypatch):
    """A stalled control response must remain inside checkpoint cancellation."""
    cc, process, _launches = _claude_process(monkeypatch, respond=False)
    admission = _admission(monkeypatch, "claude-code")
    cancel = asyncio.Event()
    operation = asyncio.create_task(cc.invoke_checkpoint(admission.provider_request, cancel, kind=CHECKPOINT_KIND))
    try:
        await asyncio.wait_for(process.context_requested.wait(), 1)
        cancel.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(operation, 1)
    finally:
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)
    assert process.terminated
    assert process.user_inputs == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("seeded_cc_thread")
async def test_cc_silent_process_times_out_without_generating(monkeypatch):
    """A non-reporting process is refused, not waited on (#2078)."""
    from core.checkpoint_types import ProviderCheckpointRejected

    monkeypatch.setattr("core.checkpoint_cc_cold._CONTEXT_ADMISSION_TIMEOUT_S", 0.05)
    cc, process, _launches = _claude_process(monkeypatch, respond=False, init_mcp_status=None)
    with pytest.raises(ProviderCheckpointRejected, match=r"cold context admission timed out.*init status: not received.*elapsed from process spawn: [0-9.]+s"):
        await cc.invoke_checkpoint(_admission(monkeypatch, "claude-code").provider_request, asyncio.Event(), kind=CHECKPOINT_KIND)
    assert process.context_requested.is_set()
    assert process.user_inputs == []
    assert process.terminated


@pytest.mark.asyncio
async def test_cc_context_timeout_starts_at_control_write_not_process_spawn(monkeypatch):
    from core import checkpoint_cc_cold as cold
    from core.checkpoint_types import ProviderCheckpointRejected

    waits = []

    async def timeout(awaitable, seconds):
        awaitable.close()
        waits.append(seconds)
        raise TimeoutError

    monkeypatch.setattr(cold.asyncio, "wait_for", timeout)
    monkeypatch.setattr(cold.time, "monotonic", lambda: 100.0)
    with pytest.raises(ProviderCheckpointRejected, match="admission timed out"):
        await cold._loaded_context(
            _ColdClaudeProcess(respond=False, init_mcp_status=None),
            _admission(monkeypatch, "claude-code").provider_request,
            spawned_at=70.0,
        )
    assert waits == [30.0]


@pytest.mark.asyncio
@pytest.mark.usefixtures("seeded_cc_thread")
async def test_cc_configured_mcp_failure_refuses_tool_free_generation(monkeypatch):
    from core.checkpoint_types import ProviderCheckpointRejected
    cc, process, _launches = _claude_process(monkeypatch, mcp_tokens=0, init_mcp_status=None, mcp_status="pending")
    request = replace(_admission(monkeypatch, "claude-code").provider_request, mcp_config="fixture-mcp.json")
    with pytest.raises(ProviderCheckpointRejected, match=r"MCP tools.*MCP status: pending.*elapsed from process spawn: [0-9.]+s"):
        await cc.invoke_checkpoint(request, asyncio.Event(), kind=CHECKPOINT_KIND)
    assert process.user_inputs == []
    assert process.terminated


@pytest.mark.asyncio
@pytest.mark.usefixtures("seeded_cc_thread")
async def test_cc_exit_before_context_reports_init_status_and_exchange_time(monkeypatch):
    from core.checkpoint_types import ProviderCheckpointRejected
    cc, process, _launches = _claude_process(monkeypatch, respond=False, init_mcp_status=None)
    process.stdout.feed_data(b'{"type":"system","subtype":"init","mcp_servers":[{"name":"corsia","status":"starting"}]}\n')
    process.stdout.feed_eof()
    with pytest.raises(ProviderCheckpointRejected, match=r"exited before cold context admission.*init status: starting.*elapsed from process spawn: [0-9.]+s"):
        await cc.invoke_checkpoint(_admission(monkeypatch, "claude-code").provider_request, asyncio.Event(), kind=CHECKPOINT_KIND)
    assert process.user_inputs == []
    assert process.terminated


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [b"", b"not json\n", b"[]\n", b'{"type":"result"}\n'])
@pytest.mark.usefixtures("seeded_cc_thread")
async def test_cc_missing_or_invalid_context_never_sends_generation(monkeypatch, response):
    from core.checkpoint_types import ProviderCheckpointRejected
    cc, process, _launches = _claude_process(monkeypatch, respond=False, init_mcp_status=None)
    process.stdout.feed_data(response)
    process.stdout.feed_eof()
    with pytest.raises(ProviderCheckpointRejected):
        await cc.invoke_checkpoint(_admission(monkeypatch, "claude-code").provider_request, asyncio.Event(), kind=CHECKPOINT_KIND)
    assert process.user_inputs == []
    assert process.terminated


@pytest.mark.asyncio
@pytest.mark.usefixtures("seeded_cc_thread")
async def test_cc_ignores_uncorrelated_context_and_honors_smaller_reported_window(monkeypatch):
    cc, process, _launches = _claude_process(monkeypatch, respond=False)
    for request_id, window in (("unrelated", 1_000_000), ("corsia-checkpoint-context", 16_384)):
        process.stdout.feed_data((json.dumps({
            "type": "control_response",
            "response": {
                "request_id": request_id, "subtype": "success",
                "response": {"model": process.model, "totalTokens": 1000, "maxTokens": window},
            },
        }) + "\n").encode())
    with pytest.raises(ColdFoldInputTooLarge):
        await cc.invoke_checkpoint(_admission(monkeypatch, "claude-code").provider_request, asyncio.Event(), kind=CHECKPOINT_KIND)
    assert process.user_inputs == []
    assert process.terminated
