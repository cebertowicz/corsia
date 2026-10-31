from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.usefixtures("signed_in_defaults")  # a harness turn needs a signed-in Default: no ambient login
DISCOVERED_MODELS = {'claude-code': ('claude-code/reviewer',), 'codex': ('codex/reviewer',)}


def _target(harness: str = "native"):
    from llm.resolution import ExecutionTarget

    selector = {
        "native": "openai/reviewer",
        "claude-code": "fable",
        "codex": "gpt-5.6-sol",
    }[harness]
    return ExecutionTarget(
        catalog_id=f"{harness}/reviewer",
        harness=harness,
        provider=harness,
        backend="api" if harness == "native" else harness,
        transport_model=selector,
        expected_reported_model=selector,
        context_window=200_000,
        available=True,
        availability_reason=None,
        display_name="Exact Reviewer",
        supports_reasoning=True,
    )


class _ClaudeProcess:
    def __init__(self, envelope: dict[str, object]) -> None:
        self.returncode = 0
        self._envelope = envelope

    async def communicate(self, _value: bytes) -> tuple[bytes, bytes]:
        return json.dumps(self._envelope).encode(), b""


def _claude_envelope(
    *,
    model_usage: dict[str, object] | None = None,
    usage: dict[str, object] | None = None,
    total_cost_usd: float | None = None,
) -> dict[str, object]:
    envelope: dict[str, object] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "num_turns": 1,
        "modelUsage": model_usage or {"fable": {"contextWindow": 1_000_000}},
        "permission_denials": [],
        "structured_output": {"verdict": "allow"},
    }
    if usage is not None:
        envelope["usage"] = usage
    if total_cost_usd is not None:
        envelope["total_cost_usd"] = total_cost_usd
    return envelope


def _invoke_kwargs(**overrides: Any) -> dict[str, Any]:
    values = {
        "system_prompt": "fixed policy",
        "user_prompt": "{}",
        "output_schema": {"type": "object"},
        "validator": lambda raw: raw,
        "max_output_tokens": 100,
    }
    values.update(overrides)
    return values


def _codex_token_usage() -> dict[str, object]:
    counts = {
        "inputTokens": 10,
        "cachedInputTokens": 0,
        "outputTokens": 5,
        "totalTokens": 15,
    }
    return {"total": counts, "last": counts, "modelContextWindow": 200_000}


def test_session_limit_is_unavailable_before_rate_limit_retry() -> None:
    from llm.isolated_structured import (
        IsolatedStructuredUnavailableError,
        _classify_claude_error,
    )

    assert isinstance(
        _classify_claude_error(
            "You've hit your session limit. Resets at 4:20 PM (rate_limit_error: 429)",
            "provider failed",
        ),
        IsolatedStructuredUnavailableError,
    )


def test_claude_structured_env_binds_the_explicit_account_root_over_an_inherited_override(tmp_path) -> None:
    from llm import isolated_structured

    account_root = str((tmp_path / "account-home").resolve())
    request = isolated_structured.IsolatedStructuredRequest(
        system_prompt="policy",
        user_prompt="payload",
        output_schema={"type": "object"},
        max_output_tokens=100,
    )

    env = isolated_structured._claude_structured_env(
        request,
        clean_env=lambda: {
            "CLAUDE_CONFIG_DIR": "/inherited/custom-claude-root",
            "PATH": "/bin",
        },
        claude_dir=account_root,
    )

    # There is no ambient Default root to leave unset: every launch names its account's home.
    assert env["CLAUDE_CONFIG_DIR"] == account_root


def _patch_native(monkeypatch, module, response: object) -> None:
    async def chat_completion(**_kwargs):
        return response

    monkeypatch.setattr(
        module,
        "_make_provider_params",
        lambda model: {"model": model, "api_key": None, "api_base": None},
    )
    monkeypatch.setattr(module, "_chat_completion", chat_completion)


def _assert_telemetry(telemetry, target, **expected: Any) -> None:
    values = asdict(telemetry)
    assert values.pop("elapsed_ms") > 0
    baseline = {
        "catalog_id": target.catalog_id,
        "harness": target.harness,
        "transport_model": target.transport_model,
        "reported_model": target.expected_reported_model,
        "expected_reported_model": target.expected_reported_model,
        "prompt_tokens": None,
        "completion_tokens": None,
        "cache_creation_tokens": None,
        "cache_read_tokens": None,
        "total_tokens": None,
        "cost_usd": None,
        "cost_source": None,
        "usage_available": True,
    }
    baseline.update(expected)
    assert values == baseline


def test_claude_schema_wrapper_preserves_root_relative_definitions() -> None:
    from llm import isolated_structured

    wrapped = isolated_structured._wrap_claude_output_schema(
        {
            "$defs": {"item": {"type": "string"}},
            "type": "object",
            "properties": {"value": {"$ref": "#/$defs/item"}},
        }
    )

    assert wrapped["$defs"] == {"item": {"type": "string"}}
    assert "$defs" not in wrapped["properties"]["payload"]
    assert wrapped["properties"]["payload"]["properties"]["value"] == {
        "$ref": "#/$defs/item"
    }


