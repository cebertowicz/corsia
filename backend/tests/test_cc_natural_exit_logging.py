"""Natural Claude exits retain bounded stderr diagnostics."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_natural_exit_logs_bounded_stderr(caplog):
    from subagents.cc_stream import CCStreamSession

    exits = []

    async def on_exit(code, stderr):
        exits.append((code, stderr))

    text = 'vendor failure ' + 'x' * 600
    session = CCStreamSession(binary='claude', session_id='test', on_exit=on_exit)
    session._proc = SimpleNamespace(stderr=SimpleNamespace(read=AsyncMock(return_value=text.encode())),
                                    returncode=1, wait=AsyncMock(return_value=1))
    await session._finish_reader(None)
    assert exits == [(1, text)]
    assert text[:500] in caplog.text
    assert text not in caplog.text
