"""Every warm-source refusal names one log-only reason instead of a silent None."""

from __future__ import annotations

from contextlib import contextmanager
import threading
from types import SimpleNamespace

import pytest
from core.fork_codex_root import RegisteredRollout
from loguru import logger

from test_checkpoint_unavailable_release import blocked_input  # noqa: F401


@contextmanager
def _info_lines():
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(m.rstrip("\n")), format="{message}", level="INFO")
    try:
        yield lines
    finally:
        logger.remove(sink)


def _refused(thread_id: str, harness: str, reason: str) -> str:
    return f"fork warm capture refused for {thread_id} ({harness}): {reason}"


@pytest.mark.asyncio
async def test_native_capture_without_receipt_names_its_reason() -> None:
    from core.checkpoint_native_admission import capture_native_checkpoint_source

    state = SimpleNamespace(
        checkpoint_source_messages=[],
        last_measured_call_usage=None,
        last_measured_rendered_input_tokens=None,
    )
    with _info_lines() as lines:
        source = await capture_native_checkpoint_source(
            "thread-n", state, SimpleNamespace(), "high",
        )

    assert source is None
    assert lines == [_refused("thread-n", "native", "no_correlated_receipt")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("harness", "boundary", "warm_boundary", "reason"),
    (
        ("codex", object(), None, "target_not_claude_code"),
        ("claude-code", None, None, "no_boundary"),
        ("claude-code", object(), object(), "no_warm_boundary"),
    ),
)
async def test_claude_initial_capture_names_its_reason(
    harness, boundary, warm_boundary, reason,
) -> None:
    from core.fork_cc_boundary import capture_boundary_warm_source

    context = SimpleNamespace(
        target=SimpleNamespace(harness=harness),
        boundary=boundary,
        warm_boundary=warm_boundary,
    )
    with _info_lines() as lines:
        source = await capture_boundary_warm_source(context, object(), "thread-c")

    assert source is None
    assert lines == [_refused("thread-c", "claude-code", reason)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("harness", "binary", "reason"),
    (
        ("native", "/bin/x", "native_source_unsupported"),
        ("claude-code", None, "no_binary"),
        ("codex", None, "no_binary"),
    ),
)
async def test_switch_capture_names_its_reason(harness, binary, reason) -> None:
    from core.fork_render import capture_warm_fork_source

    with _info_lines() as lines:
        source = await capture_warm_fork_source(
            "thread-s", object(),
            target=SimpleNamespace(harness=harness),
            binary=binary,
            reasoning_effort=None,
        )

    assert source is None
    assert lines == [_refused("thread-s", harness, reason)]


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ("claude-code", "codex"))
async def test_switch_capture_renders_durable_boundary_off_loop_and_preserves_result(
    monkeypatch, harness: str,
) -> None:
    """Both harness paths retain their source/result contract off the event loop."""
    import core.fork_render as fork_render

    loop_thread = threading.get_ident()
    render_threads: list[int] = []
    durable_boundary = object()
    expected_source = object()
    observed: dict[str, tuple[object, ...]] = {}

    def render_boundary(thread_id, boundary, rendered_harness):
        observed["durable_args"] = (thread_id, boundary, rendered_harness)
        render_threads.append(threading.get_ident())
        return durable_boundary

    monkeypatch.setattr(fork_render, "_durable_warm_boundary", render_boundary)
    target = SimpleNamespace(harness=harness)
    boundary = object()
    if harness == "claude-code":
        import core.fork_cc_boundary as cc_boundary

        async def capture_claude(thread_id, captured_boundary, warm_boundary, **kwargs):
            observed["capture_args"] = (thread_id, captured_boundary, warm_boundary, kwargs)
            return expected_source

        monkeypatch.setattr(cc_boundary, "capture_registered_boundary_warm_source", capture_claude)
        source = await fork_render.capture_warm_fork_source(
            "thread-off-loop", boundary, target=target, binary="/bin/claude", reasoning_effort=None,
        )
    else:
        import core.fork_codex as codex

        def prefix(thread_id, warm_boundary):
            observed["prefix_args"] = (thread_id, warm_boundary)
            return 123

        async def capture_codex(thread_id, captured_boundary, **kwargs):
            observed["capture_args"] = (thread_id, captured_boundary, kwargs)
            return expected_source

        monkeypatch.setattr(codex, "warm_tool_boundary_prefix_bytes", prefix)
        monkeypatch.setattr(codex, "capture_warm_source", capture_codex)
        source = await fork_render.capture_warm_fork_source(
            "thread-off-loop", boundary, target=target, binary="/bin/codex", reasoning_effort=None,
        )

    assert source is expected_source
    assert observed["durable_args"] == ("thread-off-loop", boundary, harness)
    assert render_threads and render_threads != [loop_thread]
    if harness == "claude-code":
        assert observed["capture_args"][:3] == ("thread-off-loop", boundary, durable_boundary)
    else:
        assert observed["prefix_args"] == ("thread-off-loop", durable_boundary)
        assert observed["capture_args"] == (
            "thread-off-loop", boundary,
            {"target": target, "binary": "/bin/codex", "prefix_bytes": 123},
        )


