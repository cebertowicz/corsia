"""Disposable checkpoint adapter contracts."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import fork_cc_commands
from core.checkpoint_fork_kind import CHECKPOINT_KIND
from core.checkpoint_types import CheckpointProviderForkRequest
from core.fork_fit import ColdFoldInput
from history.types import HistoryBoundary
from llm.resolution import ExecutionTarget
from llm.router import LLMResponse, ToolCallRequest
from vendor_account_fixtures import default_home, default_receipt
from native_checkpoint_fixtures import cold_source, seed_vendor_checkpoint_thread

pytestmark = pytest.mark.usefixtures("signed_in_defaults")  # a harness turn needs a signed-in Default: no ambient login
DISCOVERED_MODELS = {'claude-code': ('catalog-claude-code',), 'codex': ('catalog-codex',)}


def _target(harness: str = "native") -> ExecutionTarget:
    return ExecutionTarget(
        catalog_id=f"catalog-{harness}",
        harness=harness,
        provider=harness,
        backend="local" if harness == "native" else "harness",
        transport_model=f"model-{harness}",
        expected_reported_model=None,
        context_window=262_144,
        available=True,
        availability_reason=None,
        display_name="native",
        supports_reasoning=True,
        preserve_thinking_default=True,
    )


def _boundary() -> HistoryBoundary:
    return HistoryBoundary("thread-1", 4, 91, "message-91")


@pytest.fixture
def vendor_thread(isolated_db):
    seed_vendor_checkpoint_thread([_target(harness) for harness in ("native", "claude-code", "codex")])


def _request(source):
    return CheckpointProviderForkRequest(
        fork_id="checkpoint-1",
        thread_id="thread-1",
        target=_target(),
        instruction="Write the current checkpoint.",
        cold_source=cold_source() if source is None else None,
        reasoning_effort="high",
        working_dir="/work/project",
        warm_source=source,
    )


def _source(
    module,
    *,
    provider_input_tokens: int = 0,
    rendered_input_tokens: int = 0,
):
    return module.NativeWarmSource(
        "thread-1",
        _boundary(),
        ({"role": "system", "content": "prefix"},),
        (),
        "prefix",
        provider_input_tokens=provider_input_tokens,
        rendered_input_tokens=rendered_input_tokens,
    )


def test_native_warm_fit_uses_provider_anchor_plus_only_added_delta(monkeypatch):
    """A warm fork must not recount its accepted prefix with a new heuristic."""
    from core import fork_native as native
    from core.fork_native_tools import input_fits
    from core.token_estimation import TokenMeasurement

    source = _source(
        native,
        provider_input_tokens=700,
        rendered_input_tokens=900,
    )
    request = replace(
        _request(source),
        target=replace(_target(), context_window=800),
        max_output_tokens=50,
    )
    messages = [
        *source.prefix_messages,
        {"role": "user", "content": request.instruction},
    ]
    monkeypatch.setattr(
        "core.token_estimation.measure_request",
        lambda *_a, **_k: TokenMeasurement(950, None, "model", "test"),
    )

    # The estimator moved from the frozen 900-token baseline to 950. The
    # provider measured that same baseline at 700, so only the +50 delta is new.
    assert input_fits(request, messages, list(source.tool_definitions)) is True

    too_full = replace(source, provider_input_tokens=780)
    assert input_fits(
        replace(request, warm_source=too_full),
        messages,
        list(source.tool_definitions),
    ) is False


@pytest.mark.asyncio
async def test_native_warm_initial_fit_reserves_max_output_before_provider_call(monkeypatch):
    """A warm fork that cannot fit its generation budget must correct before calling."""
    from core import fork_native as native
    from core.fork_types import WarmForkUnusable
    from core.token_estimation import TokenMeasurement

    source = _source(
        native,
        provider_input_tokens=700,
        rendered_input_tokens=900,
    )
    request = replace(
        _request(source),
        target=replace(_target(), context_window=800),
        max_output_tokens=100,
    )
    monkeypatch.setattr(
        "core.token_estimation.measure_request",
        lambda *_a, **_k: TokenMeasurement(950, None, "model", "test"),
    )

    async def unexpected_provider_call(**_kwargs):
        pytest.fail("initial overflow must be refused before the provider call")

    monkeypatch.setattr(
        native, "_get_agent", lambda: _async_value(SimpleNamespace(max_iterations=1))
    )
    monkeypatch.setattr(native, "_chat_completion", unexpected_provider_call)
    monkeypatch.setattr(native, "_provider_params", lambda _model: {"model": "wire"})

    with pytest.raises(WarmForkUnusable):
        await native.invoke_fork(request, asyncio.Event(), kind=CHECKPOINT_KIND)


@pytest.mark.asyncio
async def test_native_checkpoint_refuses_non_continuity_tools_and_keeps_continuity_edit(monkeypatch):
    """Only the checkpoint's continuity tool may execute in its private loop."""
    from core import fork_native_tools

    executed = []

    class Tools:
        async def execute(self, name, arguments):
            executed.append((name, arguments))
            return "edit staged"

    response = LLMResponse(
        content=None,
        tool_calls=[
            ToolCallRequest(id="call-read", name="read_file", arguments={"path": "/huge"}),
            ToolCallRequest(id="call-edit", name="continuity_edit", arguments={"op": "add"}),
        ],
    )
    monkeypatch.setattr(
        fork_native_tools, "input_fits_off_loop", lambda *_args: _async_value(True),
    )

    messages = []
    result = await fork_native_tools.run_tool_calls(
        SimpleNamespace(tools=Tools()),
        response,
        messages,
        asyncio.Event(),
        _request(None),
        [],
        kind=CHECKPOINT_KIND,
    )

    assert result is True
    assert executed == [("continuity_edit", {"op": "add"})]
    assert messages[-2] == {
        "role": "tool",
        "tool_call_id": "call-read",
        "name": "read_file",
        "content": "Native checkpoints may only use continuity_edit.",
    }
    assert messages[-1]["content"] == "edit staged"


