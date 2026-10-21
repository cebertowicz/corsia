
from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil
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


def test_build_argv_uses_the_bundled_managed_binary(monkeypatch):
    from llm.providers import omlx_process

    bundled = "/Applications/CORSIA.app/Contents/Resources/bin/omlx"
    monkeypatch.setattr(omlx_process, "managed_omlx_executable", lambda: bundled)

    assert omlx_process.build_argv("127.0.0.1", 5200, {})[0] == bundled


def test_managed_supervisor_argv_uses_the_bundled_binary_only_for_the_managed_row(monkeypatch):
    from llm.providers import omlx_supervisor as sup

    bundled = "/Applications/CORSIA.app/Contents/Resources/bin/omlx"
    monkeypatch.setattr(sup.process, "managed_omlx_executable", lambda: bundled)
    monkeypatch.setattr(sup.shutil, "which", lambda _name: "/opt/homebrew/bin/omlx")
    monkeypatch.setattr(sup, "_is_managed_omlx_row", lambda _settings: True)

    argv = sup._managed_omlx_argv("127.0.0.1", 5200, {})

    assert isinstance(argv, list)
    assert argv[:2] == [bundled, "serve"]


def test_legacy_supervisor_argv_uses_path_binary(monkeypatch):
    from llm.providers import omlx_supervisor as sup

    monkeypatch.setattr(sup.process, "managed_omlx_executable", lambda: "/Applications/CORSIA.app/Contents/Resources/bin/omlx")
    monkeypatch.setattr(sup.shutil, "which", lambda _name: "/opt/homebrew/bin/omlx")
    monkeypatch.setattr(sup, "_is_managed_omlx_row", lambda _settings: False)

    argv = sup._managed_omlx_argv("127.0.0.1", 8000, {})

    assert isinstance(argv, list)
    assert argv[:2] == ["/opt/homebrew/bin/omlx", "serve"]


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
    assert any(arg.startswith("--api-key=") for arg in argv)
    assert "--max-concurrent-requests" in argv
    assert argv[argv.index("--max-concurrent-requests") + 1] == "4"
    assert "--no-cache" not in argv
    assert "ignored_extra_field" not in " ".join(argv)


def test_build_argv_no_cache_flag():
    from llm.providers.omlx_supervisor import _build_argv
    argv = _build_argv("127.0.0.1", 5200, {"no_cache": True})
    assert "--no-cache" in argv


def test_build_argv_binds_a_key_that_starts_with_a_dash():
    import argparse

    from llm.providers.omlx_supervisor import _build_argv
    dashed = "-" + "x" * 42
    argv = _build_argv("127.0.0.1", 5200, {"api_key": dashed})
    parser = argparse.ArgumentParser()
    parser.add_argument("--host")
    parser.add_argument("--port")
    parser.add_argument("--api-key")
    assert parser.parse_args(argv[2:]).api_key == dashed



def test_pid_roundtrip(isolated_runtime):
    from llm.providers.omlx_supervisor import _read_pid, _write_pid, _clear_pid
    sid = "ns-pid-test"
    assert _read_pid(sid) is None
    _write_pid(sid, os.getpid())
    assert _read_pid(sid) == os.getpid()
    _clear_pid(sid)
    assert _read_pid(sid) is None
    _clear_pid(sid)


def test_missing_lsof_is_an_unknown_port_owner(monkeypatch):
    from llm.providers import omlx_process

    monkeypatch.setattr(omlx_process.shutil, "which", lambda _name: None)

    assert omlx_process.port_owner_pid("127.0.0.1", 8200) is omlx_process.PORT_OWNER_UNKNOWN


@pytest.mark.parametrize(("stdout", "stderr", "free", "expected"), [
    ("", "", True, None),
    ("", "", False, "unknown"),
    ("", "permission denied", False, "unknown"),
])
def test_lsof_exit_one_only_means_no_owner_when_the_port_is_free(monkeypatch, stdout, stderr, free, expected):
    from llm.providers import omlx_process

    monkeypatch.setattr(omlx_process.shutil, "which", lambda _name: "lsof")
    monkeypatch.setattr(omlx_process.subprocess, "run", lambda *_args, **_kwargs: subprocess.CompletedProcess(
        ["lsof"], 1, stdout, stderr,
    ))
    monkeypatch.setattr(omlx_process, "port_is_free", lambda _port: free)

    owner = omlx_process.port_owner_pid("127.0.0.1", 8200)

    assert owner is None if expected is None else owner is omlx_process.PORT_OWNER_UNKNOWN


