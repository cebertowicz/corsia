"""Five-step model-switch composition over the clean checkpoint runtime."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from core.checkpoint_fork_kind import CHECKPOINT_KIND


@pytest.mark.asyncio
async def test_same_harness_switch_uses_matching_terminal_receipt(monkeypatch) -> None:
    from infra import context_receipts
    from infra.context_receipts import ContextReceiptIdentity
    from thread.model_source import _live_same_harness_context_usage

    identity = ContextReceiptIdentity(
        "codex",
        "codex/luna",
        3,
        "b" * 64,
    )
    monkeypatch.setattr(
        context_receipts,
        "current_context_receipt_identity",
        lambda *_args, **_kwargs: identity,
    )
    monkeypatch.setattr(
        context_receipts,
        "latest_measured_context_receipt",
        lambda *_args, **_kwargs: {
            "message_id": "assistant-receipt",
            "context_usage": {"total_used": 123_456},
        },
    )

    total = await _live_same_harness_context_usage(
        "thread-1",
        source_harness="codex",
        source_target=SimpleNamespace(catalog_id="codex/luna"),
        destination_target=SimpleNamespace(harness="codex"),
        source_runtime_context_epoch=3,
    )

    assert total == 123_456


@dataclass(frozen=True)
class JourneyResult:
    status: str
    checkpoint_mode: str | None
    destination_model: str
    alternate_models_tried: list[str]
    paid_checkpoint_calls: int
    checkpoint_state_reads: int


class CheckpointModelSwitchHarness:
    def __init__(self, monkeypatch) -> None:
        self.monkeypatch = monkeypatch

    @staticmethod
    def _request():
        from infra.model_switch_types import SwitchRequest

        return SwitchRequest(
            "switch-1",
            "thread-1",
            "native/source",
            "native/target",
            0,
            1,
            0,
            "user",
            "user-1",
            "model_switch",
            "current",
            "2026-08-27T00:00:00+00:00",
            0,
        )

    @staticmethod
    def _state(journey: str):
        return SimpleNamespace(
            active=(object() if journey == "installed_checkpoint_plus_tail_fits" else None),
            attempt=None,
            fits=journey in {
                "full_history_fits",
                "installed_checkpoint_plus_tail_fits",
            },
            mode=None,
            paid=0,
            reads=0,
            status="preparing",
        )

    def _install_driver(self, request, state, journey: str, model_pending) -> None:
        from thread.model_state import ModelSwitchCheckpointRequired

        async def commit(_request):
            if not state.fits:
                raise ModelSwitchCheckpointRequired(
                    _request.source_model, _request.destination_model,
                )
            state.status = "committed"
            return SimpleNamespace(status="committed")

        def checkpoint_state(_request):
            state.reads += 1
            return state.active, state.attempt

        async def start(_request, mode):
            if (
                mode == "warm"
                and journey in {
                    "source_quota_target_truncated_cold",
                    "minimum_target_replay_impossible",
                }
            ):
                return None, False
            if mode == "cold" and journey == "minimum_target_replay_impossible":
                from core.checkpoint_fit import ColdFoldInputTooLarge

                raise ColdFoldInputTooLarge("minimum meaningful replay does not fit")
            state.mode = mode
            state.attempt = SimpleNamespace(mode=mode, status="running", error=None)
            state.active = state.attempt
            return state.attempt, False

        async def fail(_request, _error):
            state.status = "failed"
            return SimpleNamespace(status="failed")

        async def drive(_thread_id):
            return None

        self.monkeypatch.setattr("infra.model_switches.advance_pending_switch", lambda *_a: request)
        self.monkeypatch.setattr(model_pending, "_commit_fitting_switch", commit)
        self.monkeypatch.setattr(model_pending, "_checkpoint_state", checkpoint_state)
        self.monkeypatch.setattr(model_pending, "_start_checkpoint", start)
        self.monkeypatch.setattr(model_pending, "_fail_switch", fail)
        self.monkeypatch.setattr(model_pending, "_drive_destination", drive)

    async def run(self, journey: str) -> JourneyResult:
        from thread import model_pending

        request = self._request()
        state = self._state(journey)
        self._install_driver(request, state, journey, model_pending)

        await model_pending.drive_pending_model_switch(request.thread_id)
        if state.status == "failed" or state.status == "committed":
            return self._result(request, state)
        if journey == "source_quota_target_full_cold":
            state.active = None
            state.attempt = SimpleNamespace(
                mode="warm", status="failed", error="source quota exhausted",
            )
            await model_pending.drive_pending_model_switch(request.thread_id)
        if state.active is not None:
            state.active = None
            state.attempt = SimpleNamespace(
                mode=state.mode, status="installed", error=None,
            )
            state.paid += 1
            state.fits = True
            await model_pending.drive_pending_model_switch(request.thread_id)
        return self._result(request, state)

    @staticmethod
    def _result(request, state) -> JourneyResult:
        return JourneyResult(
            state.status,
            state.mode,
            request.destination_model,
            [],
            state.paid,
            state.reads,
        )


@pytest.fixture
def checkpoint_model_switch_harness(monkeypatch):
    return CheckpointModelSwitchHarness(monkeypatch)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("journey", "expected_mode", "expected_status"),
    (
        ("full_history_fits", None, "committed"),
        ("installed_checkpoint_plus_tail_fits", None, "committed"),
        ("source_warm_checkpoint_required", "warm", "committed"),
        ("source_quota_target_full_cold", "cold", "committed"),
        ("source_quota_target_truncated_cold", "cold", "committed"),
        ("minimum_target_replay_impossible", None, "failed"),
    ),
)
async def test_checkpoint_switch_ladder_has_one_clean_result(
    journey,
    expected_mode,
    expected_status,
    checkpoint_model_switch_harness,
) -> None:
    result = await checkpoint_model_switch_harness.run(journey)

    assert result.status == expected_status
    assert result.checkpoint_mode == expected_mode
    assert result.destination_model == "native/target"
    assert result.alternate_models_tried == []
    assert result.paid_checkpoint_calls <= 1


@pytest.mark.asyncio
async def test_fitting_destination_wins_before_independent_checkpoint(
    checkpoint_model_switch_harness,
) -> None:
    result = await checkpoint_model_switch_harness.run(
        "installed_checkpoint_plus_tail_fits"
    )

    assert result.status == "committed"
    assert result.checkpoint_state_reads == 0


@pytest.mark.asyncio
async def test_installed_race_winner_rechecks_switch_without_completion_wake(
    monkeypatch,
) -> None:
    from thread import model_pending
    from thread.model_state import ModelSwitchCheckpointRequired

    request = CheckpointModelSwitchHarness._request()
    commit_calls = 0
    driven: list[str] = []

    async def commit(_request):
        nonlocal commit_calls
        commit_calls += 1
        if commit_calls == 1:
            raise ModelSwitchCheckpointRequired(
                request.source_model, request.destination_model,
            )
        return SimpleNamespace(status="committed")

    async def start(_request, mode):
        assert mode == "warm"
        return SimpleNamespace(status="installed", id="installed-winner"), True

    async def drive(thread_id):
        driven.append(thread_id)

    monkeypatch.setattr("infra.model_switches.advance_pending_switch", lambda *_a: request)
    monkeypatch.setattr(model_pending, "_commit_fitting_switch", commit)
    monkeypatch.setattr(model_pending, "_checkpoint_state", lambda _request: (None, None))
    monkeypatch.setattr(model_pending, "_start_checkpoint", start)
    monkeypatch.setattr(model_pending, "_drive_destination", drive)

    outcome = await model_pending.drive_pending_model_switch(request.thread_id)

    assert outcome.status == "committed"
    assert commit_calls == 2
    assert driven == [request.thread_id]


@pytest.mark.asyncio
async def test_conflicting_activity_switch_fails_then_next_switch_drives(monkeypatch):
    from dataclasses import replace

    from thread import model_pending
    from thread.model_state import ModelSwitchConflict

    first = CheckpointModelSwitchHarness._request()
    second = replace(first, operation_id="switch-2", thread_id="thread-2")
    requests = {request.thread_id: request for request in (first, second)}
    failed = []
    driven = []

    async def commit(request):
        if request is first:
            raise ModelSwitchConflict("history changed before activity commit")
        return SimpleNamespace(status="committed")

    async def fail(request, error):
        failed.append((request.thread_id, str(error)))
        return SimpleNamespace(status="failed")

    async def drive(thread_id):
        driven.append(thread_id)

    monkeypatch.setattr(
        "infra.model_switches.advance_pending_switch",
        lambda thread_id: requests[thread_id],
    )
    monkeypatch.setattr(model_pending, "_commit_fitting_switch", commit)
    monkeypatch.setattr(model_pending, "_fail_switch", fail)
    monkeypatch.setattr(model_pending, "_drive_destination", drive)

    outcomes = [
        await model_pending.drive_pending_model_switch(thread_id)
        for thread_id in (first.thread_id, second.thread_id)
    ]

    assert [outcome.status for outcome in outcomes] == ["failed", "committed"]
    assert failed == [("thread-1", "history changed before activity commit")]
    assert driven == ["thread-2"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status_response",
    [
        pytest.param(
            {"status": 200, "body": {"models": [{"id": "Fortissimo", "loaded": False}]}},
            id="unloaded",
        ),
        pytest.param({"status": 503, "body": None}, id="loaded-state-unknown"),
    ],
)
async def test_unavailable_native_omlx_source_skips_warm_capture(
    monkeypatch,
    status_response,
) -> None:
    """An unsafe warm source yields None so the existing target-cold branch runs."""
    import httpx
    from unittest.mock import AsyncMock

    from infra.model_switch_types import SwitchRequest
    from thread import model_checkpoint

    request = SwitchRequest(
        "switch-1", "thread-1", "omlx@ns/Fortissimo", "codex/target",
        1, 2, 0, "user", "user-1", "model_switch", "current",
        "2026-08-31T00:00:00+00:00", 0,
    )
    source_target = SimpleNamespace(
        harness="native",
        provider="omlx",
        transport_model="omlx@ns/Fortissimo",
    )
    context = SimpleNamespace(
        source_target=source_target,
        boundary=object(),
        source_binary=None,
        reasoning_effort=None,
    )
    monkeypatch.setattr(
        model_checkpoint,
        "read_switch_checkpoint_context",
        lambda _request: context,
    )
    monkeypatch.setattr(
        "llm.capabilities.make_provider_params",
        lambda _model: {
            "model": "openai/Fortissimo",
            "api_key": "omlx",
            "api_base": "http://omlx.test/v1",
        },
    )
    capture = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "core.checkpoint_render.capture_warm_checkpoint_source",
        capture,
    )

    calls: list[str] = []

    def handler(http_request: httpx.Request) -> httpx.Response:
        calls.append(str(http_request.url))
        if status_response["body"] is None:
            return httpx.Response(status_response["status"])
        return httpx.Response(
            status_response["status"],
            json=status_response["body"],
        )

    import llm.providers.omlx_lifecycle as lifecycle

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(lifecycle.httpx, "AsyncClient", client)

    result = await model_checkpoint.prepare_source_checkpoint(request, "checkpoint-1")

    assert result is None
    capture.assert_not_awaited()
    assert calls == ["http://omlx.test/v1/models/status"]


def test_target_cold_preserves_unavailable_source_model(monkeypatch) -> None:
    """Cold audit identity remains exact when the old provider is unavailable."""
    from infra.model_switch_types import SwitchRequest
    from thread import model_checkpoint

    request = SwitchRequest(
        "switch-1", "thread-1", "claude-code/source", "codex/target",
        1, 2, 0, "user", "user-1", "model_switch", "current",
        "2026-08-27T00:00:00+00:00", 0,
    )
    context = SimpleNamespace(
        source_target=None,
        # A non-harness destination: this audit-identity test binds no vendor account.
        destination_target=SimpleNamespace(harness="external", catalog_id="codex/target"),
        boundary=object(),
        previous=None,
        projection=object(),
        working_dir="/tmp/project",
        reasoning_effort=None,
        destination_binary="codex",
        destination_mcp_config=None,
    )
    captured = {}

    class _Db:
        def close(self) -> None:
            return None

    def build(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(
        model_checkpoint, "read_switch_checkpoint_context", lambda _request: context,
    )
    monkeypatch.setattr("infra.database.get_db", lambda: _Db())
    monkeypatch.setattr("core.checkpoint_cold_admission.build_checkpoint_admission", build)

    model_checkpoint._target_cold_admission(request, "checkpoint-1")

    assert captured["source_model"] == "claude-code/source"


@pytest.mark.asyncio
async def test_model_switch_target_cold_without_bridge_reaches_denied_tool_command(
    monkeypatch, isolated_db,
) -> None:
    """A bridge-less target-cold checkpoint reaches Claude with no native tools."""
    import asyncio
    import json
    from dataclasses import replace
    from unittest.mock import AsyncMock, Mock

    from core import checkpoint_cc_fork as cc
    from infra.model_switch_types import SwitchRequest
    from native_checkpoint_fixtures import cold_source, native_target, seed_vendor_checkpoint_thread
    from thread import model_checkpoint

    target = replace(
        native_target(131_072),
        catalog_id="claude-code/target", harness="claude-code", transport_model="sonnet",
    )
    seed_vendor_checkpoint_thread([target])
    projection = cold_source().projection
    request = SwitchRequest(
        "switch-1", "thread-1", "native/source", target.catalog_id,
        1, 2, 0, "user", "user-1", "model_switch", "current",
        "2026-09-08T00:00:00+00:00", 0,
    )
    context = SimpleNamespace(
        source_target=None, destination_target=target,
        boundary=projection.sealed_boundary,
        previous=None, projection=projection, working_dir="/work/project",
        reasoning_effort=None, destination_binary="claude", destination_mcp_config=None,
    )
    stdout = (
        b'{"type":"assistant","message":{"content":[{"type":"text","text":"checkpoint"}]}}\n'
        b'{"type":"result","result":"checkpoint","is_error":false,"usage":{}}\n'
    )
    context_stream = asyncio.StreamReader()
    context_stream.feed_data((json.dumps({
        "type": "control_response",
        "response": {
            "request_id": "corsia-checkpoint-context", "subtype": "success",
            "response": {"model": "sonnet", "totalTokens": 10_000, "maxTokens": 200_000},
        },
    }) + "\n").encode())
    context_stream.feed_data(stdout)
    context_stream.feed_eof()
    error_stream = asyncio.StreamReader()
    error_stream.feed_eof()
    process = SimpleNamespace(
        returncode=0, terminate=Mock(), wait=AsyncMock(return_value=0),
        stdin=SimpleNamespace(write=Mock(), drain=AsyncMock(), close=Mock()),
        stdout=context_stream, stderr=error_stream,
    )
    spawn = AsyncMock(return_value=process)

    monkeypatch.setattr(model_checkpoint, "read_switch_checkpoint_context", lambda _request: context)
    monkeypatch.setattr(
        "infra.db_async.run_in_db",
        AsyncMock(side_effect=lambda function, *args, **kwargs: function(*args, **kwargs)),
    )
    monkeypatch.setattr("infra.continuity_store.active_entries", lambda *_args: {})
    monkeypatch.setattr(cc, "_spawn_process", spawn)

    admission = await model_checkpoint.prepare_target_checkpoint(request, "checkpoint-1")
    result = await cc.invoke_fork(admission.provider_request, asyncio.Event(), kind=CHECKPOINT_KIND)
    assert spawn.await_args is not None
    argv = spawn.await_args.args[0]

    assert result.raw_body == "checkpoint"
    assert spawn.await_args.kwargs["cwd"] == "/work/project"
    assert argv[argv.index("--tools") + 1] == ""
    assert "--disable-slash-commands" in argv
    assert argv[argv.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert "--strict-mcp-config" in argv
    assert process.stdin.write.call_count == 2
    assert json.loads(process.stdin.write.call_args.args[0])["type"] == "user"


def test_relevant_native_warm_checkpoint_matches_transport_selection(
    isolated_db, monkeypatch,
) -> None:
    """A selected Native transport and its catalog ID are one model."""
    from infra.database import get_db
    from infra.model_switch_types import SwitchRequest
    from thread import model_checkpoint

    db = get_db()
    db.execute(
        "UPDATE models SET litellm_model='omlx@local/qwen-transport' "
        "WHERE id='oMLX/qwen'"
    )
    db.commit()
    db.close()
    request = SwitchRequest(
        "switch-1", "thread-1", "omlx@local/qwen-transport", "omlx@local/qwen",
        1, 2, 0, "user", "user-1", "model_switch", "current",
        "2026-08-27T00:00:00+00:00", 0,
    )
    record = SimpleNamespace(
        trigger="model_switch",
        mode="warm",
        source_model="oMLX/qwen",
        writer_model="oMLX/qwen",
        started_at="2026-08-27T00:00:01+00:00",
    )
    monkeypatch.setattr(
        "infra.checkpoint_queries.model_switch_for_thread",
        lambda _thread_id, **_kwargs: record,
    )

    assert model_checkpoint.relevant_checkpoint(request) is record
