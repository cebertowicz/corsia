"""Cold checkpoint fit includes the provider surface it will actually send."""

from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace
from typing import Any

import pytest
from core.checkpoint_fork_kind import CHECKPOINT_KIND

from core.checkpoint_types import CheckpointProviderForkRequest
from llm.resolution import ExecutionTarget
from native_checkpoint_fixtures import cold_source, seed_vendor_checkpoint_thread


def _target(harness: str, *, context_window: int = 262_144) -> ExecutionTarget:
    return ExecutionTarget(
        catalog_id=f"catalog-{harness}",
        harness=harness,
        provider=harness,
        backend="harness",
        transport_model=f"model-{harness}",
        expected_reported_model=f"reported-{harness}",
        context_window=context_window,
        available=True,
        availability_reason=None,
        display_name=harness,
        supports_reasoning=True,
        preserve_thinking_default=True,
    )


def _admission(monkeypatch, harness: str, *, window=262_144, projection=None):
    from core.checkpoint_cold_admission import build_checkpoint_admission

    monkeypatch.setattr("infra.continuity_store.active_entries", lambda *_a: {})
    monkeypatch.setattr(
        "prompts.checkpoint.build_checkpoint_system_prompt",
        lambda: "checkpoint instruction",
    )
    target = _target(harness, context_window=window)
    projection = projection or cold_source().projection
    return build_checkpoint_admission(
        checkpoint_id="checkpoint-1",
        thread_id="thread-1",
        trigger="model_switch",
        source_target=target,
        writer_target=target,
        boundary=projection.sealed_boundary,
        previous=None,
        projection=projection,
        working_dir="/work/project",
        reasoning_effort="high",
        binary=harness,
        db=object(),
    )


@pytest.mark.parametrize("harness", ("native", "codex", "claude-code"))
@pytest.mark.parametrize("window,allowance", ((262_144, 232_653), (131_072, 108_135)))
def test_cold_fits_reserve_output_and_five_percent_framing(
    monkeypatch, harness: str, window: int, allowance: int,
) -> None:
    from core.fork_fit import ColdFoldInputTooLarge
    from core.fork_render import fit_cold_fork_input

    admission = _admission(monkeypatch, harness, window=window)
    request = admission.provider_request
    baseline = fit_cold_fork_input(request, provider_surface_tokens=0)
    surface = allowance - baseline.input_tokens
    exact = fit_cold_fork_input(request, provider_surface_tokens=surface)

    assert admission.request.first_included_seq is None
    assert exact.input_tokens == allowance
    assert exact.messages == baseline.messages
    assert exact.first_included_seq == 1
    with pytest.raises(ColdFoldInputTooLarge):
        fit_cold_fork_input(request, provider_surface_tokens=surface + 1)


def test_one_million_cold_fit_omits_old_source_only_with_explicit_boundary(
    monkeypatch,
) -> None:
    from cache_warm_fixtures import make_exact_replay_selection
    from core.fork_render import fit_cold_fork_input

    # Dense identifiers are the worst real case: they tokenize near one token
    # per byte, so this old turn cannot fit even a 1M window.
    dense = " ".join(
        hashlib.sha256(str(index).encode()).hexdigest()[:8]
        for index in range(110_000)
    )
    source = make_exact_replay_selection("thread-1", [
        [("user", "older ask"), ("assistant", "OLD_NEAR_LIMIT " + dense)],
        [("user", "newest ask"), ("assistant", "newest answer")],
    ]).tail
    admission = _admission(
        monkeypatch, "claude-code", window=1_000_000, projection=source,
    )
    fitted = fit_cold_fork_input(
        admission.provider_request, provider_surface_tokens=29_384,
    )
    body = "\n".join(message["content"] for message in fitted.messages)

    assert fitted.first_included_seq == 3
    assert fitted.omitted_units == 1
    assert "OLD_NEAR_LIMIT" not in body
    assert "newest ask" in body and "newest answer" in body
    assert fitted.input_tokens <= 933_616