@pytest.mark.asyncio
async def test_native_cold_fork_never_offers_a_tool_it_can_only_refuse(monkeypatch):
    """A cold fork must not tempt a call it can only bounce (#2116).

    Offering the full catalog invites a disallowed tool call; refusing it
    still bills output tokens for that round trip while the loop's messages
    grow, and a tightly-fit cold input can run out of room before any
    narrative text is ever produced -- an installable body of "".
    """
    from core import fork_native as native
    from core.checkpoint_fork_kind import CHECKPOINT_TOOL_NAME

    catalog = (
        {"type": "function", "function": {"name": CHECKPOINT_TOOL_NAME}},
        {"type": "function", "function": {"name": "read_file"}},
    )
    # Once any tool call round-trips through the loop, pretend it no longer
    # fits -- exactly the tightly-fit cold input this failure needs, without
    # depending on real token counts.
    monkeypatch.setattr(
        "core.fork_native_tools.input_fits_off_loop",
        lambda *_args: _async_value(False),
    )

    calls = []

    async def completion(**kwargs):
        calls.append(kwargs)
        offered = {tool["function"]["name"] for tool in kwargs["tools"]}
        if offered - {CHECKPOINT_TOOL_NAME}:
            # A real model reaches for a tool it can see; only the fix
            # (not advertising it) can stop this branch from firing.
            return LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(id="call-1", name="read_file", arguments={"path": "/x"}),
                ],
                usage={"prompt_tokens": 100, "completion_tokens": 8},
            )
        return LLMResponse(
            content="checkpoint narrative",
            usage={"prompt_tokens": 100, "completion_tokens": 8},
        )

    class Tools:
        def get_definitions(self):
            return list(catalog)

        async def execute(self, name, arguments):
            pytest.fail(f"a refused tool must never execute: {name}")

    agent = SimpleNamespace(max_iterations=5, tools=Tools())
    monkeypatch.setattr(native, "_get_agent", lambda: _async_value(agent))
    monkeypatch.setattr(native, "_chat_completion", completion)
    monkeypatch.setattr(native, "_provider_params", lambda _model: {"model": "wire"})

    result = await native.invoke_fork(_request(None), asyncio.Event(), kind=CHECKPOINT_KIND)

    from core.checkpoint_render import usable_result

    assert result.raw_body == "checkpoint narrative"
    assert usable_result(result.raw_body)
    assert len(calls) == 1
    assert {tool["function"]["name"] for tool in calls[0]["tools"]} == {CHECKPOINT_TOOL_NAME}


