"""Naming seam for the connected send/dispatch thread-row contract."""

from __future__ import annotations

import ast
import dataclasses
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from api import delegation
from core import context_preview, thread_identity
from infra import (
    destination_payloads,
    input_render,
    preflight_attempt,
    preflight_render,
    send_dispatch,
    send_effects,
    send_preflight,
    send_service,
    thread_execution,
)
from infra.input_render import PreparedBatch
from infra.preflight_attempt import DestinationAttempt
from subagents import cc_spawn, codex_dispatch, harness_checkpoint_context, harness_checkpoint_resume
from thread import model_projection
from ws import native_inbox_batch, turn, turn_helpers
from ws.turn_helpers import NativeTurnRequest

_MODULES = (
    delegation,
    context_preview,
    thread_identity,
    send_preflight,
    preflight_attempt,
    preflight_render,
    destination_payloads,
    input_render,
    send_service,
    thread_execution,
    send_dispatch,
    send_effects,
    cc_spawn,
    codex_dispatch,
    harness_checkpoint_context,
    harness_checkpoint_resume,
    model_projection,
    native_inbox_batch,
    turn,
    turn_helpers,
)


def _module_functions(module):
    for name, obj in vars(module).items():
        if inspect.isfunction(obj) and obj.__module__ == module.__name__:
            yield f"{module.__name__}.{name}", obj


def test_destination_attempt_carries_a_thread_row_field() -> None:
    names = [field.name for field in dataclasses.fields(DestinationAttempt)]
    assert "thread_row" in names, names
    assert "session" not in names, names


def test_prepared_batch_carries_a_thread_row_field() -> None:
    names = [field.name for field in dataclasses.fields(PreparedBatch)]
    assert "thread_row" in names, names
    assert "session" not in names, names


@pytest.mark.parametrize(
    "record_type",
    (
        send_dispatch.DispatchContext,
        harness_checkpoint_context.ExternalCheckpointContext,
        NativeTurnRequest,
    ),
)
def test_connected_row_records_use_thread_row(record_type) -> None:
    names = (
        [field.name for field in dataclasses.fields(record_type)]
        if dataclasses.is_dataclass(record_type)
        else list(record_type._fields)
    )
    assert "thread_row" in names, names
    assert "session" not in names, names


def test_no_connected_send_function_takes_a_session_parameter() -> None:
    offenders = [
        name for module in _MODULES for name, fn in _module_functions(module)
        if "session" in inspect.signature(fn).parameters
    ]
    assert offenders == [], offenders


def test_each_preflight_chain_module_exposes_the_thread_row_contract() -> None:
    # preflight_attempt carries the row as the DestinationAttempt field (asserted above).
    for module in (send_preflight, preflight_render, destination_payloads, input_render):
        carriers = [
            name for name, fn in _module_functions(module)
            if "thread_row" in inspect.signature(fn).parameters
        ]
        assert carriers, module.__name__


@pytest.mark.asyncio
async def test_external_checkpoint_renderer_uses_thread_row_keyword(
    monkeypatch,
) -> None:
    captured = {}
    prepared = object()

    async def render(**kwargs):
        captured.update(kwargs)
        return prepared

    monkeypatch.setattr(preflight_render, "render_preflight_selection", render)
    context = SimpleNamespace(
        thread_row={"id": "thread-1"},
        target=SimpleNamespace(harness="codex", catalog_id="codex/test"),
    )
    selection = SimpleNamespace(
        tail=SimpleNamespace(thread_id="thread-1", resume_presence_line=None),
    )

    result = await harness_checkpoint_resume.render_external_selection(
        context,
        identity=object(),
        selection=selection,
        input_identity="input-1",
    )

    assert result is prepared
    assert captured["thread_row"] is context.thread_row
    assert "session" not in captured


_DIRECT_ROW_CALLS = {
    "send_thread_input": 1,
    "execute_claimed_batch": 1,
    "dispatch_cc_main": 1,
    "dispatch_native_event": 1,
    "dispatch_native_delegation": 1,
    "dispatch_native_ws": 1,
    "dispatch_codex": 1,
    "run_batch_accepted_effects": 1,
    "build_next_turn_context_preview": 1,
    "thread_agent_identity": 0,
}


def test_production_row_callers_do_not_forward_session_named_values() -> None:
    offenders = []
    backend = Path(__file__).parents[1]
    stale_names = {"session", "sess", "selected_session", "stale_session"}

    for path in backend.rglob("*.py"):
        if "tests" in path.parts:
            continue
        source = path.read_text()
        tree = ast.parse(source)
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            name = (
                call.func.id
                if isinstance(call.func, ast.Name)
                else call.func.attr
                if isinstance(call.func, ast.Attribute)
                else None
            )
            if name not in _DIRECT_ROW_CALLS:
                continue
            session_keywords = [kw for kw in call.keywords if kw.arg == "session"]
            if session_keywords:
                offenders.append(f"{path.relative_to(backend)}:{call.lineno}:session=")
                continue
            thread_keyword = next(
                (kw.value for kw in call.keywords if kw.arg == "thread_row"), None
            )
            index = _DIRECT_ROW_CALLS[name]
            value = thread_keyword or (call.args[index] if len(call.args) > index else None)
            if value is None:
                continue
            expression = ast.get_source_segment(source, value) or ""
            root = expression.split(".", 1)[0]
            if root in stale_names or ".session" in expression:
                offenders.append(
                    f"{path.relative_to(backend)}:{call.lineno}:{name}({expression})"
                )

    assert offenders == [], offenders