@pytest.mark.parametrize("window", [131_072, 262_144])
@pytest.mark.parametrize("protected", ["abcdef09" * 128, "约束🌍" * 128], ids=["hex", "unicode"])
def test_cold_boundary_preserves_every_protected_byte(monkeypatch, window, protected):
    from dataclasses import replace
    from core.fork_fit import ColdFoldInputTooLarge
    from core.fork_render import fit_cold_fork_input

    request = _admission(monkeypatch, "claude-code", window=window).provider_request
    block = {"role": "user", "content": protected}
    request = replace(request, cold_source=replace(request.cold_source, protected_blocks=(block,)))
    baseline = fit_cold_fork_input(request, provider_surface_tokens=0)
    surface = window - 16_384 - window // 20 - baseline.input_tokens
    fitted = fit_cold_fork_input(request, provider_surface_tokens=surface)

    assert fitted.messages[0]["content"].partition("\n\n")[0].encode() == protected.encode()
    assert fitted.messages == baseline.messages
    assert fitted.first_included_seq == 1
    with pytest.raises(ColdFoldInputTooLarge):
        fit_cold_fork_input(request, provider_surface_tokens=surface + 1)


def test_codex_cold_surface_counts_and_freezes_nested_schemas(
    monkeypatch,
) -> None:
    from core.fork_cold_count import cold_input_tokens
    from core.fork_render import codex_cold_surface
    from subagents.codex_app_server_events import to_dynamic_tool_specs
    from subagents.managed_launch import CODEX_WRITE_PATH_DEVELOPER_INSTRUCTIONS

    registry = object()
    definitions: tuple[dict[str, Any], ...] = ({
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "write one file",
            "parameters": {"type": "object", "properties": {}},
        },
    },)
    monkeypatch.setattr(
        "subagents.harness_tool_bridge.build_tool_registry", lambda _cwd: registry,
    )
    monkeypatch.setattr(
        "subagents.harness_tool_bridge.dynamic_tool_definitions",
        lambda _registry: definitions,
    )

    writer = _target("codex")
    count, frozen_registry, specs, developer = codex_cold_surface(
        "/work/project", writer=writer,
    )
    payload = {
        "developerInstructions": CODEX_WRITE_PATH_DEVELOPER_INSTRUCTIONS,
        "dynamicTools": to_dynamic_tool_specs(definitions),
    }
    assert count == cold_input_tokens(payload, writer)
    assert frozen_registry is registry
    assert specs == tuple(to_dynamic_tool_specs(definitions))
    assert developer == CODEX_WRITE_PATH_DEVELOPER_INSTRUCTIONS
    definitions[0]["function"]["parameters"]["properties"]["late"] = {"type": "string"}
    assert specs[0]["inputSchema"]["properties"] == {}


def test_external_cold_prompt_does_not_strip_protected_trailing_bytes():
    from core.fork_fit import ColdFoldInput

    body = "protected continuation\n\n"
    fitted = ColdFoldInput(({"role": "user", "content": body},), 10, None, 0)
    assert fitted.as_prompt("instruction") == "instruction\n\n" + body


def test_luna_131k_fits_the_actual_default_tool_surface(monkeypatch, tmp_path):
    from core.fork_codex import _fit_cold_request

    request = _admission(monkeypatch, "codex", window=131_072).provider_request
    frozen, fitted = _fit_cold_request(request, str(tmp_path), kind=CHECKPOINT_KIND)

    assert any(tool["name"] == "continuity_edit" for tool in frozen.dynamic_tool_specs)
    assert fitted.input_tokens <= 108_135
    assert fitted.first_included_seq == 1
    assert fitted.omitted_units == 0