@pytest.mark.asyncio
async def test_native_cold_fold_at_its_allowance_completes_edit_then_narrative(monkeypatch):
    """A fold fitted to its allowance must still hold its own round trip (#2116).

    The first reply is a continuity_edit call that carries reasoning; appending
    it grew the input past the loop's own limit, so the fork returned an empty
    truncated result ("no content") before the edit ran. Admission has to leave
    the room that round trip needs.
    """
    from cache_warm_fixtures import make_exact_replay_selection
    from core import fork_native as native
    from core.fork_cold_count import cold_input_tokens
    from core.checkpoint_fork_kind import CHECKPOINT_TOOL_NAME
    from core.fork_types import ColdForkSource

    window, max_output = 100_000, 8_000
    turns = [
        [("user", f"ask {i} " + "history " * 100), ("assistant", "answer " * 100)]
        for i in range(400)
    ]
    selection = make_exact_replay_selection("thread-1", turns)
    request = replace(
        _request(None),
        cold_source=ColdForkSource(selection.tail, ()),
        target=replace(_target(), context_window=window),
        max_output_tokens=max_output,
    )
    calls, edits = [], []

    async def completion(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return LLMResponse(
                content=None,
                reasoning_content="considering the edit " * 450,
                tool_calls=[ToolCallRequest(
                    id="call-1", name=CHECKPOINT_TOOL_NAME, arguments={"op": "add"},
                )],
                finish_reason="tool_calls",
            )
        return LLMResponse(content="checkpoint narrative")

    class Tools:
        def get_definitions(self):
            return [{"type": "function", "function": {"name": CHECKPOINT_TOOL_NAME}}]

        async def execute(self, name, arguments):
            edits.append((name, arguments))
            return "ok"

    agent = SimpleNamespace(max_iterations=5, tools=Tools())
    monkeypatch.setattr(native, "_get_agent", lambda: _async_value(agent))
    monkeypatch.setattr(native, "_chat_completion", completion)
    monkeypatch.setattr(native, "_provider_params", lambda _model: {"model": "wire"})

    result = await native.invoke_fork(request, asyncio.Event(), kind=CHECKPOINT_KIND)

    first = calls[0]
    fitted = cold_input_tokens(first["messages"], request.target) + cold_input_tokens(
        first["tools"], request.target,
    )
    assert fitted >= window - 2 * max_output - window // 20 - 2_000
    assert result.raw_body == "checkpoint narrative"
    assert result.diagnostic is None
    assert edits == [(CHECKPOINT_TOOL_NAME, {"op": "add"})]
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_native_cold_fold_fits_a_small_local_window(monkeypatch):
    """A 41,681-token local seat must still fold under pressure.

    A fixed 16,384 output allowance reserved twice (reply, then its round
    trip) left under 7k of input, so every cold fold on a 32 GiB local-only
    install failed with "minimum meaningful cold fork input does not fit".
    Once it fit, re-counting a ~6.8k-token reasoning reply as UTF-8 bytes
    overflowed the loop, so the round trip counts what the provider measured.
    """
    from cache_warm_fixtures import make_exact_replay_selection
    from core import fork_native as native
    from core.checkpoint_fork_kind import CHECKPOINT_TOOL_NAME
    from core.fork_types import ColdForkSource
    from prompts.checkpoint import build_checkpoint_system_prompt

    window = 41_681
    turns = [
        [("user", f"ask {i} " + "history " * 100), ("assistant", "answer " * 100)]
        for i in range(60)
    ]
    # Its writer has no bundled tokenizer, so cold input counts UTF-8 bytes.
    turns.append([("user", "latest ask " + "detail " * 1_500), ("assistant", "done")])
    selection = make_exact_replay_selection("thread-1", turns)
    request = replace(
        _request(None),
        cold_source=ColdForkSource(selection.tail, ()),
        target=replace(_target(), context_window=window),
        instruction=build_checkpoint_system_prompt(),
    )
    calls, edits = [], []

    async def completion(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return LLMResponse(
                content=None,
                reasoning_content="considering the edit " * 1_300,
                tool_calls=[ToolCallRequest(
                    id="call-1", name=CHECKPOINT_TOOL_NAME, arguments={"op": "add"},
                )],
                finish_reason="tool_calls",
                usage={"prompt_tokens": 5_500, "completion_tokens": 6_800},
            )
        return LLMResponse(
            content="checkpoint narrative",
            usage={"prompt_tokens": 12_310, "completion_tokens": 400},
        )

    class Tools:
        def get_definitions(self):
            return [{"type": "function", "function": {"name": CHECKPOINT_TOOL_NAME}}]

        async def execute(self, name, arguments):
            edits.append((name, arguments))
            return "ok"

    agent = SimpleNamespace(max_iterations=5, tools=Tools())
    monkeypatch.setattr(native, "_get_agent", lambda: _async_value(agent))
    monkeypatch.setattr(native, "_chat_completion", completion)
    monkeypatch.setattr(native, "_provider_params", lambda _model: {"model": "wire"})

    result = await native.invoke_fork(request, asyncio.Event(), kind=CHECKPOINT_KIND)

    assert result.raw_body == "checkpoint narrative"
    assert result.diagnostic is None
    assert edits == [(CHECKPOINT_TOOL_NAME, {"op": "add"})]
    assert [call["max_tokens"] for call in calls] == [window // 5] * 2


@pytest.mark.asyncio
async def test_native_cold_fold_fits_a_pasted_document_on_a_small_local_window(monkeypatch):
    """A person who pastes a document into a local chat can still fold.

    The final turn's initiating message is never cut, and an unknown-family
    writer was counted in UTF-8 bytes, so a 15,767-character paste plus its
    reply overran the 41,681 seat's whole cold budget: every later pressure
    checkpoint failed with "minimum meaningful cold fork input does not fit".
    The writer's own server now counts it with the model's tokenizer.
    """
    from cache_warm_fixtures import make_exact_replay_selection
    from core import fork_cold_count, fork_native as native
    from core.checkpoint_fork_kind import CHECKPOINT_TOOL_NAME
    from core.fork_fit import ColdFoldInputTooLarge
    from core.fork_types import ColdForkSource
    from core.token_estimation import _model_encoding
    from prompts.checkpoint import build_checkpoint_system_prompt

    window = 41_681
    paste = " ".join(
        f"Section {i}: the venue holds {i * 7} guests and the caterer needs a final "
        f"count by the {i % 28 + 1}th, with dietary notes for table {i % 40}."
        for i in range(400)
    )[:15_767]
    turns = [
        [("user", f"ask {i} " + "history " * 100), ("assistant", "answer " * 100)]
        for i in range(30)
    ]
    turns.append([("user", paste), ("assistant", "Summary of the plan. " * 400)])
    selection = make_exact_replay_selection("thread-1", turns)
    request = replace(
        _request(None),
        cold_source=ColdForkSource(selection.tail, ()),
        target=replace(_target(), transport_model="omlx@ns-test/qwen", context_window=window),
        instruction=build_checkpoint_system_prompt(),
    )
    monkeypatch.setattr(fork_cold_count, "_server_tokens", lambda _text, _writer: None)
    with pytest.raises(ColdFoldInputTooLarge):
        native._fit_cold_input(native._window_output(request), [])

    encoding = _model_encoding("gpt-5")
    monkeypatch.setattr(
        fork_cold_count, "_server_tokens",
        lambda text, _writer: len(encoding.encode(text, disallowed_special=())),
    )
    calls, edits = [], []

    async def completion(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest(
                    id="call-1", name=CHECKPOINT_TOOL_NAME, arguments={"op": "add"},
                )],
                finish_reason="tool_calls",
                usage={"prompt_tokens": 9_000, "completion_tokens": 300},
            )
        return LLMResponse(content="checkpoint narrative")

    class Tools:
        def get_definitions(self):
            return [{"type": "function", "function": {"name": CHECKPOINT_TOOL_NAME}}]

        async def execute(self, name, arguments):
            edits.append((name, arguments))
            return "ok"

    agent = SimpleNamespace(max_iterations=5, tools=Tools())
    monkeypatch.setattr(native, "_get_agent", lambda: _async_value(agent))
    monkeypatch.setattr(native, "_chat_completion", completion)
    monkeypatch.setattr(native, "_provider_params", lambda _model: {"model": "wire"})

    result = await native.invoke_fork(request, asyncio.Event(), kind=CHECKPOINT_KIND)

    assert paste in json.dumps(calls[0]["messages"], ensure_ascii=False)
    assert result.raw_body == "checkpoint narrative"
    assert result.diagnostic is None
    assert edits == [(CHECKPOINT_TOOL_NAME, {"op": "add"})]


def test_native_round_trip_counts_from_the_provider_measurement():
    """After a measured call, later input is that measurement plus what was added."""
    from core import fork_native_tools as loop
    from core.token_estimation import measure_request

    request = replace(_request(None), target=replace(_target(), context_window=41_681))
    reply = {"role": "assistant", "content": None, "reasoning_content": "think " * 5_000}
    messages = [{"role": "system", "content": "fold"}, reply]
    measured = loop.measured_input(
        request, messages, [], {"prompt_tokens": 9_000, "completion_tokens": 7_000},
    )
    tool_reply = {"role": "tool", "tool_call_id": "call-1", "content": "ok"}
    added = measure_request(
        {"messages": [*messages, tool_reply], "tools": []}, target=request.target,
    ).tokens - measured.rendered_input_tokens

    room = loop.input_room(request, [*messages, tool_reply], [], measured=measured)

    assert measured.provider_input_tokens == 16_000
    assert room.counted == 16_000 + added
    assert loop.measured_input(request, messages, [], {"prompt_tokens": 9_000}) is None


def test_native_cold_admission_keeps_room_for_its_output_budget():
    """A cold input admitted with framing margin also passes the loop check."""
    from core import fork_native as native
    from core.fork_native_tools import input_fits

    request = _request(None)
    fitted = native._fit_cold_input(request, [])
    messages = native.cold_messages(request.instruction, fitted.messages)

    assert input_fits(request, messages, []) is True


@pytest.mark.asyncio
async def test_native_warm_capture_reuses_prefix_tools_and_tool_loop(monkeypatch):
    """Changing the prefix/tools or skipping the tool continuation is a bug."""
    from core import fork_native as native
    from infra import state

    prefix = (
        {"role": "system", "content": "stable system"},
        {"role": "user", "content": "stable history"},
    )
    tools = ({"type": "function", "function": {"name": "continuity_edit"}},)
    source = native.capture_live_warm_source(
        "thread-1",
        _boundary(),
        messages=list(prefix),
        tools=list(tools),
        reasoning_effort="high",
        preserve_thinking=True,
        provider_input_tokens=0,
        rendered_input_tokens=0,
    )
    assert source.provider_input_tokens == 0
    assert source.rendered_input_tokens == 0

    calls = []
    responses = [
        LLMResponse(
            content=None,
            tool_calls=[
                ToolCallRequest(
                    id="call-1", name="continuity_edit", arguments={"op": "add"}
                )
            ],
            usage={"prompt_tokens": 100, "completion_tokens": 8},
        ),
        LLMResponse(
            content="final checkpoint",
            usage={"prompt_tokens": 120, "completion_tokens": 12},
        ),
    ]

    async def completion(**kwargs):
        calls.append(kwargs)
        return responses.pop(0)

    class Tools:
        async def execute(self, name, arguments):
            assert (name, arguments) == ("continuity_edit", {"op": "add"})
            assert state._current_thread_id.get(None) == "thread-1"
            return "edit staged"

    agent = SimpleNamespace(max_iterations=5, tools=Tools())
    monkeypatch.setattr(native, "_get_agent", lambda: _async_value(agent))
    monkeypatch.setattr(native, "_chat_completion", completion)
    monkeypatch.setattr(native, "_provider_params", lambda _model: {"model": "wire"})

    outer = state._current_thread_id.set("outer-thread")
    try:
        result = await native.invoke_fork(_request(source), asyncio.Event(), kind=CHECKPOINT_KIND)
        assert state._current_thread_id.get(None) == "outer-thread"
    finally:
        state._current_thread_id.reset(outer)

    assert result.raw_body == "final checkpoint"
    assert calls[0]["messages"][:-1] == list(prefix)
    assert calls[0]["messages"][-1] == {
        "role": "user",
        "content": "Write the current checkpoint.",
    }
    assert calls[0]["tools"] == list(tools)
    assert calls[0]["reasoning_effort"] == "high"
    assert calls[1]["messages"][-1] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "name": "continuity_edit",
        "content": "edit staged",
    }
    assert len(result.usage["calls"]) == 2


@pytest.mark.asyncio
async def test_native_provider_failure_cannot_be_installed_as_checkpoint(monkeypatch):
    """A transport failure is not model output, even when it carries text."""
    from core import fork_native as native
    from core.fork_types import ProviderForkRejected

    response = LLMResponse(
        content="Error calling LLM: Cannot load selected model",
        finish_reason="error",
    )
    monkeypatch.setattr(
        native,
        "_get_agent",
        lambda: _async_value(SimpleNamespace(max_iterations=1, tools=None)),
    )
    monkeypatch.setattr(native, "_chat_completion", lambda **_: _async_value(response))
    monkeypatch.setattr(native, "_provider_params", lambda _model: {})

    with pytest.raises(ProviderForkRejected, match="Cannot load selected model"):
        await native.invoke_fork(_request(_source(native)), asyncio.Event(), kind=CHECKPOINT_KIND)


@pytest.mark.asyncio
async def test_native_checkpoint_cancel_cancels_exact_provider_call(monkeypatch):
    """Native Stop cancels the owned oMLX/provider task, not only its caller."""
    from core import fork_native as native

    provider_cancelled = asyncio.Event()

    async def completion(**_kwargs):
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            provider_cancelled.set()
            raise

    agent = SimpleNamespace(max_iterations=1, tools=None)
    monkeypatch.setattr(native, "_get_agent", lambda: _async_value(agent))
    monkeypatch.setattr(native, "_chat_completion", completion)
    monkeypatch.setattr(native, "_provider_params", lambda _model: {})
    cancel = asyncio.Event()
    operation = asyncio.create_task(
        native.invoke_fork(_request(_source(native)), cancel, kind=CHECKPOINT_KIND)
    )
    await asyncio.sleep(0)
    cancel.set()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert provider_cancelled.is_set()


@pytest.mark.asyncio
async def test_cc_usable_body_retains_process_exit_diagnostic(monkeypatch, vendor_thread):
    from core import fork_cc as cc
    from core.fork_render import fit_cold_fork_input

    async def prepare(_process, request, _cancel, _on_input, _spawned_at):
        from subagents.cc_stream_frames import build_user_frame

        fitted = fit_cold_fork_input(request, provider_surface_tokens=25_685)
        return build_user_frame(fitted.as_prompt(request.instruction))

    monkeypatch.setattr("core.fork_cc_cold.prepare_cold_payload", prepare)

    stdout = json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "paid body"}]},
    }).encode()

    async def spawn(*_args, **_kwargs):
        from test_checkpoint_external_adapters import FakeProcess

        return FakeProcess(stdout, returncode=7, stderr=b"quota/auth process terminal")

    monkeypatch.setattr(fork_cc_commands, "cold_command", lambda _request, _kind: ["claude"])
    monkeypatch.setattr(cc, "_fork_env", lambda *_args: {})
    monkeypatch.setattr(cc, "_spawn_process", spawn)
    request = replace(_request(None), target=_target("claude-code"))

    result = await cc.invoke_fork(request, asyncio.Event(), kind=CHECKPOINT_KIND)

    assert result.raw_body == "paid body"
    assert result.diagnostic == "quota/auth process terminal"


