"""A first oMLX prefill-guard refusal waits for the seat to go quiet and resends (#1219).

The guard prices the whole prompt as new KV before the prefix cache is consulted,
so a call can be refused only because another request on the seat holds memory
at that moment. Learning a window from that refusal shrank every later turn
below its fixed prompt; the call now waits for the seat and is sent once more.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

_BACKEND_DIR = os.path.dirname(os.path.dirname(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from infra import carry_in_capacity  # noqa: E402
from infra.state.context import _current_turn_identity  # noqa: E402
from infra.thread_input_types import TurnIdentity  # noqa: E402
from llm import memory_fit  # noqa: E402

SID = "ns-guard"
MODEL = "Qwen-Local"
GUARD = (
    "litellm.BadRequestError: OpenAIException - oMLX prefill memory guard rejected "
    "this prompt: Prefill would require ~21.38 GB peak (current 19.57 GB + KV+SDPA "
    "1.81 GB) but the prefill limit is 20.75 GB (metal_cap ceiling 22.55 GB - "
    "safety margin 1.80 GB)"
)
OVERFLOW = "maximum context length is 8192 tokens, however you requested 9000 tokens"


@pytest.fixture(autouse=True)
def _clean():
    """Each test runs inside one Turn, so a learned refusal would be recorded."""
    memory_fit.reset_for_tests()
    token = _current_turn_identity.set(TurnIdentity("turn-guard", 1))
    yield
    _current_turn_identity.reset(token)
    memory_fit.reset_for_tests()


def _seat(monkeypatch, readings):
    """The seat answers each ``/api/status`` read with the next reading."""
    seen = []

    async def probe(client, server_id, host, port):
        seen.append(server_id)
        payload = readings[min(len(seen), len(readings)) - 1]
        return None if payload is None else (host, 1, payload)

    async def address(*_a):
        return ("127.0.0.1", 8210)

    monkeypatch.setattr("llm.providers.omlx_public_probe.probe_registered_status", probe)
    monkeypatch.setattr(carry_in_capacity, "run_in_db", address)
    return seen


def _status(active=0, waiting=0):
    return {"active_requests": active, "waiting_requests": waiting, "total_requests": 3}


def _capped() -> bool:
    return memory_fit.memory_fit_window({"litellm_model": f"omlx@{SID}/{MODEL}"}) is not None


def _rediscovery(monkeypatch, window=41_681):
    """Rediscovery reads the seat's window capped by this Turn's memory fit, as the live path does."""
    from llm.context_window import WindowFacts, cap_to_memory_fit

    async def rediscover(model):
        row = {"litellm_model": f"omlx@{SID}/{model.removeprefix('openai/')}"}
        return cap_to_memory_fit(WindowFacts(window, window), row).effective

    monkeypatch.setattr("llm.context_window_discovery.rediscover_context_window", rediscover)


@pytest.mark.asyncio
@pytest.mark.parametrize(("readings", "timeout", "quiet", "reads"), [
    ([_status(active=1), _status(waiting=1), _status()], 5.0, True, 3),
    ([_status()], 5.0, True, 1),
    ([None], 5.0, False, 1),  # unauthenticated or unreachable: resend now, never wait blind
    ([{"status": "ok"}], 5.0, False, 1),  # no activity fields to wait on
    ([_status(active=1)], 0.05, False, 2),  # still busy when the wait runs out
])
async def test_wait_until_quiet(monkeypatch, readings, timeout, quiet, reads):
    seen = _seat(monkeypatch, readings)
    assert await carry_in_capacity.wait_until_quiet(SID, timeout=timeout, interval=0.03) is quiet
    assert len(seen) == reads


@pytest.mark.asyncio
async def test_first_guard_refusal_waits_for_the_seat_and_learns_nothing(monkeypatch):
    from llm.provider_overflow import OverflowDisposition, overflow_disposition

    seen = _seat(monkeypatch, [_status(active=1), _status()])
    monkeypatch.setattr("llm.provider_overflow.GUARD_QUIET_INTERVAL_SECONDS", 0)
    disposition = await overflow_disposition(
        Exception(GUARD), f"openai/{MODEL}", messages=[{"role": "user", "content": "hi"}],
        omlx_server_id=SID,
    )
    assert disposition is OverflowDisposition.RETRY
    assert len(seen) == 2
    assert not _capped()


@pytest.mark.asyncio
async def test_second_guard_refusal_takes_the_overflow_path(monkeypatch):
    from llm.provider_overflow import OverflowDisposition, overflow_disposition

    _seat(monkeypatch, [_status()])
    disposition = await overflow_disposition(
        Exception(GUARD), f"openai/{MODEL}", messages=[{"role": "user", "content": "hi"}],
        already_retried=True, omlx_server_id=SID,
    )
    assert disposition is OverflowDisposition.HANDLED
    assert _capped()


@pytest.mark.asyncio
@pytest.mark.parametrize(("error", "server_id"), [
    (OVERFLOW, SID),  # a real window overflow on a local seat
    (GUARD, None),  # no oMLX seat to wait on
])
async def test_other_overflows_never_wait(monkeypatch, error, server_id):
    from llm.provider_overflow import OverflowDisposition, overflow_disposition

    monkeypatch.setattr(
        "llm.providers.omlx_public_probe.probe_registered_status", lambda *a: pytest.fail("probed"),
    )
    _rediscovery(monkeypatch, window=1)
    disposition = await overflow_disposition(
        Exception(error), f"openai/{MODEL}", messages=[{"role": "user", "content": "hi"}],
        omlx_server_id=server_id,
    )
    assert disposition is OverflowDisposition.HANDLED


@pytest.mark.asyncio
async def test_turn_call_refused_while_the_seat_is_busy_is_resent_and_answers(monkeypatch):
    """The c8 shape: another ~30k call holds memory; the turn's call is refused once.

    Before the fix the refusal was learned first, rediscovery read the window capped
    below the payload, and the call ended as a handled overflow with no resend.
    """
    import llm.base as llm_base

    seen = _seat(monkeypatch, [_status(active=1), _status()])
    monkeypatch.setattr("llm.provider_overflow.GUARD_QUIET_INTERVAL_SECONDS", 0, raising=False)
    _rediscovery(monkeypatch)
    calls = []

    async def acompletion(**_kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise Exception(GUARD)
        message = SimpleNamespace(content="answered", tool_calls=None, reasoning_content=None)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason="stop")], usage=None,
        )

    monkeypatch.setattr(llm_base, "acompletion", acompletion)
    response = await llm_base._completion_with_retries(
        {"model": f"openai/{MODEL}", "messages": [{"role": "user", "content": "word " * 3000}]},
        model=f"openai/{MODEL}", use_streaming=False,
        on_reasoning_chunk=None, on_content_chunk=None, on_tool_call_chunk=None,
        on_response_started=None, retry_transient_errors=True, omlx_server_id=SID,
    )
    assert response.content == "answered"
    assert len(calls) == 2 and len(seen) == 2
    assert not _capped()