@pytest.mark.asyncio
async def test_isolated_structured_dispatch_keeps_caller_policy_and_validation(
    monkeypatch,
) -> None:
    from llm import isolated_structured

    calls: list[dict[str, Any]] = []
    validated: list[object] = []
    raw = '{"schema_version":1,"verdict":"allow"}'

    async def invoke_native(target, request, **kwargs):
        calls.append({"target": target, "request": request, **kwargs})
        return raw

    def validate(value: object) -> dict[str, object]:
        validated.append(value)
        return {"validated": value}

    monkeypatch.setattr(
        isolated_structured,
        "_invoke_native_structured",
        invoke_native,
    )
    result = await isolated_structured.invoke_isolated_structured(
        _target(),
        system_prompt="fixed policy",
        user_prompt='{"untrusted":"payload"}',
        output_schema={"type": "object", "additionalProperties": False},
        validator=validate,
        max_output_tokens=777,
        reasoning_effort="low",
        timeout_seconds=9.0,
    )

    assert result == {"validated": raw}
    assert validated == [raw]
    assert calls == [
        {
            "target": _target(),
            "request": isolated_structured.IsolatedStructuredRequest(
                system_prompt="fixed policy",
                user_prompt='{"untrusted":"payload"}',
                output_schema={"type": "object", "additionalProperties": False},
                max_output_tokens=777,
                reasoning_effort="low",
                timeout_seconds=9.0,
            ),
        }
    ]


def test_native_structured_refuses_truncated_output_by_default() -> None:
    from llm import isolated_structured

    target = _target()
    request = isolated_structured.IsolatedStructuredRequest(
        system_prompt="fixed policy",
        user_prompt="{}",
        output_schema={"type": "object"},
        max_output_tokens=8_192,
    )
    envelope = isolated_structured._native_raw_result_envelope(
        target,
        SimpleNamespace(
            content='{"verdict":"allow"',
            finish_reason="length",
            tool_calls=None,
            reported_model=target.expected_reported_model,
            reasoning_content=None,
            usage=None,
        ),
        provider_model=target.transport_model,
    )

    with pytest.raises(
        isolated_structured.IsolatedStructuredOutputTruncatedError,
        match="reviewer_output_truncated",
    ):
        isolated_structured._materialize_native_raw_result(target, request, envelope)


@pytest.mark.asyncio
async def test_isolated_structured_requires_exact_available_target_and_binary(
    monkeypatch,
) -> None:
    from dataclasses import replace

    from llm import isolated_structured

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("transport must not run")

    monkeypatch.setattr(
        isolated_structured,
        "_invoke_claude_code_structured",
        forbidden,
    )
    unavailable = replace(
        _target("claude-code"),
        available=False,
        availability_reason="connection_unavailable",
    )
    kwargs = {
        "system_prompt": "policy",
        "user_prompt": "{}",
        "output_schema": {"type": "object"},
        "validator": lambda raw: raw,
        "max_output_tokens": 10,
        "timeout_seconds": 1.0,
    }

    with pytest.raises(
        isolated_structured.IsolatedStructuredError,
        match="connection_unavailable",
    ):
        await isolated_structured.invoke_isolated_structured(unavailable, **kwargs)

    with pytest.raises(
        isolated_structured.IsolatedStructuredError,
        match="binary",
    ):
        await isolated_structured.invoke_isolated_structured(
            _target("claude-code"),
            **kwargs,
        )


def test_codex_host_review_constructs_real_app_server_session(tmp_path, isolated_db, monkeypatch) -> None:
    """Keep the isolated adapter bound to the production transport signature."""
    from llm import isolated_structured
    from subagents.codex_app_server import CodexAppServerSession

    request = isolated_structured.IsolatedStructuredRequest(
        system_prompt="fixed policy",
        user_prompt="{}",
        output_schema={"type": "object"},
        max_output_tokens=100,
    )
    from tests.native_checkpoint_fixtures import seed_vendor_checkpoint_thread
    from llm.isolated_admission import admit_structured
    from connections.account_operation import prepare_operation_home

    seed_vendor_checkpoint_thread([_target('codex')])
    monkeypatch.setattr('llm.codex_rollout_files.default_codex_sessions_root',
                        lambda: tmp_path / 'home' / 'sessions')
    request = admit_structured(_target('codex'), request)
    request = prepare_operation_home(request, tmp_path / 'home', kind='detached',
                                     model=_target('codex').catalog_id)
    capture = SimpleNamespace(on_event=None, on_exit=None, on_raw_message=None)

    session = isolated_structured._new_codex_invocation_session(
        _target("codex"),
        request,
        capture,
        binary="codex",
        working_dir=tmp_path / "cwd",
        codex_home=tmp_path / "home",
        session_factory=None,
    )

    assert isinstance(session, CodexAppServerSession)
    assert session.thread_id is None