@pytest.mark.asyncio
async def test_cc_process_context_limit_retains_partial_body(monkeypatch, vendor_thread):
    from core import fork_cc as cc
    from core.fork_render import fit_cold_fork_input

    async def prepare(_process, request, _cancel, _on_input, _spawned_at):
        from subagents.cc_stream_frames import build_user_frame

        fitted = fit_cold_fork_input(request, provider_surface_tokens=25_685)
        return build_user_frame(fitted.as_prompt(request.instruction))

    monkeypatch.setattr("core.fork_cc_cold.prepare_cold_payload", prepare)

    stdout = json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "partial body"}]},
    }).encode()

    async def spawn(*_args, **_kwargs):
        from test_checkpoint_external_adapters import FakeProcess

        return FakeProcess(stdout, returncode=1, stderr=b"Prompt is too long")

    monkeypatch.setattr(fork_cc_commands, "cold_command", lambda _request, _kind: ["claude"])
    monkeypatch.setattr(cc, "_fork_env", lambda *_args: {})
    monkeypatch.setattr(cc, "_spawn_process", spawn)
    request = replace(_request(None), target=_target("claude-code"))

    result = await cc.invoke_fork(request, asyncio.Event(), kind=CHECKPOINT_KIND)
    assert result.raw_body == "partial body"
    assert result.diagnostic == "checkpoint_context_truncated"


