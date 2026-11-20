"""A Claude Code usage-limit refusal during a checkpoint fork installs nothing and records the limit.

The frames are reconstructions around Claude Code's real refusal text (the
strings the terminal limit tests already pin) and the ``rate_limit_event``
shape the live event pump reads. The two result shapes and every position of
the event are covered, since the order Claude Code emits them in is not pinned.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from core import fork_cc_commands
from core.checkpoint_fork_kind import CHECKPOINT_KIND
from core.fork_types import ProviderForkRejected
from test_checkpoint_provider_adapters import _request, _target, vendor_thread  # noqa: F401

pytestmark = pytest.mark.usefixtures("signed_in_defaults")

#: Claude Code's refusal wording (also pinned by the terminal limit tests).
FIVE_HOUR_REFUSAL = "You've hit your 5-hour limit · resets 7am (America/Los_Angeles)"
SESSION_REFUSAL = "You've hit your session limit · resets 1am (America/Los_Angeles)"
REJECTED = {"status": "rejected", "rateLimitType": "five_hour", "resetsAt": 4102444800}
MODEL = "catalog-claude-code"


def _line(record: dict) -> bytes:
    return json.dumps(record).encode() + b"\n"


def _assistant(text: str, *, model: str = "claude-model") -> bytes:
    return _line({"type": "assistant", "message": {
        "id": f"msg-{len(text)}-{model}", "model": model,
        "content": [{"type": "text", "text": text}],
    }})


def _result(text: str, *, is_error: bool) -> bytes:
    return _line({"type": "result", "subtype": "success", "is_error": is_error,
                  "result": text, "num_turns": 1})


def _event(info: dict) -> bytes:
    return _line({"type": "rate_limit_event", "rate_limit_info": info})


def limit_frame(shape: str, event_at: str | None, refusal: str = FIVE_HOUR_REFUSAL,
                *, prefix: bytes = b"") -> bytes:
    """One refusal as Claude Code prints it: optional event before or after the terminal records."""
    terminal = _result(refusal, is_error=True)
    if shape == "synthetic_and_error":
        terminal = _assistant(refusal, model="<synthetic>") + terminal
    event = _event(REJECTED) if event_at else b""
    return prefix + (event + terminal if event_at == "before" else terminal + event)


def use_cc_frame(monkeypatch, stdout: bytes, *, returncode: int = 1, stderr: bytes = b""):
    """Run the real fork adapter over a substituted process that prints ``stdout``."""
    from core import fork_cc as cc
    from core.fork_render import fit_cold_fork_input

    async def prepare(_process, request, _cancel, _on_input, _spawned_at):
        from subagents.cc_stream_frames import build_user_frame

        return build_user_frame(fit_cold_fork_input(request, provider_surface_tokens=25_685)
                                .as_prompt(request.instruction))

    async def spawn(*_args, **_kwargs):
        from test_checkpoint_external_adapters import FakeProcess

        return FakeProcess(stdout, returncode=returncode, stderr=stderr)

    monkeypatch.setattr("core.fork_cc_cold.prepare_cold_payload", prepare)
    monkeypatch.setattr(fork_cc_commands, "cold_command", lambda _request, _kind, **_kwargs: ["claude"])
    monkeypatch.setattr(cc, "_fork_env", lambda *_args: {})
    monkeypatch.setattr(cc, "_spawn_process", spawn)
    return cc


def _held(model: str = MODEL):
    from infra.held_admission import limited_admission

    return limited_admission("thread-1", model)


@pytest.mark.asyncio
@pytest.mark.parametrize("event_at", ["before", "after", None])
@pytest.mark.parametrize("shape", ["synthetic_and_error", "error_only"])
async def test_cc_limit_frame_rejects_the_fold_and_records_the_limit(
    monkeypatch, vendor_thread, shape, event_at,
):
    """Every shape and event position: nothing to install, and the account reads limited."""
    stdout = limit_frame(shape, event_at, prefix=_assistant("Partial narrative before the refusal\n"))
    cc = use_cc_frame(monkeypatch, stdout)
    assert _held() is None

    with pytest.raises(ProviderForkRejected) as raised:
        await cc.invoke_fork(replace(_request(None), target=_target("claude-code")),
                             asyncio.Event(), kind=CHECKPOINT_KIND)

    assert type(raised.value) is ProviderForkRejected
    assert "Partial narrative" not in str(raised.value)
    held = _held()
    assert held is not None and held.connection == "claude-code"


@pytest.mark.asyncio
async def test_cc_session_limit_wording_records_from_the_error_result(monkeypatch, vendor_thread):
    """The older session wording, with no event, still names a usage limit."""
    cc = use_cc_frame(monkeypatch, limit_frame("error_only", None, SESSION_REFUSAL))

    with pytest.raises(ProviderForkRejected):
        await cc.invoke_fork(replace(_request(None), target=_target("claude-code")),
                             asyncio.Event(), kind=CHECKPOINT_KIND)
    assert _held() is not None


@pytest.mark.asyncio
async def test_cc_narrative_naming_a_limit_does_not_trip_the_limit_path(monkeypatch, vendor_thread):
    """Only structured signals record a limit; the model's own words never do."""
    narrative = "The agent noted: You've hit your weekly limit · resets 7am (America/Los_Angeles)\n"
    stdout = _assistant(narrative) + _result(narrative, is_error=False)
    cc = use_cc_frame(monkeypatch, stdout, returncode=0)

    result = await cc.invoke_fork(replace(_request(None), target=_target("claude-code")),
                                  asyncio.Event(), kind=CHECKPOINT_KIND)

    assert result.raw_body == narrative
    assert result.diagnostic is None
    assert _held() is None


@pytest.mark.asyncio
async def test_cc_overage_in_use_is_not_a_refusal(monkeypatch, vendor_thread):
    """A rejected window with overage allowed still serves the call."""
    stdout = (_event({**REJECTED, "overageStatus": "allowed"}) + _assistant("Narrative\n")
              + _result("Narrative\n", is_error=False))
    cc = use_cc_frame(monkeypatch, stdout, returncode=0)

    result = await cc.invoke_fork(replace(_request(None), target=_target("claude-code")),
                                  asyncio.Event(), kind=CHECKPOINT_KIND)

    assert result.raw_body == "Narrative\n"
    assert _held() is None


@pytest.mark.asyncio
async def test_cc_error_result_text_is_never_the_body(monkeypatch, vendor_thread):
    """A non-limit error with no assistant output fails named; its text is not a narrative."""
    cc = use_cc_frame(monkeypatch, _result("API Error: 500 upstream overloaded", is_error=True))

    with pytest.raises(ProviderForkRejected) as raised:
        await cc.invoke_fork(replace(_request(None), target=_target("claude-code")),
                             asyncio.Event(), kind=CHECKPOINT_KIND)
    assert "produced no body" in str(raised.value)
    assert _held() is None


@pytest.mark.asyncio
async def test_cc_limit_whose_verdict_write_fails_fails_visibly(monkeypatch, vendor_thread):
    """A refusal that cannot be recorded says so in the fold's failure, and nothing reads limited."""
    import sqlite3

    from connections import harness_usage

    def broken_store(**_kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(harness_usage, "record_usage", broken_store)
    cc = use_cc_frame(monkeypatch, limit_frame("error_only", "before"))

    with pytest.raises(ProviderForkRejected) as raised:
        await cc.invoke_fork(replace(_request(None), target=_target("claude-code")),
                             asyncio.Event(), kind=CHECKPOINT_KIND)
    assert "usage-limit verdict not recorded" in str(raised.value)
    assert _held() is None