@pytest.mark.asyncio
async def test_codex_host_review_captures_terminal_with_typed_usage() -> None:
    """Internal token accounting must not corrupt the strict result envelope."""
    from infra.provider_usage import TokenUsage
    from llm import isolated_structured

    request = isolated_structured.IsolatedStructuredRequest(
        system_prompt="fixed policy",
        user_prompt="{}",
        output_schema={"type": "object"},
        max_output_tokens=100,
    )
    terminal = asyncio.get_running_loop().create_future()
    capture = isolated_structured._CodexInvocationCapture(request, terminal)

    await capture.on_event(
        {
            "kind": "result",
            "is_error": False,
            "stop_reason": "completed",
            "token_volume": TokenUsage(input_uncached=10, output=5),
        }
    )

    assert terminal.done()
    assert capture.events == [
        {"kind": "result", "is_error": False, "stop_reason": "completed"}
    ]


@pytest.mark.asyncio
async def test_codex_host_review_real_terminal_pipeline_is_strict_json(
    tmp_path, isolated_db, monkeypatch,
) -> None:
    """Exercise the production event producer through the isolated capture."""
    from llm import isolated_structured

    target = _target("codex")
    request = isolated_structured.IsolatedStructuredRequest(
        system_prompt="fixed policy",
        user_prompt="{}",
        output_schema={"type": "object"},
        max_output_tokens=100,
    )
    from tests.native_checkpoint_fixtures import seed_vendor_checkpoint_thread
    from llm.isolated_admission import admit_structured
    from connections.account_operation import prepare_operation_home

    seed_vendor_checkpoint_thread([_target('codex')])
    monkeypatch.setattr('llm.codex_rollout_files.default_codex_sessions_root',
                        lambda: tmp_path / 'home' / 'sessions')
    request = admit_structured(_target('codex'), request)
    request = prepare_operation_home(request, tmp_path / 'home', kind='detached',
                                     model=_target('codex').catalog_id)
    terminal = asyncio.get_running_loop().create_future()
    capture = isolated_structured._CodexInvocationCapture(request, terminal)
    working_dir = tmp_path / "cwd"
    session = isolated_structured._new_codex_invocation_session(
        target,
        request,
        capture,
        binary="codex",
        working_dir=working_dir,
        codex_home=tmp_path / "home",
        session_factory=None,
    )
    capture.session = session
    capture.live_state = isolated_structured._validate_codex_raw_protocol(
        target,
        request,
        working_dir=str(working_dir),
        request_log=[],
        raw_messages=[],
    )
    session.thread_id = "thread-1"
    session._active_turn_id = "turn-1"

    await session._record_reported_model(target.expected_reported_model)
    await session._handle_notification(
        "item/completed",
        {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "item": {
                "type": "agentMessage",
                "id": "message-1",
                "text": '{"verdict":"allow"}',
            },
        },
    )
    await session._handle_notification(
        "thread/tokenUsage/updated",
        {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "tokenUsage": _codex_token_usage(),
        },
    )
    await session._handle_notification(
        "turn/completed",
        {
            "threadId": "thread-1",
            "turn": {"id": "turn-1", "status": "completed"},
        },
    )

    assert terminal.done()
    assert all("token_volume" not in event for event in capture.events)
    assert isolated_structured._validate_strict_codex_events(
        target, request, capture.events
    ) == (True, True)


@pytest.mark.asyncio
@pytest.mark.parametrize("reported_model", [None, "openai/substituted"])
async def test_native_host_review_disables_retries_and_rejects_unproven_model(
    monkeypatch,
    reported_model: str | None,
) -> None:
    from llm import isolated_structured
    from llm.router import LLMResponse

    calls: list[dict[str, Any]] = []

    async def chat_completion(**kwargs):
        calls.append(kwargs)
        return LLMResponse(
            content='{"verdict":"allow"}',
            reported_model=reported_model,
        )

    monkeypatch.setattr(
        isolated_structured,
        "_make_provider_params",
        lambda model: {"model": model, "api_key": None, "api_base": None},
    )
    monkeypatch.setattr(
        isolated_structured,
        "_chat_completion",
        chat_completion,
    )

    with pytest.raises(
        isolated_structured.IsolatedStructuredError,
        match="reported model",
    ):
        await isolated_structured.invoke_isolated_structured(
            _target(),
            system_prompt="fixed policy",
            user_prompt="{}",
            output_schema={"type": "object"},
            validator=lambda raw: raw,
            max_output_tokens=100,
        )

    assert len(calls) == 1
    assert calls[0]["retry_transient_errors"] is False