def test_cc_warm_command_preserves_vetted_live_launch_files(tmp_path):
    """A live warm fork keeps the cache-bearing seed and continuity tools."""
    from core import fork_cc as cc
    from subagents.cc_invocation import ClaudeInvocation

    seed_file = tmp_path / "seed.txt"
    mcp_config = tmp_path / "tools.json"
    seed_file.write_text("system prompt")
    mcp_config.write_text("{}")
    invocation = ClaudeInvocation(
        binary="old-claude",
        permission_mode="bypassPermissions",
        session_id="source-session",
        seed_file=str(seed_file),
        mcp_config=str(mcp_config),
        working_dir="/work/source",
        model="sonnet",
        effort="high",
    )
    source = cc.CCWarmSource(
        "thread-1",
        _boundary(),
        "source-session",
        "/transcript.jsonl",
        12,
        "digest",
        invocation,
        None,
    )
    request = replace(
        _request(source),
        target=_target("claude-code"),
        binary="current-claude",
    )

    command = fork_cc_commands.warm_command(source, request, CHECKPOINT_KIND, "snapshot-session")

    assert command[0] == "current-claude"
    assert command[command.index("--resume") + 1] == "snapshot-session"
    assert command[command.index("--append-system-prompt-file") + 1] == str(seed_file)
    assert command[command.index("--mcp-config") + 1] == str(mcp_config)
    for expected in ("bypassPermissions", "sonnet", "high"):
        assert expected in " ".join(command)
    # The live working directory travels as the spawn cwd, never as an
    # argument; it used to appear here only inside the old cwd-derived
    # claudeMdExcludes, which #1874 replaced with globs.
    assert "/work/source" not in " ".join(command)