def test_cc_cold_env_enforces_output_cap_without_changing_warm_env(
    monkeypatch,
) -> None:
    from core import fork_cc as cc

    monkeypatch.setattr("subagents.cc_env.clean_claude_env", lambda: {"BASE": "1"})
    monkeypatch.setattr(
        "subagents.cc_env.apply_claude_config_dir", lambda _env, _root: None,
    )
    request = CheckpointProviderForkRequest(
        fork_id="checkpoint-1",
        thread_id="thread-1",
        target=_target("claude-code"),
        instruction="checkpoint",
        reasoning_effort=None,
        working_dir="/work/project",
        max_output_tokens=16_384,
    )

    cold = cc._fork_env(None, request)
    warm = cc._fork_env(SimpleNamespace(claude_dir=None), request)

    assert cold["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "16384"
    assert "CLAUDE_CODE_MAX_OUTPUT_TOKENS" not in warm


def test_cc_declared_context_limit_cannot_be_installed_as_checkpoint() -> None:
    from core.fork_cc_result import result
    from core.checkpoint_fork_kind import CHECKPOINT_KIND
    from core.fork_types import ProviderForkRejected
    from core.fork_types import FORK_CONTEXT_TRUNCATED

    too_long = json.dumps({
        "type": "result", "is_error": True, "result": "Prompt is too long",
    }).encode()
    # Provider error prose is never checkpoint content, and no body fails named.
    with pytest.raises(ProviderForkRejected, match=FORK_CONTEXT_TRUNCATED):
        result(too_long, kind=CHECKPOINT_KIND)

    unrelated = json.dumps({
        "type": "result", "is_error": True, "result": "usable checkpoint",
    }).encode()
    accepted = result(unrelated, kind=CHECKPOINT_KIND)
    assert accepted.raw_body == "usable checkpoint"
    assert accepted.diagnostic == "usable checkpoint"


def test_cc_checkpoint_preserves_every_auto_continued_generation() -> None:
    from core.fork_cc_result import result
    from core.checkpoint_fork_kind import CHECKPOINT_KIND

    stdout = b"\n".join((
        json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "first part"}]},
        }).encode(),
        json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "second part"}]},
        }).encode(),
        json.dumps({
            "type": "result", "is_error": False, "result": "second part",
        }).encode(),
    ))

    assert result(stdout, kind=CHECKPOINT_KIND).raw_body == "first part\nsecond part"


@pytest.mark.asyncio
async def test_codex_cold_adapter_uses_the_surface_admission_counted(
    tmp_path, monkeypatch, isolated_db,
) -> None:
    seed_vendor_checkpoint_thread([_target("codex")])
    from core import fork_codex as codex
    from core import fork_render
    from core.fork_cold_count import cold_input_tokens

    registry = object()
    definitions = ({
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "write",
            "parameters": {"type": "object"},
        },
    },)
    observed = {}
    counted = {}
    real_fit = fork_render.fit_cold_fork_input

    def observe_fit(request, *, provider_surface_tokens):
        counted["request"] = request
        counted["surface"] = provider_surface_tokens
        return real_fit(request, provider_surface_tokens=provider_surface_tokens)

    monkeypatch.setattr(fork_render, "fit_cold_fork_input", observe_fit)

    class Session:
        def __init__(self, **kwargs):
            observed.update(kwargs)
            self.on_event = kwargs["on_event"]

        async def start(self):
            await self.on_event({"kind": "assistant_text", "text": "checkpoint"})
            await self.on_event({"kind": "result", "token_volume": None})
            return True

        async def terminate(self):
            return None

    monkeypatch.setattr(
        "subagents.codex_app_server.CodexAppServerSession", Session,
    )
    request = CheckpointProviderForkRequest(
        fork_id="checkpoint-1",
        thread_id="thread-1",
        target=_target("codex"),
        instruction="checkpoint",
        cold_source=cold_source("history"),
        reasoning_effort="high",
        working_dir=str(tmp_path),
        binary="codex",
        tool_registry=registry,
        dynamic_tool_specs=definitions,
        developer_instructions="frozen developer surface",
    )

    result = await codex.invoke_fork(request, asyncio.Event(), kind=CHECKPOINT_KIND)

    assert result.raw_body == "checkpoint"
    assert observed["tool_registry"] is registry
    assert observed["dynamic_tools"] is counted["request"].dynamic_tool_specs
    assert observed["developer_instructions"] == counted["request"].developer_instructions
    assert observed["developer_instructions"].startswith("frozen developer surface")
    assert counted["surface"] == cold_input_tokens({
        "dynamicTools": observed["dynamic_tools"],
        "developerInstructions": observed["developer_instructions"],
    }, counted["request"].target)