@pytest.mark.asyncio
async def test_native_host_review_preserves_rate_limit_failure_classification(
    monkeypatch,
) -> None:
    from llm import isolated_structured
    from llm.router import LLMResponse

    async def chat_completion(**_kwargs):
        return LLMResponse(
            content="Error calling LLM: Rate limited by the API provider.",
            finish_reason="error",
        )

    monkeypatch.setattr(
        isolated_structured,
        "_make_provider_params",
        lambda model: {"model": model, "api_key": None, "api_base": None},
    )
    monkeypatch.setattr(
        isolated_structured,
        "_chat_completion",
        chat_completion,
    )

    with pytest.raises(isolated_structured.IsolatedStructuredRateLimitError):
        await isolated_structured.invoke_isolated_structured(
            _target(),
            system_prompt="fixed policy",
            user_prompt="{}",
            output_schema={"type": "object"},
            validator=lambda raw: raw,
            max_output_tokens=100,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("finish_reason", "content", "error_type"),
    [
        ("error", "HTTP 503 error: provider_unavailable", "IsolatedStructuredProviderError"),
        ("length", "partial reviewer response", "IsolatedStructuredOutputTruncatedError"),
    ],
)
async def test_native_host_review_classifies_terminal_provider_stops(
    monkeypatch, finish_reason, content, error_type,
) -> None:
    from llm import isolated_structured
    from llm.router import LLMResponse

    async def chat_completion(**_kwargs):
        return LLMResponse(content=content, finish_reason=finish_reason)

    monkeypatch.setattr(isolated_structured, "_make_provider_params", lambda model: {"model": model, "api_key": None, "api_base": None})
    monkeypatch.setattr(isolated_structured, "_chat_completion", chat_completion)

    with pytest.raises(getattr(isolated_structured, error_type)):
        await isolated_structured.invoke_isolated_structured(
            _target(), system_prompt="fixed policy", user_prompt="{}",
            output_schema={"type": "object"}, validator=lambda raw: raw,
            max_output_tokens=100,
        )


@pytest.mark.asyncio
async def test_claude_host_review_disables_retries_and_terminal_title(
    monkeypatch,
) -> None:
    from llm import isolated_structured

    spawned: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def spawn(*argv, **kwargs):
        spawned.append((argv, kwargs))
        envelope = _claude_envelope()
        envelope["structured_output"] = {
            "payload": envelope["structured_output"],
        }
        return _ClaudeProcess(envelope)

    monkeypatch.setattr(isolated_structured.asyncio, "create_subprocess_exec", spawn)

    result = await isolated_structured._invoke_claude_code_structured(
        _target("claude-code"),
        isolated_structured.IsolatedStructuredRequest(
            system_prompt="fixed policy",
            user_prompt="{}",
            output_schema={"type": "object"},
            max_output_tokens=100,
        ),
        binary="claude",
        clean_env=lambda: {"PATH": "/bin"},
    )

    assert result == {"verdict": "allow"}
    argv, kwargs = spawned[0]
    schema_arg = argv[argv.index("--json-schema") + 1]
    assert json.loads(schema_arg) == {
        "additionalProperties": False,
        "properties": {"payload": {"type": "object"}},
        "required": ["payload"],
        "type": "object",
    }
    assert argv[-2:] == ("--max-turns", "2")
    from vendor_account_fixtures import default_home

    assert kwargs["env"] == {
        "PATH": "/bin",
        "CLAUDE_CODE_DISABLE_BUNDLED_SKILLS": "1",
        "CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1",
        "CLAUDE_CODE_MAX_RETRIES": "0",
        "CLAUDE_CONFIG_DIR": str(Path(default_home("claude-code")).resolve()),
    }


@pytest.mark.asyncio
async def test_claude_host_review_rejects_auxiliary_model_usage(monkeypatch) -> None:
    from llm import isolated_structured

    async def spawn(*_argv, **_kwargs):
        return _ClaudeProcess(
            _claude_envelope(
                model_usage={
                    "fable": {"contextWindow": 1_000_000},
                    "claude-haiku-4-5": {"contextWindow": 200_000},
                }
            )
        )

    monkeypatch.setattr(isolated_structured.asyncio, "create_subprocess_exec", spawn)

    with pytest.raises(
        isolated_structured.IsolatedStructuredError,
        match="additional model",
    ):
        await isolated_structured._invoke_claude_code_structured(
            _target("claude-code"),
            isolated_structured.IsolatedStructuredRequest(
                system_prompt="fixed policy",
                user_prompt="{}",
                output_schema={"type": "object"},
                max_output_tokens=100,
            ),
            binary="claude",
            clean_env=lambda: {"PATH": "/bin"},
        )