@pytest.mark.asyncio
async def test_cc_registered_warm_recreates_then_removes_seed(tmp_path, monkeypatch, vendor_thread):
    from core import fork_cc as cc
    from subagents.cc_invocation import ClaudeInvocation

    transcript = tmp_path / "source.jsonl"
    transcript.write_text('{"type":"user"}\n')
    source = cc.CCWarmSource(
        "thread-1", _boundary(), "source-session", str(transcript),
        transcript.stat().st_size, hashlib.sha256(transcript.read_bytes()).hexdigest(),
        ClaudeInvocation("claude", "bypassPermissions", working_dir="/work/source"),
        default_home("claude-code"), "restored seed", vendor_account_receipt=default_receipt("claude-code"),
    )
    observed = {}

    async def spawn(argv, **kwargs):
        from test_checkpoint_external_adapters import FakeProcess

        seed = Path(argv[argv.index("--append-system-prompt-file") + 1])
        observed.update(argv=argv, seed=seed, text=seed.read_text(), cwd=kwargs["cwd"])
        body = {"type": "result", "result": "checkpoint", "is_error": False}
        return FakeProcess((json.dumps(body) + "\n").encode(), stderr=b"", context_model="model-claude-code")

    monkeypatch.setattr(cc, "_spawn_process", spawn)
    request = replace(_request(source), target=_target("claude-code"), binary="claude")
    result = await cc.invoke_fork(request, asyncio.Event(), kind=CHECKPOINT_KIND)

    assert result.raw_body == "checkpoint"
    assert observed["text"] == "restored seed"
    assert not observed["seed"].exists()
    assert observed["cwd"] == "/work/source"
    argv = observed["argv"]
    assert argv[argv.index("--tools") + 1] == ""
    assert "--disable-slash-commands" in argv
    assert argv[argv.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert "--strict-mcp-config" in argv


@pytest.mark.asyncio
async def test_codex_usable_body_retains_process_exit_diagnostic(
    tmp_path, monkeypatch, vendor_thread,
):
    from core import fork_codex as codex

    class Session:
        def __init__(self, **kwargs):
            self.on_event = kwargs["on_event"]
            self.on_exit = kwargs["on_exit"]

        async def start(self):
            await self.on_event({"kind": "assistant_text", "text": "paid body"})
            await self.on_exit(7, "quota/auth process terminal")
            return True

        async def terminate(self):
            return None

    monkeypatch.setattr(
        "subagents.codex_app_server.CodexAppServerSession", Session,
    )
    request = replace(
        _request(None),
        target=_target("codex"),
        tool_registry=object(),
    )

    from core.fork_account import admit_fork
    from connections.account_operation import prepare_operation_home

    request = prepare_operation_home(admit_fork(request), Path(tmp_path), kind='checkpoint')
    result = await codex._run_app_server_fork(
        request,
        None,
        source_home=Path(tmp_path),
        isolated_home=Path(tmp_path),
        cwd=str(tmp_path),
        cancel_event=asyncio.Event(),
        cold_input=ColdFoldInput((), 10, None, 0),
        kind=CHECKPOINT_KIND,
    )

    assert result.raw_body == "paid body"
    assert result.diagnostic == "quota/auth process terminal"


async def _async_value(value):
    return value
