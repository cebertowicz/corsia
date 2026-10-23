"""A cold Claude Code checkpoint that ends without a body must say why.

Each case runs a real child process through the Claude Code fork adapter. The
"no text" stream is the shape Claude Code 2.1.296 prints when every model reply
holds only thinking: it nudges once, then reports a successful empty result
with exit code 0 and nothing on stderr.
"""

import asyncio
import json
import sys
from dataclasses import replace

import pytest
from core.checkpoint_fork_kind import CHECKPOINT_KIND

from native_checkpoint_fixtures import seed_vendor_checkpoint_thread
from test_checkpoint_external_adapters import _request, _target

_THINKING_ONLY = {"type": "assistant", "message": {
    "id": "reply", "role": "assistant", "stop_reason": None,
    "content": [{"type": "thinking", "thinking": "Considering the history.", "signature": "c2ln"}],
}}
_NO_TEXT_RECORDS = [
    {"type": "system", "subtype": "init"},
    {"type": "user", "message": {"role": "user", "content": "Write a checkpoint."}},
    _THINKING_ONLY,
    {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "Continue."}]}},
    _THINKING_ONLY,
    {"type": "result", "subtype": "success", "is_error": False, "num_turns": 2,
     "result": "", "stop_reason": "end_turn", "terminal_reason": "completed"},
]

_SCRIPTS = {
    "no_text": (
        "import sys\n"
        "sys.stdin.buffer.read()\n"
        f"print({json.dumps(chr(10).join(json.dumps(record) for record in _NO_TEXT_RECORDS))}, flush=True)\n"
    ),
    "silent_exit": "import sys\nsys.stdin.buffer.read()\n",
    "exit_before_input": "pass\n",
}

_EXPECTED = {
    "no_text": (
        "Claude Code checkpoint produced no body: result success with empty text after 2 turns "
        "(stop end_turn); assistant replies had thinking only; exit code 0; input sent (20 bytes)"
    ),
    "silent_exit": (
        "Claude Code checkpoint produced no body: no result record; no stdout records; "
        "exit code 0; input sent (20 bytes)"
    ),
    "exit_before_input": (
        "Claude Code checkpoint produced no body: no result record; no stdout records; "
        "exit code 0; input write failed: ConnectionResetError"
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(_SCRIPTS))
async def test_cold_checkpoint_without_body_names_its_cause(monkeypatch, isolated_db, case):
    seed_vendor_checkpoint_thread([_target("claude-code")])
    from core import fork_cc as cc
    from core.fork_types import ProviderForkRejected

    process = await asyncio.create_subprocess_exec(
        sys.executable, "-u", "-c", _SCRIPTS[case],
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def spawn(*_args, **_kwargs):
        return process

    async def prepare(*_args):
        if case == "exit_before_input":
            await process.wait()  # The CLI ends while CORSIA is still fitting history.
        return b"Write a checkpoint.\n"

    monkeypatch.setattr(cc, "_spawn_process", spawn)
    monkeypatch.setattr("core.fork_cc_cold.prepare_cold_payload", prepare)
    target = replace(_target("claude-code"), transport_model="sonnet", expected_reported_model="sonnet")
    try:
        with pytest.raises(ProviderForkRejected) as raised:
            await asyncio.wait_for(cc.invoke_fork(
                _request(target, None, binary="unused"), asyncio.Event(), kind=CHECKPOINT_KIND,
            ), 10)
        assert str(raised.value) == _EXPECTED[case]
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()