def test_reconcile_clears_stale_pid(isolated_runtime):
    from llm.providers.omlx_supervisor import reconcile, _pid_path, _read_pid
    sid = "ns-reconcile-stale"
    _pid_path(sid).write_text(json.dumps({"pid": 999999, "create_time": 1.0}))
    res = reconcile({"server_id": sid})
    assert res["adopted"] is False
    assert res["cleared"] is True
    assert _read_pid(sid) is None


def test_legacy_pid_record_is_adopted_only_when_its_pid_owns_the_row_port(isolated_runtime, monkeypatch):
    from llm.providers import omlx_supervisor as sup

    sid, port = "legacy-owner", 8200
    sup._pid_path(sid).write_text(str(os.getpid()))
    monkeypatch.setattr(sup, "_port_owner_pid", lambda _host, observed_port: os.getpid() if observed_port == port else None)

    result = sup.reconcile({"server_id": sid, "host": "127.0.0.1", "port": port})

    assert result == {"adopted": True, "cleared": False, "pid": os.getpid()}
    assert json.loads(sup._pid_path(sid).read_text())["pid"] == os.getpid()


def test_legacy_pid_record_survives_an_unknown_or_empty_port_probe(isolated_runtime, monkeypatch):
    from llm.providers import omlx_supervisor as sup

    sid, port = "legacy-foreign", 8201
    sup._pid_path(sid).write_text(str(os.getpid()))
    monkeypatch.setattr(sup, "_port_owner_pid", lambda *_args: None)
    monkeypatch.setattr(sup.os, "kill", lambda *_args: pytest.fail("an unproven record must not signal a process"))

    result = asyncio.run(sup.stop_server({"server_id": sid, "host": "127.0.0.1", "port": port}))

    assert result["already_stopped"]
    assert sup._pid_path(sid).exists()


def test_legacy_pid_record_is_deleted_only_for_a_positive_port_mismatch(isolated_runtime, monkeypatch):
    from llm.providers import omlx_supervisor as sup

    sid, port = "legacy-mismatch", 8202
    sup._pid_path(sid).write_text(str(os.getpid()))
    monkeypatch.setattr(sup, "_port_owner_pid", lambda *_args: 999999)

    assert sup._read_pid(sid, host="127.0.0.1", port=port) is None
    assert not sup._pid_path(sid).exists()


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
    sup._write_pid(sid, os.getpid())
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
    sup._write_pid(sid, os.getpid())
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
    from llm.providers.omlx_supervisor import stop_server, _pid_path, _read_pid
    sid = "ns-stop-stale"
    _pid_path(sid).write_text(json.dumps({"pid": 999999, "create_time": 1.0}))
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
    monkeypatch.setattr(sup.process, "managed_omlx_executable", lambda: None)
    monkeypatch.setattr(sup.shutil, "which", lambda _name: None)
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
    monkeypatch.setattr(sup.process, "managed_omlx_executable", lambda: "/opt/homebrew/bin/omlx")
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


def test_readiness_wait_fails_at_once_when_the_new_child_exits(monkeypatch):
    from llm.providers import omlx_supervisor as sup

    monkeypatch.setattr(sup, "_process_exited", lambda _pid: True)
    began = time.monotonic()
    assert asyncio.run(sup._wait_for_healthy("127.0.0.1", 9, pid=1234)) is False
    assert time.monotonic() - began < 5


def test_new_omlx_start_waits_on_its_child_not_a_fixed_30_seconds(isolated_runtime, monkeypatch):
    from llm.providers import omlx_supervisor as sup

    class Spawned:
        pid = 1234

    waited = {}

    async def healthy(_host, _port, **kwargs):
        waited.update(kwargs)
        return True

    monkeypatch.setattr(sup, "_port_owner_pid", lambda *_args: None)
    monkeypatch.setattr(sup, "_managed_omlx_argv", lambda *_args: ["omlx"])
    monkeypatch.setattr(sup, "_spawn_process", lambda *_args: Spawned())
    monkeypatch.setattr(sup, "_wait_for_healthy", healthy)

    result = asyncio.run(sup.start_server({"server_id": "progress", "host": "127.0.0.1", "port": 9999}))

    assert result["ok"] is True
    assert waited == {"server_id": "progress", "pid": 1234}