@pytest.mark.asyncio
async def test_codex_real_thread_start_serializes_the_frozen_surface() -> None:
    from subagents.codex_app_server import CodexAppServerSession
    from subagents.managed_launch import CODEX_WRITE_PATH_DEVELOPER_INSTRUCTIONS

    specs = ({
        "type": "function",
        "name": "write_file",
        "description": "write",
        "inputSchema": {"type": "object"},
    },)
    calls = []

    async def request(method, params):
        calls.append((method, params))
        return {"thread": {"id": "checkpoint-thread"}}

    session = CodexAppServerSession(
        binary="codex",
        task="checkpoint",
        dynamic_tools=specs,
        tool_registry=object(),
        developer_instructions=CODEX_WRITE_PATH_DEVELOPER_INSTRUCTIONS,
        ephemeral=True,
        allow_provider_model_fallback=False,
    )
    session._request = request

    await session._start_or_resume_thread()

    assert calls == [("thread/start", {
        "sandbox": "read-only",
        "approvalPolicy": "never",
        "developerInstructions": CODEX_WRITE_PATH_DEVELOPER_INSTRUCTIONS,
        "dynamicTools": list(specs),
        "environments": [],
        "ephemeral": True,
        "allowProviderModelFallback": False,
    })]


@pytest.mark.asyncio
async def test_codex_checkpoint_keeps_body_after_unrelated_terminal_failure(
    tmp_path, monkeypatch, isolated_db,
) -> None:
    seed_vendor_checkpoint_thread([_target("codex")])
    from core import fork_codex as codex

    class Session:
        def __init__(self, **kwargs):
            self.on_event = kwargs["on_event"]

        async def start(self):
            await self.on_event({
                "kind": "assistant_text",
                "text": "usable paid checkpoint",
            })
            await self.on_event({
                "kind": "turn_failed",
                "cause": "process_exited",
                "message": "unrelated terminal failure",
            })
            return True

        async def terminate(self):
            return None

    monkeypatch.setattr(
        "subagents.codex_app_server.CodexAppServerSession", Session,
    )
    request = CheckpointProviderForkRequest(
        fork_id="checkpoint-1",
        thread_id="thread-1",
        target=_target("codex"),
        instruction="checkpoint",
        cold_source=cold_source(),
        reasoning_effort="high",
        working_dir=str(tmp_path),
        binary="codex",
    )

    result = await codex.invoke_fork(request, asyncio.Event(), kind=CHECKPOINT_KIND)

    assert result.raw_body == "usable paid checkpoint"
    assert result.diagnostic == "unrelated terminal failure"


@pytest.mark.asyncio
async def test_codex_context_limit_retains_partial_checkpoint(
    tmp_path, monkeypatch, isolated_db,
) -> None:
    seed_vendor_checkpoint_thread([_target("codex")])
    from core import fork_codex as codex
    from core.fork_types import FORK_CONTEXT_TRUNCATED

    class Session:
        def __init__(self, **kwargs):
            self.on_event = kwargs["on_event"]

        async def start(self):
            await self.on_event({
                "kind": "assistant_text",
                "text": "incomplete checkpoint",
            })
            await self.on_event({
                "kind": "turn_failed",
                "cause": "context_window_exceeded",
                "message": "Prompt is too long",
            })
            return True

        async def terminate(self):
            return None

    monkeypatch.setattr(
        "subagents.codex_app_server.CodexAppServerSession", Session,
    )
    request = CheckpointProviderForkRequest(
        fork_id="checkpoint-1",
        thread_id="thread-1",
        target=_target("codex"),
        instruction="checkpoint",
        cold_source=cold_source(),
        reasoning_effort="high",
        working_dir=str(tmp_path),
        binary="codex",
    )

    result = await codex.invoke_fork(request, asyncio.Event(), kind=CHECKPOINT_KIND)
    assert result.raw_body == "incomplete checkpoint"
    assert result.diagnostic == FORK_CONTEXT_TRUNCATED