@pytest.mark.asyncio
async def test_registered_capture_without_identity_names_its_reason(monkeypatch) -> None:
    import core.fork_cc as fork
    from core.fork_cc_boundary import capture_registered_boundary_warm_source

    async def no_live_identity(_thread_id):
        return None

    monkeypatch.setattr(fork, "_live_identity", no_live_identity)
    monkeypatch.setattr(fork, "_registered_identity", lambda _thread_id: None)
    with _info_lines() as lines:
        source = await capture_registered_boundary_warm_source(
            "thread-i", object(), object(), target=object(), binary="/bin/claude",
        )

    assert source is None
    assert lines == [_refused("thread-i", "claude-code", "no_identity")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("harness", "boundary_thread", "registered", "reason"),
    (
        ("claude-code", "thread-x", None, "target_not_codex"),
        ("codex", "thread-other", None, "boundary_thread_mismatch"),
        ("codex", "thread-x", None, "rollout_unregistered"),
        ("codex", "thread-x", RegisteredRollout("provider-1", "/elsewhere/r.jsonl", "", None), "rollout_outside_sessions"),
    ),
)
async def test_codex_capture_names_one_reason(
    monkeypatch, harness, boundary_thread, registered, reason,
) -> None:
    import core.fork_codex as fork
    from history.types import HistoryBoundary

    monkeypatch.setattr(fork, "_registered_rollout", lambda _thread_id: registered)
    with _info_lines() as lines:
        source = await fork.capture_warm_source(
            "thread-x", HistoryBoundary(boundary_thread, 1, 9, "row-9"),
            target=SimpleNamespace(harness=harness), binary="/bin/codex",
        )

    assert source is None
    assert lines == [_refused("thread-x", "codex", reason)]


@pytest.mark.asyncio
async def test_codex_tool_boundary_refusal_names_the_rollout_check(monkeypatch) -> None:
    """The rollout matcher's own reason reaches the log, once, in place of
    the old catch-all ``prefix_unproved``."""
    import core.fork_codex as fork

    monkeypatch.setattr(fork, "_live_refusal", lambda *_: None)
    monkeypatch.setattr(
        fork, "warm_tool_boundary_prefix_bytes", lambda *_: "receipt_missing:user",
    )
    context = SimpleNamespace(
        target=SimpleNamespace(harness="codex"), boundary=object(), warm_boundary=object(),
    )
    with _info_lines() as lines:
        source = await fork._capture_tool_boundary_warm_source(context, object(), "thread-r")

    assert source is None
    assert lines == [_refused("thread-r", "codex", "receipt_missing:user")]


@pytest.mark.asyncio
async def test_switch_target_choice_is_not_a_refusal() -> None:
    from thread.model_checkpoint import prepare_source_checkpoint

    request = SimpleNamespace(thread_id="thread-t", checkpoint_choice="target")
    with _info_lines() as lines:
        admission = await prepare_source_checkpoint(request, "checkpoint-t")

    assert admission is None
    assert lines == []


def _choice(choice: str) -> str:
    return f"model switch checkpoint choice for held-thread: {choice}"


def _choice_lines(lines: list[str]) -> list[str]:
    return [line for line in lines if line.startswith("model switch checkpoint choice")]


@pytest.mark.parametrize(("requested", "logged"), (("current", "source"), ("target", "target")))
def test_switch_logs_its_requested_choice_once(blocked_input, requested, logged) -> None:
    from infra.model_switches import request_switch

    with _info_lines() as lines:
        for _ in range(2):  # A duplicate request is the same switch, not a new choice.
            request_switch(
                "held-thread", "native/destination", operation_id="switch-once",
                requested_at="t4", checkpoint_choice=requested, db=blocked_input,
            )

    assert _choice_lines(lines) == [_choice(logged)]


@pytest.mark.parametrize(("requested", "logged"), (
    ("current", ["source", "forced"]),
    ("target", ["target"]),
))
def test_forced_transfer_logs_only_an_override(blocked_input, requested, logged) -> None:
    from infra.model_switches import request_switch
    from test_unwritten_switch_suffix import _finish, _settle_refusal, _suffix

    db = blocked_input
    with _info_lines() as lines:
        _suffix(db)
        request_switch(
            "held-thread", "native/destination", operation_id="switch-held",
            requested_at="t4", checkpoint_choice=requested, db=db,
        )
        _settle_refusal(db)
        _finish(db)  # The terminal owner transfers the unwritten suffix.

    assert db.execute(
        "SELECT target_execution_epoch FROM messages WHERE id='healthy-suffix'",
    ).fetchone()[0] == 1
    assert _choice_lines(lines) == [_choice(choice) for choice in logged]
