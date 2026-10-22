"""The first warm checkpoint request, with its output reserve, fits before Claude inference."""

import asyncio
import json
from dataclasses import replace

import pytest

from native_checkpoint_fixtures import seed_vendor_checkpoint_thread
from test_checkpoint_external_adapters import FakeProcess, _request, _target


@pytest.mark.asyncio
@pytest.mark.parametrize("loaded, truncated", [(100, False), (90_000, True), (120_000, True)])
async def test_warm_instruction_waits_for_loaded_capacity(loaded, truncated, isolated_db):
    seed_vendor_checkpoint_thread([_target("claude-code")])
    from core.fork_cc_io import communicate_fork
    from core.fork_types import ProviderForkRejected

    writes = []

    class Process(FakeProcess):
        def write(self, data):
            writes.append(data)
            frame = json.loads(data)
            if frame["type"] == "control_request":
                self.stdout.feed_data(json.dumps({"type": "control_response", "response": {
                    "subtype": "success", "request_id": frame["request_id"], "response": {
                        "totalTokens": loaded, "maxTokens": 100_000, "model": "reported-claude-code",
                    },
                }}).encode() + b"\n")

    process = Process(b"", stderr=b"")
    target = replace(_target("claude-code"), context_window=100_000)
    request = _request(target, object(), binary="unused")
    if truncated:
        with pytest.raises(ProviderForkRejected, match="initial.*input"):
            await asyncio.wait_for(communicate_fork(process, request, asyncio.Event(), spawned_at=0.0), 2)
    else:
        output = await asyncio.wait_for(communicate_fork(process, request, asyncio.Event(), spawned_at=0.0), 2)
        assert not output.truncated
    assert json.loads(writes[0])["request"] == {"subtype": "get_context_usage"}
    assert process.returncode is not None
    if truncated:
        assert len(writes) == 1  # No checkpoint input, even if the loaded prefix is already oversized.
    else:
        assert len(writes) == 2
        assert json.loads(writes[1])["message"]["content"] == request.instruction
