"""A model switch survives a catalog refresh of the destination it was asked for.

The switch request schedules its destination harness's catalog check
(``prepare_selection``), so that refresh can commit while the switch prepares
or before it commits. A refreshed window, label or capability is the same
destination; another catalog row, harness, provider, backend, transport
selector or resolved model is not; an unavailable destination fails with its
reason. Only the CLI processes (version probe and model listing) are faked.
"""

from __future__ import annotations

import copy
import dataclasses
from unittest.mock import AsyncMock

import pytest
from loguru import logger

from shared_send_fixtures import _insert_native_model, _insert_session

pytestmark = pytest.mark.usefixtures("signed_in_defaults")  # a harness turn needs a signed-in Default: no ambient login

THREAD = "switch-identity"
USER = "u-test"
DESTINATION = "claude-code/opus"
HARNESS_VERSION = "2.1.300"
IDENTITY_CHANGES = [
    ("catalog_id", "claude-code/opus-next"),
    ("harness", "codex"),
    ("provider", "codex"),
    ("backend", "codex"),
    ("transport_model", "opus-next"),
    ("expected_reported_model", "claude-opus-5-0"),
]


def _listing(*, opus_window: int = 1_000_000, include_opus: bool = True) -> dict:
    def entry(selector: str, resolved: str, label: str, window: int) -> dict:
        return {
            "value": selector, "resolvedModel": resolved, "displayName": label,
            "contextUsage": {"rawMaxTokens": window, "maxTokens": window},
        }

    models = [
        entry("sonnet", "claude-sonnet-4-8", "Sonnet 4.8", 200_000),
        entry("tiny", "tiny", "Tiny", 128),
    ]
    if include_opus:
        models.insert(0, entry("opus", "claude-opus-4-8", "Opus 4.8", opus_window))
    return {"models": models}


@pytest.fixture
def switch(isolated_db, monkeypatch):
    """A native thread with a pending switch to Claude Code Opus and one queued input."""
    from connections import harness_models, harness_refresh
    from connections.harness_model_rows import CLAUDE_SOURCE, normalize_claude_models
    from infra.input_admission import accept_thread_input
    from infra.model_switches import request_switch

    listing = {"payload": _listing()}

    async def list_models(_binary, context=None):
        from connections.account_discovery_types import StagedDiscovery

        return StagedDiscovery(payload=copy.deepcopy(listing["payload"]))

    monkeypatch.setattr(harness_refresh, "_harness_version", AsyncMock(return_value=HARNESS_VERSION))
    monkeypatch.setitem(
        harness_models._ADAPTERS, "claude-code", (list_models, normalize_claude_models, CLAUDE_SOURCE),
    )
    monkeypatch.setattr(harness_refresh, "_notify_models_changed", lambda: None)
    _insert_native_model("native/source")
    _insert_session(THREAD, model="native/source", spawned_by=None)
    request_switch(THREAD, DESTINATION, operation_id="switch-op", requested_at="t1")
    accept_thread_input(THREAD, role="user", content="Continue", intent="apply_now", message_id="queued")
    return listing


@pytest.fixture
def warnings():
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="WARNING", format="{message}")
    yield lines
    logger.remove(sink)


async def _catalog_refresh(listing: dict, payload: dict) -> None:
    """Run, to completion, the catalog check a switch request schedules."""
    from connections import harness_models, harness_refresh

    listing["payload"] = payload
    await harness_refresh.prepare_selection(DESTINATION)
    check = harness_models._CHECK_TASKS.get("claude-code")
    assert check is not None, "the switch request schedules a background catalog check"
    await check


def _after_destination_resolved(monkeypatch, action) -> list[bool]:
    """Run ``action`` once preparation has resolved its destination, before it re-reads."""
    from infra import db_async
    from thread import model_source

    real = db_async.run_in_db
    fired: list[bool] = []

    async def run_in_db(fn, *args, **kwargs):
        result = await real(fn, *args, **kwargs)
        if fn is model_source._read_model_switch_start and not fired:
            fired.append(True)
            await action()
        return result

    monkeypatch.setattr(db_async, "run_in_db", run_in_db)
    return fired


def _change_identity(monkeypatch, field: str, value: str) -> None:
    from thread import model_source

    real = model_source._execution_target

    def resolve(model, *, db=None):
        target = real(model, db=db)
        return dataclasses.replace(target, **{field: value}) if model == DESTINATION else target

    monkeypatch.setattr(model_source, "_execution_target", resolve)


async def _render(*, thread_id, current_model, expected_selected_model, target, replay, fit_input):
    """Render a small replay for whatever destination the preparation holds."""
    from core.context_payload import PreparedContextPayload, RenderedDestinationPayload
    from core.destination_preflight import attach_destination_fit_ledger

    sections = {"fixed": 0, "checkpoint": 0, "tail": 0, "current_input": 100, "tools": 0, "provider_wrapper": 0}
    prepared = PreparedContextPayload(
        mode="exact_replay" if replay.mode == "exact" else replay.mode,
        target_catalog_id=target.catalog_id,
        model_context_window=target.context_window,
        history_epoch=replay.history_epoch,
        sealed_boundary=replay.sealed_boundary,
        replay=replay,
        rendered=RenderedDestinationPayload(
            payload={"rendered": thread_id}, input_tokens=100,
            accounting_payload={"rendered": thread_id}, fit_section_tokens=sections,
            fit_input_identity=fit_input.identity, fit_current_input=fit_input.content,
            fit_components={"payload": thread_id},
        ),
    )
    return attach_destination_fit_ledger(
        prepared, harness=target.harness, proven_target_window_tokens=target.context_window,
    )