@pytest.mark.asyncio
async def test_native_host_review_accepts_only_matching_reported_model(
    monkeypatch,
) -> None:
    from llm import isolated_structured
    from llm.router import LLMResponse

    async def chat_completion(**_kwargs):
        return LLMResponse(
            content='{"verdict":"allow"}',
            reported_model="reviewer",
        )

    monkeypatch.setattr(
        isolated_structured,
        "_make_provider_params",
        lambda model: {"model": model, "api_key": None, "api_base": None},
    )
    monkeypatch.setattr(
        isolated_structured,
        "_chat_completion",
        chat_completion,
    )

    assert (
        await isolated_structured.invoke_isolated_structured(
            _target(),
            system_prompt="fixed policy",
            user_prompt="{}",
            output_schema={"type": "object"},
            validator=lambda raw: raw,
            max_output_tokens=100,
        )
        == '{"verdict":"allow"}'
    )


def test_codex_host_review_rejects_provider_retry_frames() -> None:
    from llm import isolated_structured

    state = isolated_structured._CodexProtocolState(
        requested_model="gpt-5.6-sol",
        expected_reported_model="gpt-5.6-sol",
        working_dir="/tmp/reviewer",
        output_schema={"type": "object"},
        user_prompt="{}",
        developer_instructions="fixed policy",
        allow_provider_retries=False,
        thread_id="thread-1",
        turn_id="turn-1",
        turn_started=True,
    )
    with pytest.raises(
        isolated_structured.IsolatedStructuredError,
        match="retry",
    ):
        isolated_structured._validate_raw_codex_message(
            {
                "method": "error",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "willRetry": True,
                    "error": {"message": "rate limited"},
                },
            },
            request_log=[],
            state=state,
        )


def test_codex_host_review_preserves_terminal_rate_limit_classification() -> None:
    from llm import isolated_structured

    state = isolated_structured._CodexProtocolState(
        requested_model="gpt-5.6-sol",
        expected_reported_model="gpt-5.6-sol",
        working_dir="/tmp/reviewer",
        output_schema={"type": "object"},
        user_prompt="{}",
        developer_instructions="fixed policy",
        allow_provider_retries=False,
        thread_id="thread-1",
        turn_id="turn-1",
        turn_started=True,
    )

    with pytest.raises(isolated_structured.IsolatedStructuredRateLimitError):
        isolated_structured._validate_raw_codex_message(
            {
                "method": "error",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "willRetry": False,
                    "error": {
                        "code": "rate_limit",
                        "message": "Too many requests",
                    },
                },
            },
            request_log=[],
            state=state,
        )


@pytest.mark.asyncio
async def test_codex_host_review_rejects_post_terminal_protocol_violation() -> None:
    from llm import isolated_structured

    late_errors: list[BaseException] = []

    class LateViolationSession:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs
            self.request_log: list[dict[str, Any]] = []

        async def start(self) -> bool:
            await self.kwargs["on_event"](
                {"kind": "model_reported", "model": "gpt-5.6-sol"}
            )
            return True

        async def start_turn(self, _text: str, *, output_schema: dict) -> bool:
            assert output_schema == {"type": "object"}
            for message in (
                {
                    "method": "thread/started",
                    "params": {"thread": {"id": "thread-1"}},
                },
                {
                    "method": "turn/started",
                    "params": {
                        "threadId": "thread-1",
                        "turn": {"id": "turn-1", "items": []},
                    },
                },
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread-1",
                        "turn": {
                            "id": "turn-1",
                            "status": "completed",
                            "items": [],
                        },
                    },
                },
            ):
                await self.kwargs["on_raw_message"](message)
            await self.kwargs["on_event"](
                {"kind": "assistant_text", "text": '{"verdict":"allow"}'}
            )
            await self.kwargs["on_event"](
                {"kind": "result", "is_error": False, "stop_reason": "completed"}
            )
            return True

        async def close(self) -> int:
            try:
                await self.kwargs["on_raw_message"](
                    {
                        "id": 99,
                        "method": "item/commandExecution/requestApproval",
                        "params": {},
                    }
                )
            except BaseException as exc:
                late_errors.append(exc)
            await self.kwargs["on_exit"](0, "")
            return 0

    captured: list[dict[str, Any]] = []

    async def observe_raw(envelope, _usage) -> None:
        captured.append(envelope)

    request = isolated_structured.IsolatedStructuredRequest(
        system_prompt="fixed policy",
        user_prompt="{}",
        output_schema={"type": "object"},
        max_output_tokens=100,
        raw_result_observer=observe_raw,
    )
    with pytest.raises(
        isolated_structured.IsolatedStructuredError,
        match="server tool/request",
    ):
        await isolated_structured._invoke_codex_structured(
            _target("codex"),
            request,
            binary="codex",
            session_factory=LateViolationSession,
        )

    assert len(late_errors) == 1
    assert isinstance(late_errors[0], isolated_structured.IsolatedStructuredError)
    assert "server tool/request" in str(late_errors[0])
    assert len(captured) == 1
    assert any(
        message.get("method") == "item/commandExecution/requestApproval"
        for message in captured[0]["result"]["raw_messages"]
    )


