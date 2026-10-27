"""Lifecycle failures keep diagnostic exceptions in logs, not the response."""

import asyncio
import signal
from unittest.mock import Mock

import pytest

from llm.providers import omlx_supervisor as sup


@pytest.mark.parametrize("failure", [FileNotFoundError, PermissionError])
def test_spawn_failure_is_logged_and_has_plain_message(tmp_path, monkeypatch, caplog, failure):
    def fail(*args, **kwargs):
        raise failure("raw spawn problem")

    monkeypatch.setattr(sup.subprocess, "Popen", fail)
    result = sup._spawn_process(["omlx"], tmp_path / "omlx.log")
    assert result == {"ok": False, "message": "Couldn’t start the model server. Try again."}
    assert "raw spawn problem" in caplog.text


@pytest.mark.parametrize("failed_signal", [signal.SIGTERM, signal.SIGKILL])
def test_stop_signal_failure_is_logged_and_has_plain_message(monkeypatch, caplog, failed_signal):
    async def run_direct(fn, *args):
        return fn(*args)

    def send_signal(_pid, sig):
        if sig == failed_signal:
            raise PermissionError("raw signal problem")

    monkeypatch.setattr(sup, "run_in_db", run_direct)
    monkeypatch.setattr(sup, "_resolve_endpoint", lambda _cfg: ("127.0.0.1", 8210))
    monkeypatch.setattr(sup, "_pid_path", lambda _sid: Mock(exists=lambda: True))
    monkeypatch.setattr(sup, "_read_pid", lambda *args, **kwargs: 42)
    monkeypatch.setattr(sup, "_process_alive", lambda _pid: True)
    monkeypatch.setattr(sup, "_process_exited", lambda _pid: False)
    monkeypatch.setattr(sup, "SIGTERM_GRACE_S", 0)
    monkeypatch.setattr(sup.os, "kill", send_signal)
    result = asyncio.run(sup._stop_server({"server_id": "test"}))
    assert result == {"ok": False, "message": "Couldn’t stop the model server. Try again."}
    assert "raw signal problem" in caplog.text
