"""A Codex checkpoint counts its own output and aggregate tool replies."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from core.checkpoint_fork_kind import CHECKPOINT_KIND

from test_checkpoint_external_adapters import _target, vendor_thread
from core.fork_fit import ColdFoldInput


def target(window=131_072):
    return replace(_target("codex"), context_window=window,
                   transport_model="gpt-4", expected_reported_model="gpt-4")


def test_warm_checkpoint_session_uses_governed_catalog_for_receipt_ids(tmp_path, vendor_thread):
    """Warm checkpoint callbacks must stay top-level so receipt IDs match."""
    from core.fork_codex import CodexWarmSource, _fork_session
    from connections.account_operation import admit_operation, prepare_operation_home
    from test_checkpoint_external_adapters import _boundary, _request

    source = CodexWarmSource(
        thread_id="thread-1",
        boundary=_boundary(),
        provider_thread_id="provider-thread",
        rollout_path=str(tmp_path / "rollout.jsonl"),
        rollout_bytes=0,
        rollout_sha256="0" * 64,
        sessions_root=str(tmp_path / "sessions"),
        working_dir=str(tmp_path),
        reasoning_effort="high",
    )
    session = _fork_session(
        prepare_operation_home(admit_operation("codex", target().catalog_id,
            _request(target(), source, binary="codex")),
            tmp_path / "isolated-home", kind="checkpoint", model=target().catalog_id),
        source,
        source_home=tmp_path / "source-home",
        isolated_home=tmp_path / "isolated-home",
        cwd=str(tmp_path),
        on_event=lambda event: None,
        on_exit=lambda code, error: None,
        fork_tool_result_fits=lambda reply, usage=None: True,
        kind=CHECKPOINT_KIND,
    )

    assert any(value.startswith("model_catalog_json=") for value in session.build_cmd())


@pytest.mark.asyncio
async def test_first_input_nonfit_never_starts_a_codex_session(monkeypatch, tmp_path):
    import asyncio
    from unittest.mock import Mock
    from core import fork_codex as codex
    from core.fork_types import ProviderForkRejected
    from test_checkpoint_external_adapters import _request

    session = Mock()
    monkeypatch.setattr(codex, "_fork_session", session)
    with pytest.raises(ProviderForkRejected, match="initial.*input"):
        await codex._run_app_server_fork(
            _request(target(100), None, binary="unused"), None,
            source_home=tmp_path, isolated_home=tmp_path, cwd=str(tmp_path),
            cancel_event=asyncio.Event(), cold_input=ColdFoldInput((), 101, None, 0),
            kind=CHECKPOINT_KIND,
        )
    session.assert_not_called()


def test_frozen_context_uses_last_receipt_not_cumulative_volume(tmp_path):
    from core.fork_codex_context import frozen_context_tokens

    last = {"input_tokens": 80_000, "output_tokens": 20}
    receipt = {"type": "event_msg", "payload": {"type": "token_count", "info": {
        "last_token_usage": last,
        "total_token_usage": {"input_tokens": 19_000_000, "output_tokens": 200_000},
    }}}
    result = {"type": "response_item", "payload": {
        "type": "function_call_output", "call_id": "edit", "output": " x" * 400,
    }}
    prefix = (json.dumps(receipt) + "\n" + json.dumps(result) + "\n").encode()
    path = tmp_path / "rollout.jsonl"
    path.write_bytes(prefix + json.dumps({"type": "response_item", "payload": {
        "type": "message", "role": "user", "content": " x" * 200_000,
    }}).encode())
    count = frozen_context_tokens(path, len(prefix), target=target(), fallback_tokens=0)
    assert 80_400 < count < 80_500  # No later tail or lifetime volume enters the fork.


@pytest.mark.asyncio
async def test_aggregate_tool_replies_cut_off_before_the_oversized_reply():
    from core.fork_codex_context import CodexForkContext

    context = CodexForkContext(target(), 80_000)
    released = []
    for _ in range(16):
        context.record_receipt({
            "last": {"inputTokens": 80_000, "outputTokens": 25},
            "total": {"inputTokens": 19_000_000, "outputTokens": 200_000},
        })
        reply = {"success": True, "contentItems": [{"type": "inputText", "text": " x" * 4096}]}
        if not context.accept_tool_result(reply):
            break
        released.append(reply)
    assert 0 < len(released) < 16
    assert not context.fits


def test_repeated_current_receipt_keeps_results_not_yet_in_the_reported_prompt():
    from core.fork_codex_context import CodexForkContext

    context = CodexForkContext(target(), 80_000)
    receipt = {
        "last": {"inputTokens": 80_030, "outputTokens": 25},
        "total": {"inputTokens": 19_000_000, "outputTokens": 200_000},
    }
    context.record_receipt(receipt)
    context.accept_tool_result({"contentItems": [{"type": "inputText", "text": " x" * 4096}]})
    assert 84_150 < context.tokens < 84_200
    count = context.tokens
    context.record_receipt(receipt)
    assert context.tokens == count


@pytest.mark.asyncio
async def test_real_codex_callback_withholds_oversized_reply_and_closes_only_its_process(monkeypatch):
    from core.fork_codex_context import CodexForkContext
    from subagents.codex_app_server import CodexAppServerSession

    context = CodexForkContext(target(100), 90)
    writes, killed, calls = [], [], []

    class Pipe:
        def write(self, payload):
            writes.append(payload)

        async def drain(self):
            pass

    async def execute(*_args, **_kwargs):
        calls.append(True)
        return " x" * 20

    def boundary(reply):
        return context.fits if reply is None else context.accept_tool_result(reply)

    monkeypatch.setattr("subagents.harness_tool_bridge.execute_tool", execute)
    session = CodexAppServerSession(
        binary="unused", task="checkpoint", codex_thread_id="provider",
        corsia_thread_id="thread", fork_id="checkpoint", tool_registry=object(),
        fork_tool_result_fits=boundary,
    )
    session._active_turn_id = "turn"
    session._proc = SimpleNamespace(stdin=Pipe(), returncode=None, kill=lambda: killed.append(True))
    await session._handle_server_request({"id": 1, "method": "item/tool/call", "params": {
        "threadId": "provider", "turnId": "turn", "callId": "edit", "tool": "edit",
        "arguments": {},
    }})
    assert calls == [True]
    assert writes == []
    assert killed == [True]
    assert session._closing
    assert session._dynamic_tool_requests_idle.is_set()


@pytest.mark.asyncio
async def test_codex_invocation_returns_partial_checkpoint_when_reply_does_not_fit(monkeypatch, tmp_path, vendor_thread):
    import asyncio
    from core import fork_codex as codex
    from connections.account_operation import admit_operation, prepare_operation_home
    from test_checkpoint_external_adapters import _request

    closed = []

    class Session:
        def __init__(self, **kwargs):
            from infra.turn_volume import CumulativeTurnVolume

            self._turn_volume = CumulativeTurnVolume()
            self.active_turn_id = "checkpoint-turn"
            self.event = kwargs["on_event"]
            self.boundary = kwargs["fork_tool_result_fits"]
            assert kwargs["model"] == "gpt-4"

        async def start(self):
            await self.event({"kind": "call_usage", "turn_id": "old-source-turn",
                              "ordinal": 1, "usage": {"prompt_tokens": 19000000}})
            await self.event({"kind": "assistant_text", "text": "Partial checkpoint."})
            receipt = {
                "last": {"inputTokens": 80, "cachedInputTokens": 30, "outputTokens": 10},
                "total": {"inputTokens": 19000000, "cachedInputTokens": 1000, "outputTokens": 2000},
            }
            await self.event({"kind": "call_usage", "turn_id": "checkpoint-turn", "ordinal": 1,
                              "usage": {"prompt_tokens": 50, "completion_tokens": 10,
                                        "cache_read_tokens": 30, "cache_creation_tokens": 0,
                                        "effective_input_tokens": 80, "total_tokens": 90}})
            assert await self.boundary(None, receipt)
            assert await self.boundary(None, receipt)  # Repeated query is not another call.
            assert not await self.boundary({"contentItems": [{"type": "inputText", "text": " x" * 20}]})
            return True

        async def terminate(self):
            closed.append(True)

    monkeypatch.setattr("subagents.codex_app_server.CodexAppServerSession", Session)
    request = replace(_request(target(100), None, binary="unused"), tool_registry=object())
    request = prepare_operation_home(admit_operation("codex", request.target.catalog_id, request),
        tmp_path, kind="checkpoint", model=request.target.catalog_id)
    result = await codex._run_app_server_fork(
        request, None, source_home=tmp_path, isolated_home=tmp_path, cwd=str(tmp_path),
        cancel_event=asyncio.Event(), cold_input=ColdFoldInput((), 90, None, 0),
        kind=CHECKPOINT_KIND,
    )
    assert result.raw_body == "Partial checkpoint."
    assert result.diagnostic == "checkpoint_context_truncated"
    assert result.usage == {
        "calls": [{"prompt_tokens": 50, "completion_tokens": 10,
                   "cache_read_tokens": 30, "cache_creation_tokens": 0,
                   "effective_input_tokens": 80, "total_tokens": 90}],
    }
    assert closed == [True]