@pytest.mark.asyncio
async def test_openai_host_review_policy_disables_sdk_and_corsia_retries(
    monkeypatch,
) -> None:
    from llm.providers import openai as provider

    attempts = 0

    async def fail(**_kwargs):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("rate limit")

    class Client:
        def __init__(self) -> None:
            self.responses = SimpleNamespace(create=fail)

        def with_options(self, *, max_retries: int):
            assert max_retries == 0
            return self

    monkeypatch.setattr(provider, "_get_client", lambda _api_key: Client())
    monkeypatch.setattr(provider, "_is_sdk_transient", lambda _error: True)

    response = await provider.openai_completion(
        [{"role": "user", "content": "review"}],
        model="openai/reviewer",
        retry_transient_errors=False,
    )

    assert attempts == 1
    assert response.finish_reason == "error"


@pytest.mark.asyncio
async def test_anthropic_host_review_policy_disables_corsia_retries(
    monkeypatch,
) -> None:
    from llm.providers import anthropic as provider

    attempts = 0

    async def fail(**_kwargs):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("rate limit")

    client = SimpleNamespace(messages=SimpleNamespace(create=fail))
    monkeypatch.setattr(provider, "_get_client", lambda _key, _base: client)

    response = await provider.anthropic_completion(
        [{"role": "user", "content": "review"}],
        model="anthropic/reviewer",
        retry_transient_errors=False,
    )

    assert attempts == 1
    assert response.finish_reason == "error"


@pytest.mark.asyncio
async def test_litellm_host_review_policy_disables_all_retries(
    monkeypatch,
) -> None:
    from llm import base as provider

    calls: list[dict[str, Any]] = []

    async def fail(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("rate limit")

    monkeypatch.setattr(provider, "acompletion", fail)

    response = await provider.base_completion(
        [{"role": "user", "content": "review"}],
        model="external/reviewer",
        retry_transient_errors=False,
    )

    assert len(calls) == 1
    assert calls[0]["num_retries"] == 0
    assert response.finish_reason == "error"


def test_native_parsers_preserve_provider_reported_model() -> None:
    from llm.base import parse_response as parse_litellm
    from llm.providers.anthropic import _parse_response as parse_anthropic
    from llm.providers.openai_translation import (
        _parse_responses_response as parse_openai,
    )

    openai_response = SimpleNamespace(
        model="gpt-exact",
        output_text="{}",
        output=[],
        status="completed",
        usage=None,
    )
    anthropic_response = SimpleNamespace(
        model="claude-exact",
        content=[],
        stop_reason="end_turn",
        usage=None,
    )
    litellm_response = SimpleNamespace(
        model="local-exact",
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="{}", tool_calls=None),
                finish_reason="stop",
            )
        ],
        usage=None,
    )

    assert (
        parse_openai(openai_response, model="openai/requested").reported_model
        == "gpt-exact"
    )
    assert (
        parse_anthropic(
            anthropic_response,
            model="anthropic/requested",
        ).reported_model
        == "claude-exact"
    )
    assert (
        parse_litellm(litellm_response, model="external/requested").reported_model
        == "local-exact"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    ["normalized", "throwing_sink", "identity_failure"],
)
async def test_native_telemetry_is_normalized_and_noninterfering(
    monkeypatch,
    case: str,
) -> None:
    from llm import isolated_structured

    target = _target("native")
    observed = []
    sink_calls = 0
    _patch_native(
        monkeypatch,
        isolated_structured,
        SimpleNamespace(
            content='{"verdict":"allow"}',
            finish_reason="stop",
            tool_calls=[],
            reported_model=(
                "openai/substituted"
                if case == "identity_failure"
                else target.expected_reported_model
            ),
            usage={
                "prompt_tokens": 101,
                "completion_tokens": 17,
                "cache_creation_tokens": 5,
                "cache_read_tokens": 23,
                "total_tokens": 118,
                "cost_usd": 0.0125,
            },
        ),
    )

    def sink(telemetry) -> None:
        nonlocal sink_calls
        sink_calls += 1
        if case == "throwing_sink":
            raise RuntimeError("telemetry backend unavailable")
        observed.append(telemetry)

    call = isolated_structured.invoke_isolated_structured(
        target,
        **_invoke_kwargs(
            validator=lambda raw: {"validated": raw},
            telemetry_sink=sink,
        ),
    )
    if case == "identity_failure":
        with pytest.raises(
            isolated_structured.IsolatedStructuredError,
            match="reported model mismatch",
        ):
            await call
    else:
        assert await call == {"validated": '{"verdict":"allow"}'}

    assert sink_calls == 1
    if case == "throwing_sink":
        assert not observed
        return
    _assert_telemetry(
        observed.pop(),
        target,
        reported_model=(
            "openai/substituted"
            if case == "identity_failure"
            else target.expected_reported_model
        ),
        prompt_tokens=101,
        completion_tokens=17,
        cache_creation_tokens=5,
        cache_read_tokens=23,
        total_tokens=118,
        cost_usd=0.0125,
        cost_source="corsia_pricing",
    )
    assert not observed


