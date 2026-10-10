
from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))



@pytest.fixture
def isolated_runtime(tmp_path, monkeypatch):
    from llm.providers import omlx_supervisor as sup
    from infra import config as cfg
    monkeypatch.setattr(cfg, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(sup, "RUNTIME_DIR", tmp_path)
    return tmp_path



def test_build_argv_minimal():
    from llm.providers.omlx_supervisor import _build_argv
    argv = _build_argv("127.0.0.1", 5200, {})
    assert argv == ["omlx", "serve", "--host", "127.0.0.1", "--port", "5200"]


def test_build_argv_with_settings():
    from llm.providers.omlx_supervisor import _build_argv
    settings = {
        "model_dir": "/Users/alice/models",
        "max_model_memory": "200GB",
        "log_level": "info",
        "api_key": "secret",
        "max_concurrent_requests": 4,
        "no_cache": False,           # falsy → omitted
        "ignored_extra_field": "x",  # unknown → ignored, not crashed
    }
    argv = _build_argv("0.0.0.0", 8000, settings)
    assert "--host" in argv and argv[argv.index("--host") + 1] == "0.0.0.0"
    assert "--port" in argv and argv[argv.index("--port") + 1] == "8000"
    assert "--model-dir" in argv
    assert argv[argv.index("--model-dir") + 1] == "/Users/alice/models"
    assert "--max-model-memory" in argv
    assert argv[argv.index("--max-model-memory") + 1] == "200GB"
    assert "--api-key" in argv
    assert "--max-concurrent-requests" in argv
    assert argv[argv.index("--max-concurrent-requests") + 1] == "4"
    assert "--no-cache" not in argv
    assert "ignored_extra_field" not in " ".join(argv)


def test_build_argv_no_cache_flag():
    from llm.providers.omlx_supervisor import _build_argv
    argv = _build_argv("127.0.0.1", 5200, {"no_cache": True})
    assert "--no-cache" in argv



def test_pid_roundtrip(isolated_runtime):
    from llm.providers.omlx_supervisor import _read_pid, _write_pid, _clear_pid
    sid = "ns-pid-test"
    assert _read_pid(sid) is None
    _write_pid(sid, 4242)
    assert _read_pid(sid) == 4242
    _clear_pid(sid)
    assert _read_pid(sid) is None
    _clear_pid(sid)


def test_reconcile_clears_stale_pid(isolated_runtime):
    from llm.providers.omlx_supervisor import reconcile, _write_pid, _read_pid
    sid = "ns-reconcile-stale"
    _write_pid(sid, 999999)
    res = reconcile({"server_id": sid})
    assert res["adopted"] is False
    assert res["cleared"] is True
    assert _read_pid(sid) is None


def test_reconcile_adopts_live_pid(isolated_runtime):
    from llm.providers.omlx_supervisor import reconcile, _write_pid
    sid = "ns-reconcile-live"
    _write_pid(sid, os.getpid())
    res = reconcile({"server_id": sid})
    assert res["adopted"] is True
    assert res["cleared"] is False
    assert res["pid"] == os.getpid()


def test_reconcile_handles_missing_pid_file(isolated_runtime):
    from llm.providers.omlx_supervisor import reconcile
    res = reconcile({"server_id": "ns-no-file"})
    assert res == {"adopted": False, "cleared": False, "pid": None}



def test_stop_server_sigterms_real_process(isolated_runtime):
    from llm.providers.omlx_supervisor import stop_server, _write_pid, _read_pid
    proc = subprocess.Popen(
        ["sleep", "60"], stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    sid = "ns-stop-test"
    _write_pid(sid, proc.pid)
    try:
        result = asyncio.run(stop_server({"server_id": sid}))
        assert result["ok"] is True
        assert _read_pid(sid) is None
        proc.wait(timeout=2)
        assert proc.returncode is not None
    finally:
        if proc.poll() is None:
            proc.kill()


def test_stop_server_does_not_signal_reused_pid(isolated_runtime, monkeypatch):
    from llm.providers import omlx_supervisor as sup

    sid = "ns-stop-pid-reuse"
    sup._write_pid(sid, 4242)
    signals = []
    exit_checks = 0

    def observe_exit(_pid):
        nonlocal exit_checks
        exit_checks += 1
        return exit_checks == 1

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(sup, "_process_alive", lambda _pid: True)
    monkeypatch.setattr(sup, "_process_exited", observe_exit)
    monkeypatch.setattr(sup.os, "kill", lambda _pid, sig: signals.append(sig))
    monkeypatch.setattr(sup.asyncio, "sleep", no_sleep)

    result = asyncio.run(sup.stop_server({"server_id": sid}))

    assert result["ok"] is True
    assert signals == [signal.SIGTERM]
    assert exit_checks == 1


def test_stop_server_confirms_forced_exit_before_clearing_pid(
    isolated_runtime, monkeypatch,
):
    from llm.providers import omlx_supervisor as sup

    sid = "ns-stop-forced-exit"
    sup._write_pid(sid, 4242)
    signals = []
    observations = iter([False, True])

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(sup, "SIGTERM_GRACE_S", 0)
    monkeypatch.setattr(sup, "_process_alive", lambda _pid: True)
    monkeypatch.setattr(sup, "_process_exited", lambda _pid: next(observations))
    monkeypatch.setattr(sup.os, "kill", lambda _pid, sig: signals.append(sig))
    monkeypatch.setattr(sup.asyncio, "sleep", no_sleep)

    result = asyncio.run(sup.stop_server({"server_id": sid}))

    assert result["ok"] is True
    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert sup._read_pid(sid) is None
    with pytest.raises(StopIteration):
        next(observations)


def test_stop_server_no_pid_file(isolated_runtime):
    from llm.providers.omlx_supervisor import stop_server
    result = asyncio.run(stop_server({"server_id": "ns-never-started"}))
    assert result["ok"] is False
    assert "not running" in result["message"].lower()


def test_stop_server_dead_pid_idempotent(isolated_runtime):
    from llm.providers.omlx_supervisor import stop_server, _write_pid, _read_pid
    sid = "ns-stop-stale"
    _write_pid(sid, 999999)
    result = asyncio.run(stop_server({"server_id": sid}))
    assert result["ok"] is True
    assert result.get("already_stopped") is True
    assert _read_pid(sid) is None


def test_stop_server_missing_server_id(isolated_runtime):
    from llm.providers.omlx_supervisor import stop_server
    result = asyncio.run(stop_server({}))
    assert result["ok"] is False
    assert "server_id" in result["message"]



def test_start_server_missing_server_id(isolated_runtime):
    from llm.providers.omlx_supervisor import start_server
    result = asyncio.run(start_server({}))
    assert result["ok"] is False
    assert "server_id" in result["message"]


def test_start_server_missing_host_port(isolated_runtime, monkeypatch):
    from llm.providers.omlx_supervisor import start_server
    from llm.providers import omlx_supervisor as sup
    monkeypatch.setattr(sup, "_resolve_endpoint", lambda cfg: (None, None))
    result = asyncio.run(start_server({"server_id": "ns-no-endpoint"}))
    assert result["ok"] is False
    assert "host" in result["message"].lower() or "port" in result["message"].lower()


def test_start_server_missing_omlx_binary(isolated_runtime, monkeypatch):
    from llm.providers import omlx_supervisor as sup
    monkeypatch.setattr(sup.shutil, "which", lambda name: None)
    monkeypatch.setattr(sup, "_port_owner_pid", lambda h, p: None)
    result = asyncio.run(sup.start_server(
        {"server_id": "ns-no-binary", "host": "127.0.0.1", "port": 9999},
    ))
    assert result["ok"] is False
    assert "omlx" in result["message"].lower()
    assert "path" in result["message"].lower()


def test_start_server_idempotent_when_pid_owns_port(isolated_runtime, monkeypatch):
    from llm.providers import omlx_supervisor as sup
    sid = "ns-idem-test"
    sup._write_pid(sid, os.getpid())
    monkeypatch.setattr(sup, "_port_owner_pid", lambda h, p: os.getpid())
    result = asyncio.run(sup.start_server(
        {"server_id": sid, "host": "127.0.0.1", "port": 9999},
    ))
    assert result["ok"] is True
    assert result.get("already_running") is True
    assert result["pid"] == os.getpid()


def test_start_server_port_in_use_by_foreign_pid(isolated_runtime, monkeypatch):
    from llm.providers import omlx_supervisor as sup
    monkeypatch.setattr(sup, "_port_owner_pid", lambda h, p: 99999)
    monkeypatch.setattr(sup.shutil, "which", lambda n: "/opt/homebrew/bin/omlx")
    result = asyncio.run(sup.start_server(
        {"server_id": "ns-port-conflict", "host": "127.0.0.1", "port": 9999},
    ))
    assert result["ok"] is False
    assert "9999" in result["message"]
    assert "use" in result["message"].lower()



def test_omlx_module_reexports_real_supervisor(isolated_runtime, monkeypatch):
    from llm.providers import omlx
    result = asyncio.run(omlx.start_server({}))
    assert result["ok"] is False
    assert "server_id" in result["message"]
