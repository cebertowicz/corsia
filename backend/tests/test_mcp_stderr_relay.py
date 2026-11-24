"""The MCP stderr relay must mask output without stalling its child."""

import subprocess
import sys
import threading

import pytest

from tools.mcp_stdio_launch import StderrTail, _FORWARD_LINE_BYTES


@pytest.mark.asyncio
async def test_child_stderr_is_masked_across_writes_and_at_eof(capfd):
    secret = "stored-value-for-relay-test"
    token = "sk-" + "a" * 32
    tail = StderrTail("test", [secret])
    source = (
        "import os, time\n"
        f"os.write(2, {secret[:10].encode()!r})\n"
        "time.sleep(0.05)\n"
        f"os.write(2, {secret[10:].encode()!r} + b'\\n')\n"
        f"os.write(2, {token.encode()!r})\n"
    )
    subprocess.run([sys.executable, "-c", source], stderr=tail.file, check=True, timeout=5)
    await tail.close(None, None, None)
    relayed = capfd.readouterr().err
    assert secret not in relayed
    assert token not in relayed
    assert relayed.count("[REDACTED]") == 2


@pytest.mark.asyncio
async def test_long_unterminated_line_flushes_safely_and_drains(capfd, monkeypatch):
    from tools import mcp_stdio_launch

    written = threading.Event()
    write_all = mcp_stdio_launch._write_all

    def observe_write(fd, data):
        result = write_all(fd, data)
        written.set()
        return result

    monkeypatch.setattr(mcp_stdio_launch, "_write_all", observe_write)
    secret = "long-line-secret-for-test"
    tail = StderrTail("test", [secret])
    # Place the secret across the cap: forwarding either fragment would leak it.
    prefix = b"x" * (_FORWARD_LINE_BYTES - 5) + secret.encode()[:5]
    tail.file.buffer.write(prefix)
    tail.file.flush()
    assert written.wait(3), "relay did not flush before EOF"
    relayed = capfd.readouterr().err
    assert "[REDACTED: stderr line over 65536 bytes]" in relayed
    tail.file.buffer.write(secret.encode()[5:] + b"\nafter\n")
    tail.file.flush()
    await tail.close(None, None, None)
    relayed += capfd.readouterr().err
    assert secret not in relayed
    assert secret[:5] not in relayed and secret[5:] not in relayed
    assert "[REDACTED:" in relayed and "after" in relayed


@pytest.mark.asyncio
async def test_mask_failure_drops_output_and_keeps_draining(monkeypatch, capfd):
    from tools import mcp_stdio_launch

    warnings = []
    monkeypatch.setattr(mcp_stdio_launch.logger, "warning", lambda *args: warnings.append(args))
    tail = StderrTail("test", [])
    monkeypatch.setattr(tail, "mask", lambda _text: (_ for _ in ()).throw(RuntimeError("failed")))
    source = "import os\nfor _ in range(256): os.write(2, b'x' * 1024 + b'\\n')\n"
    subprocess.run([sys.executable, "-c", source], stderr=tail.file, check=True, timeout=5)
    await tail.close(None, None, None)
    assert not tail._reader.is_alive()
    assert tail.lines() == ["[REDACTED]"]
    assert tail._dropped_lines == 256 and warnings
    assert capfd.readouterr().err == ""


@pytest.mark.asyncio
async def test_blocked_forwarder_does_not_block_child(monkeypatch):
    from tools import mcp_stdio_launch

    release = threading.Event()
    monkeypatch.setattr(mcp_stdio_launch, "_write_all", lambda fd, _data: (release.wait(5), fd)[1])
    tail = StderrTail("test", [])
    try:
        source = "import os\nfor _ in range(500): os.write(2, b'x' * 1024 + b'\\n')\n"
        subprocess.run([sys.executable, "-c", source], stderr=tail.file, check=True, timeout=5)
    finally:
        release.set()
        await tail.close(None, None, None)
    assert not tail._reader.is_alive()