@pytest.mark.asyncio
async def test_claude_isolated_structured_keeps_usage_cost_and_identity_distinct(
    monkeypatch,
) -> None:
    from llm import isolated_structured

    target = _target("claude-code")
    observed = []

    async def spawn(*_argv, **_kwargs):
        envelope = _claude_envelope(
            usage={
                "input_tokens": 211,
                "output_tokens": 31,
                "cache_creation_input_tokens": 7,
                "cache_read_input_tokens": 43,
            },
            total_cost_usd=0.045,
            model_usage={
                target.expected_reported_model: {
                    "contextWindow": 1_000_000,
                    "inputTokens": 999_999,
                    "outputTokens": 888_888,
                }
            },
        )
        envelope["structured_output"] = {
            "payload": envelope["structured_output"],
        }
        return _ClaudeProcess(envelope)

    monkeypatch.setattr(isolated_structured.asyncio, "create_subprocess_exec", spawn)

    result = await isolated_structured.invoke_isolated_structured(
        target,
        **_invoke_kwargs(binary="claude", telemetry_sink=observed.append),
    )

    assert result == {"verdict": "allow"}
    _assert_telemetry(
        observed.pop(),
        target,
        prompt_tokens=211,
        completion_tokens=31,
        cache_creation_tokens=7,
        cache_read_tokens=43,
    )
    assert not observed


@pytest.mark.asyncio
async def test_claude_cost_only_telemetry_does_not_claim_token_usage(
    monkeypatch,
) -> None:
    from llm import isolated_structured

    target = _target("claude-code")
    observed = []

    async def spawn(*_argv, **_kwargs):
        envelope = _claude_envelope(total_cost_usd=0.045)
        envelope["structured_output"] = {
            "payload": envelope["structured_output"],
        }
        return _ClaudeProcess(envelope)

    monkeypatch.setattr(isolated_structured.asyncio, "create_subprocess_exec", spawn)

    result = await isolated_structured.invoke_isolated_structured(
        target,
        **_invoke_kwargs(binary="claude", telemetry_sink=observed.append),
    )

    assert result == {"verdict": "allow"}
    _assert_telemetry(
        observed.pop(),
        target,
        usage_available=False,
    )
    assert not observed


@pytest.mark.asyncio
async def test_native_incomplete_usage_does_not_accept_corsia_derived_cost(
    monkeypatch,
) -> None:
    from llm import isolated_structured

    target = _target("native")
    observed = []
    _patch_native(
        monkeypatch,
        isolated_structured,
        SimpleNamespace(
            content='{"verdict":"allow"}',
            finish_reason="stop",
            tool_calls=[],
            reported_model=target.expected_reported_model,
            usage={
                "prompt_tokens": 101,
                "completion_tokens": 17,
                "cache_read_tokens": 23,
                "total_tokens": 118,
                "cost_usd": 0.0125,
            },
        ),
    )

    result = await isolated_structured.invoke_isolated_structured(
        target,
        **_invoke_kwargs(telemetry_sink=observed.append),
    )

    assert result == '{"verdict":"allow"}'
    _assert_telemetry(
        observed.pop(),
        target,
        prompt_tokens=101,
        completion_tokens=17,
        cache_read_tokens=23,
        total_tokens=118,
    )
    assert not observed


@pytest.mark.asyncio
async def test_codex_isolated_structured_reports_token_usage_notification(
    monkeypatch,
) -> None:
    from llm import isolated_structured

    target = _target("codex")
    observed = []

    async def invoke_codex(_target, request, **_kwargs):
        request._telemetry.reported_model = target.expected_reported_model
        state = isolated_structured._CodexProtocolState(
            requested_model=target.transport_model,
            expected_reported_model=target.expected_reported_model,
            working_dir="/tmp/reviewer",
            output_schema=request.output_schema,
            user_prompt=request.user_prompt,
            developer_instructions=request.system_prompt,
            thread_id="thread-telemetry",
            turn_id="turn-telemetry",
            turn_started=True,
            telemetry=request._telemetry,
        )
        isolated_structured._validate_raw_codex_message(
            {
                "method": "thread/tokenUsage/updated",
                "params": {
                    "threadId": state.thread_id,
                    "turnId": state.turn_id,
                    "tokenUsage": {
                        "total": {
                            "inputTokens": 307,
                            "cachedInputTokens": 59,
                            "outputTokens": 41,
                            "totalTokens": 348,
                        },
                        "last": {"inputTokens": 999_999},
                    },
                },
            },
            request_log=[],
            state=state,
        )
        return '{"verdict":"allow"}'

    monkeypatch.setattr(
        isolated_structured,
        "_invoke_codex_structured",
        invoke_codex,
    )

    result = await isolated_structured.invoke_isolated_structured(
        target,
        **_invoke_kwargs(binary="codex", telemetry_sink=observed.append),
    )

    assert result == '{"verdict":"allow"}'
    _assert_telemetry(
        observed.pop(),
        target,
        prompt_tokens=307,
        completion_tokens=41,
        cache_read_tokens=59,
        total_tokens=348,
    )
    assert not observed


