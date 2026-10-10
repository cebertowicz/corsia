"""A stale prepared-context rejection names the check that raised it."""

from __future__ import annotations

import pytest
from loguru import logger

from infra import send_dispatch
from infra.send_types import SendRejected
from infra.stale_context import stale_raise_site
from subagents import codex_dispatch
from subagents.cc_runtime_errors import CCPreparedDestinationStale
from subagents.codex_runtime_errors import CodexPreparedContextStale

THREAD = "af204877-aaaa-bbbb-cccc-000000000000"
TURN = "7cd5f4ec-aaaa-bbbb-cccc-000000000000"


def _epoch_check(message: str) -> None:
    raise CCPreparedDestinationStale(message)


@pytest.fixture
def records():
    seen: list[str] = []
    sink = logger.add(lambda m: seen.append(str(m)), level="WARNING")
    yield seen
    logger.remove(sink)


def test_site_comes_from_the_raising_frame_not_the_message():
    with pytest.raises(CCPreparedDestinationStale) as first:
        _epoch_check("one wording")
    with pytest.raises(CCPreparedDestinationStale) as second:
        _epoch_check("another wording")
    assert stale_raise_site(first.value) == stale_raise_site(second.value)
    assert stale_raise_site(first.value).startswith("test_stale_context.py:_epoch_check:")


@pytest.mark.asyncio
async def test_claude_code_rejection_logs_site_thread_and_turn(monkeypatch, records):
    from subagents import cc_runtime

    async def feed_main(*_args, **_kwargs):
        _epoch_check("provider session belongs to an older runtime context epoch")

    monkeypatch.setattr(cc_runtime, "feed_main", feed_main)
    monkeypatch.setattr(send_dispatch, "_validate_project_working_dir", lambda *a, **k: None)
    with pytest.raises(SendRejected) as rejected:
        await send_dispatch.dispatch_cc_main(
            THREAD, {"project_id": None}, "hi", target=object(), turn_id=TURN,
        )
    assert rejected.value.metadata["cause"] == "prepared_context_stale"
    line = "".join(records)
    assert "harness=claude-code" in line
    assert "site=test_stale_context.py:_epoch_check:" in line
    assert "thread=af204877" in line and "turn=7cd5f4ec" in line
    assert "older runtime" not in line


@pytest.mark.asyncio
async def test_codex_rejection_logs_site_thread_and_turn(monkeypatch, records):
    async def invoke(**_kwargs):
        raise CodexPreparedContextStale("admission is no longer exact")

    monkeypatch.setattr(codex_dispatch, "_invoke_codex", invoke)
    with pytest.raises(SendRejected):
        await codex_dispatch._invoke_codex_checked(
            thread_id=THREAD, successor_turn_id=TURN,
        )
    line = "".join(records)
    assert "harness=codex" in line
    assert "site=test_stale_context.py:invoke:" in line
    assert "thread=af204877" in line and "turn=7cd5f4ec" in line


def _wrapper(message: str) -> None:
    try:
        _epoch_check(message)
    except CCPreparedDestinationStale as error:
        raise CodexPreparedContextStale("wrapped wording") from error


def test_wrapped_stale_names_the_inner_check_and_no_message():
    with pytest.raises(CodexPreparedContextStale) as wrapped:
        _wrapper("inner wording")
    site = stale_raise_site(wrapped.value)
    assert site.startswith("test_stale_context.py:_epoch_check:")
    assert " <- test_stale_context.py:_wrapper:" in site
    assert "wording" not in site