async def _prepare():
    from thread.model_prepare import prepare_model_switch

    return await prepare_model_switch(THREAD, DESTINATION, user_id=USER, render_destination=_render)


async def _commit(preparation):
    from thread.model_commit import commit_model_switch

    return await commit_model_switch(preparation, user_id=USER)


def _resolved():
    from llm.resolution import resolve_execution_target

    return resolve_execution_target(DESTINATION)


def _thread_model() -> str:
    from infra.database import get_db

    db = get_db()
    try:
        return db.execute("SELECT model FROM threads WHERE id=?", (THREAD,)).fetchone()[0]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_refresh_during_preparation_keeps_the_destination(switch, monkeypatch) -> None:
    """The scheduled refresh rewrites window, label and vision mid-preparation."""
    before = _resolved()
    fired = _after_destination_resolved(
        monkeypatch, lambda: _catalog_refresh(switch, _listing(opus_window=950_000)),
    )
    preparation = await _prepare()
    assert fired, "the refresh did not land inside the preparation"
    after = _resolved()
    assert (before.context_window, after.context_window) == (1_000_000, 950_000)
    assert (before.display_name, after.display_name) == (DESTINATION, "Opus 4.8")
    # Fit is proved against the window the destination has now.
    assert preparation.destination_target == after
    assert preparation.destination_fit_ledger.proven_target_window_tokens == 950_000
    await _commit(preparation)
    assert _thread_model() == DESTINATION


@pytest.mark.asyncio
async def test_refresh_before_commit_keeps_the_destination(switch) -> None:
    """A label and capability refresh between preparation and commit still commits."""
    preparation = await _prepare()
    await _catalog_refresh(switch, _listing())
    current = _resolved()
    assert current != preparation.destination_target
    assert current.context_window == preparation.destination_target.context_window
    await _commit(preparation)
    assert _thread_model() == DESTINATION


@pytest.mark.asyncio
async def test_window_shrink_before_commit_prepares_again(switch) -> None:
    """A smaller window voids the prepared fit proof, so the switch re-prepares."""
    from thread.model_state import ModelSwitchSequencingRequired

    preparation = await _prepare()
    await _catalog_refresh(switch, _listing(opus_window=950_000))
    with pytest.raises(ModelSwitchSequencingRequired, match="window shrank before commit"):
        await _commit(preparation)
    assert _thread_model() == "native/source"
    again = await _prepare()
    assert again.destination_target.context_window == 950_000
    await _commit(again)
    assert _thread_model() == DESTINATION


@pytest.mark.asyncio
@pytest.mark.parametrize(("field", "value"), IDENTITY_CHANGES)
async def test_identity_change_during_preparation_conflicts(
    switch, monkeypatch, warnings, field, value,
) -> None:
    from thread.model_state import ModelSwitchConflict

    async def change() -> None:
        _change_identity(monkeypatch, field, value)

    _after_destination_resolved(monkeypatch, change)
    with pytest.raises(ModelSwitchConflict, match="destination changed during preparation"):
        await _prepare()
    assert any(f"{field}=" in line for line in warnings), warnings
    assert _thread_model() == "native/source"


@pytest.mark.asyncio
@pytest.mark.parametrize(("field", "value"), IDENTITY_CHANGES)
async def test_identity_change_before_commit_conflicts(
    switch, monkeypatch, warnings, field, value,
) -> None:
    from thread.model_state import ModelSwitchConflict

    preparation = await _prepare()
    _change_identity(monkeypatch, field, value)
    with pytest.raises(ModelSwitchConflict, match="destination changed before commit"):
        await _commit(preparation)
    assert any(f"{field}=" in line for line in warnings), warnings
    assert _thread_model() == "native/source"


@pytest.mark.asyncio
async def test_unlisted_destination_during_preparation_fails_with_reason(switch, monkeypatch) -> None:
    """A refresh that no longer lists the destination retires it; the switch says why."""
    from thread.model_state import ModelSwitchConflict, ModelSwitchPreparationError

    _after_destination_resolved(
        monkeypatch, lambda: _catalog_refresh(switch, _listing(include_opus=False)),
    )
    with pytest.raises(ModelSwitchPreparationError) as raised:
        await _prepare()
    assert not isinstance(raised.value, ModelSwitchConflict)
    assert str(raised.value) == "model switch destination is unavailable: not_returned_by_discovery"


@pytest.mark.asyncio
async def test_unlisted_destination_before_commit_fails_with_reason(switch) -> None:
    from thread.model_state import ModelSwitchConflict

    preparation = await _prepare()
    await _catalog_refresh(switch, _listing(include_opus=False))
    with pytest.raises(
        ModelSwitchConflict, match="destination is unavailable: not_returned_by_discovery",
    ):
        await _commit(preparation)
    assert _thread_model() == "native/source"