def test_codex_host_review_ignores_unknown_nonessential_notification() -> None:
    """Unknown fire-and-forget telemetry is ignored; unknown requests still fail.

    Regression for lease cc54c91a: the Codex CLI began emitting
    'turn/moderationMetadata', and rejecting it killed every in-flight
    isolated inference (including host-access action reviews).
    """
    from llm import isolated_structured

    state = isolated_structured._CodexProtocolState(
        requested_model="gpt-5.6-sol",
        expected_reported_model="gpt-5.6-sol",
        working_dir="/tmp/reviewer",
        output_schema={"type": "object"},
        user_prompt="{}",
        developer_instructions="fixed policy",
        allow_provider_retries=False,
        thread_id="thread-1",
        turn_id="turn-1",
        turn_started=True,
    )

    # An unknown notification (no id) is tolerated and changes no state.
    isolated_structured._validate_raw_codex_message(
        {
            "method": "turn/moderationMetadata",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "moderation": {"category": "none"},
            },
        },
        request_log=[],
        state=state,
    )
    assert state.thread_id == "thread-1"
    assert state.turn_id == "turn-1"

    # An unknown server REQUEST (id present) still fails loud: it demands a
    # response and may carry tool or permission semantics.
    with pytest.raises(
        isolated_structured.IsolatedStructuredError,
        match="server tool/request",
    ):
        isolated_structured._validate_raw_codex_message(
            {
                "id": 7,
                "method": "turn/moderationMetadata",
                "params": {},
            },
            request_log=[],
            state=state,
        )

    # Known notifications keep their strict shape validation.
    with pytest.raises(
        isolated_structured.IsolatedStructuredError,
        match="rate-limit telemetry",
    ):
        isolated_structured._validate_raw_codex_message(
            {
                "method": "account/rateLimits/updated",
                "params": {"unexpected": True},
            },
            request_log=[],
            state=state,
        )


@pytest.mark.asyncio
async def test_unknown_nonessential_notification_keeps_inference_alive() -> None:
    """An in-flight inference survives a mid-turn unknown notification."""
    from llm import isolated_structured

    class ModerationMidTurnSession:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs
            self.request_log: list[dict[str, Any]] = []

        async def start(self) -> bool:
            await self.kwargs["on_event"](
                {"kind": "model_reported", "model": "gpt-5.6-sol"}
            )
            return True

        async def start_turn(self, _text: str, *, output_schema: dict) -> bool:
            assert output_schema == {"type": "object"}
            for message in (
                {
                    "method": "thread/started",
                    "params": {"thread": {"id": "thread-1"}},
                },
                {
                    "method": "turn/started",
                    "params": {
                        "threadId": "thread-1",
                        "turn": {"id": "turn-1", "items": []},
                    },
                },
                {
                    "method": "turn/moderationMetadata",
                    "params": {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "moderation": {"category": "none"},
                    },
                },
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread-1",
                        "turn": {
                            "id": "turn-1",
                            "status": "completed",
                            "items": [],
                        },
                    },
                },
            ):
                await self.kwargs["on_raw_message"](message)
            await self.kwargs["on_event"](
                {"kind": "assistant_text", "text": '{"verdict":"allow"}'}
            )
            await self.kwargs["on_event"](
                {"kind": "result", "is_error": False, "stop_reason": "completed"}
            )
            return True

        async def close(self) -> int:
            await self.kwargs["on_exit"](0, "")
            return 0

    request = isolated_structured.IsolatedStructuredRequest(
        system_prompt="fixed policy",
        user_prompt="{}",
        output_schema={"type": "object"},
        max_output_tokens=100,
    )

    result = await isolated_structured._invoke_codex_structured(
        _target("codex"),
        request,
        binary="codex",
        session_factory=ModerationMidTurnSession,
    )

    assert result == '{"verdict":"allow"}'


def test_provider_error_class_accepts_only_closed_safe_tokens() -> None:
    from llm.isolated_failure import provider_error_class

    assert provider_error_class('"type": "overloaded_error"') == "provider_error:overloaded_error"
    assert provider_error_class("status 429") == "provider_error:http_429"
    assert provider_error_class("content-type: text/html") == "provider_error"
    assert provider_error_class("error: sk-secret-value") == "provider_error"
    assert provider_error_class("error: eyJhbGciOiJIUzI1NiJ9.payload.signature") == "provider_error"
    assert provider_error_class("error: QWxhZGRpbjpvcGVuIHNlc2FtZQ") == "provider_error"
