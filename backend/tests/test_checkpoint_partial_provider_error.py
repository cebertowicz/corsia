"""A fold cut short by a provider error keeps the last complete narrative and carries only whole lines after it."""

from dataclasses import replace

import pytest

from core.checkpoint_render import (
    INCOMPLETE_UPDATE,
    PARTIAL_UPDATE_END,
    PRIOR_PROVIDER_ERROR_WARNING,
    PROVIDER_ERROR_WARNING,
    render_installed_checkpoint,
    render_prior_checkpoint,
)
from core.fork_types import ProviderForkResult, partial_provider_error

PRIOR = "Prior decisions and unfinished work."
ERROR = "API Error: upstream overloaded"


async def _fold(monkeypatch, thread: str, checkpoint_id: str, body: str, previous: str | None,
                through: int = 3):
    """Run one fold through the real runtime and capture; only the provider call is substituted."""
    from core import checkpoint_runtime as runtime
    from test_checkpoint_runtime import _checkpoint, _provider

    async def invoke(_request, _cancel):
        return ProviderForkResult(body, {"calls": [{"input_tokens": 123}]}, partial_provider_error(ERROR))

    async def notify(_record):
        return None

    monkeypatch.setattr(runtime, "_invoke", invoke)
    monkeypatch.setattr(runtime, "_notify_completion", notify)
    request = replace(_checkpoint(thread, checkpoint_id), previous_generation_id=previous,
                      through_seq=through, through_message_id=f"{thread}-m{through}")
    await runtime.request_checkpoint(request, _provider(thread, checkpoint_id))
    return await runtime.await_checkpoint(checkpoint_id)


def _install_prior(thread: str, through: int = 2) -> None:
    from infra import checkpoints as store
    from test_checkpoint_runtime import _checkpoint, _seed

    _seed(thread)
    store.create_running(replace(_checkpoint(thread, "prior"), through_seq=through,
                                 through_message_id=f"{thread}-m{through}"))
    store.capture("prior", raw_result=PRIOR, usage_json=None, captured_at="t2")
    store.install("prior", installed_at="t3")


@pytest.mark.asyncio
async def test_partial_is_carried_after_the_last_complete_narrative_with_its_end_marked(
    isolated_db, monkeypatch,
):
    _install_prior("partial")

    record = await _fold(monkeypatch, "partial", "update",
                         "Decided the schema.\nStarted the migr", "prior")

    assert record.status == "installed"
    assert record.raw_result == (
        PRIOR + "\n\n" + INCOMPLETE_UPDATE + "Decided the schema.\n" + PARTIAL_UPDATE_END
    )  # the cut word is gone, the end is marked
    assert ERROR not in record.raw_result  # the provider's text never becomes narrative
    assert render_installed_checkpoint(record).startswith(PROVIDER_ERROR_WARNING)
    assert render_prior_checkpoint(record).startswith(PRIOR_PROVIDER_ERROR_WARNING)


@pytest.mark.asyncio
async def test_a_second_partial_replaces_the_first_instead_of_stacking(isolated_db, monkeypatch):
    from infra import checkpoint_queries

    _install_prior("again", through=1)
    await _fold(monkeypatch, "again", "first", "First partial line.\n", "prior", through=2)

    record = await _fold(monkeypatch, "again", "second", "Second partial line.\n", "first")

    assert record.status == "installed"
    assert record.raw_result == (
        PRIOR + "\n\n" + INCOMPLETE_UPDATE + "Second partial line.\n" + PARTIAL_UPDATE_END
    )
    assert checkpoint_queries.current_installed("again").id == "second"


@pytest.mark.asyncio
async def test_a_partial_echoing_the_marker_is_still_replaced_whole(isolated_db, monkeypatch):
    # The fold model sees the marker in its own input and can copy it into its output.
    _install_prior("echo", through=1)
    await _fold(monkeypatch, "echo", "first", "Copied " + INCOMPLETE_UPDATE + "First line.\n", "prior",
                through=2)

    record = await _fold(monkeypatch, "echo", "second", "Second partial line.\n", "first")

    assert record.raw_result == (
        PRIOR + "\n\n" + INCOMPLETE_UPDATE + "Second partial line.\n" + PARTIAL_UPDATE_END
    )


@pytest.mark.asyncio
async def test_a_partial_ending_on_an_echoed_marker_line_is_still_replaced_whole(isolated_db, monkeypatch):
    # The likeliest echo: the fold model copies the marker line, then the error cuts the next line,
    # so the copy is the partial's last complete line and has lost its line break.
    _install_prior("echo-end", through=1)
    await _fold(monkeypatch, "echo-end", "first",
                "First line.\n" + INCOMPLETE_UPDATE + "Cut sec", "prior", through=2)

    record = await _fold(monkeypatch, "echo-end", "second", "Second partial line.\n", "first")

    assert record.raw_result == (
        PRIOR + "\n\n" + INCOMPLETE_UPDATE + "Second partial line.\n" + PARTIAL_UPDATE_END
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["Started the migr", "\n"])
async def test_no_complete_line_fails_the_attempt_and_keeps_the_prior_checkpoint(
    isolated_db, monkeypatch, body,
):
    from infra import checkpoint_queries

    _install_prior("empty")

    record = await _fold(monkeypatch, "empty", "update", body, "prior")

    assert record.status == "failed"
    assert record.installed_at is None
    assert checkpoint_queries.current_installed("empty").raw_result == PRIOR


@pytest.mark.asyncio
async def test_first_checkpoint_with_no_previous_narrative_carries_only_the_marked_partial(
    isolated_db, monkeypatch,
):
    from test_checkpoint_runtime import _seed

    _seed("fresh")

    record = await _fold(monkeypatch, "fresh", "first", "Only line so far.\nhalf", None)

    assert record.status == "installed"
    assert record.raw_result == INCOMPLETE_UPDATE + "Only line so far.\n" + PARTIAL_UPDATE_END
    assert render_installed_checkpoint(record).startswith(PROVIDER_ERROR_WARNING)
