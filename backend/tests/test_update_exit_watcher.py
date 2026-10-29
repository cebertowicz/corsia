"""Update child exits release the original backend's operation."""

import asyncio
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("returncode", [1, 143])
async def test_failed_update_child_releases_operation(isolated_db, monkeypatch, tmp_path, returncode):
    from api import update_exit_watcher
    from automation import restart, self_restart, update_failure_notice
    from infra import drain

    operation_id = self_restart.try_acquire(None, "owner", None, None, "update", kind="update")
    for previous, target in [("holding", "steering"), ("steering", "draining")]:
        assert self_restart.transition_operation(operation_id, expected=previous, target=target)
    monkeypatch.setattr(drain, "cancel_drain_projection", lambda: None)
    monkeypatch.setattr(update_failure_notice, "tell_conductor_detached", lambda *args: None)
    completed = asyncio.Event()
    original = update_exit_watcher._settle_exited_update

    async def settle(*args):
        try:
            await original(*args)
        finally:
            completed.set()

    monkeypatch.setattr(update_exit_watcher, "_settle_exited_update", settle)
    class Child:
        def wait(self):
            return returncode

    log_path = tmp_path / "custom-lifecycle.log"
    log_path.write_text("Safe update refused\n")
    update_exit_watcher.watch_update_process(Child(), operation_id, log_path)
    await asyncio.wait_for(completed.wait(), 5)
    operation = self_restart.operation_by_id(operation_id)
    assert operation["status"] == "failed"
    assert operation["failure_code"] == "launch_failed"
    assert json.loads(operation["failure_detail"]) == {"exit_code": returncode, "log_path": str(log_path)}
    assert operation["error"] == "Safe update refused"
    assert self_restart.in_flight_request() is None
    if restart._RESUME_TASKS:
        await asyncio.gather(*tuple(restart._RESUME_TASKS))


@pytest.mark.asyncio
async def test_failure_summary_keeps_real_curl_errors(tmp_path, monkeypatch):
    from api.update_exit_watcher import _settle_exited_update
    from automation import restart

    log_path = tmp_path / "custom-lifecycle.log"
    log_path.write_text(
        "curl: update download failed\nLifecycle backend health HTTP 503\n"
    )
    captured = {}

    async def abort(_operation_id, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(restart, "abort_restart_control", abort)
    await _settle_exited_update("op", 1, log_path)
    assert "curl: update download failed" in captured["error"]
    assert "Lifecycle backend health" not in captured["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("returncode", [200, -9])
async def test_teardown_attempt_exit_keeps_hold(isolated_db, monkeypatch, returncode):
    from api import update_exit_watcher
    from automation import self_restart

    operation_id = self_restart.try_acquire(None, "owner", None, None, "update", kind="update")
    for previous, target in [("holding", "steering"), ("steering", "draining")]:
        assert self_restart.transition_operation(operation_id, expected=previous, target=target)
    assert self_restart.transition_operation(operation_id, expected="draining", target="ready")
    assert self_restart.transition_operation(operation_id, expected="ready", target="restarting")
    reported = asyncio.Event()
    loop = asyncio.get_running_loop()
    call_soon = loop.call_soon_threadsafe

    def capture(callback, *args):
        def report():
            callback(*args)
            reported.set()
        return call_soon(report)

    class Child:
        def wait(self):
            return returncode

    monkeypatch.setattr(loop, "call_soon_threadsafe", capture)
    update_exit_watcher.watch_update_process(Child(), operation_id, Path("unused"))
    await asyncio.wait_for(reported.wait(), 5)
    assert self_restart.operation_by_id(operation_id)["phase"] == "restarting"
    assert self_restart.in_flight_request() is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("poll_result", [1, None])
async def test_failed_wait_uses_one_poll(monkeypatch, poll_result):
    from api import update_exit_watcher

    polled = threading.Event()
    settled = asyncio.Event()
    calls = []

    class Child:
        def wait(self):
            raise OSError("wait failed")

        def poll(self):
            calls.append("poll")
            polled.set()
            return poll_result

    async def settle(*args):
        calls.append(args)
        settled.set()

    monkeypatch.setattr(update_exit_watcher, "_settle_exited_update", settle)
    update_exit_watcher.watch_update_process(Child(), "operation", Path("unused"))
    assert await asyncio.to_thread(polled.wait, 5)
    if poll_result is not None:
        await asyncio.wait_for(settled.wait(), 5)
        assert calls == ["poll", ("operation", 1, Path("unused"))]
    else:
        await asyncio.sleep(0)
        assert calls == ["poll"]


@pytest.mark.asyncio
async def test_successful_handoff_does_not_settle(monkeypatch):
    from api import update_exit_watcher

    called = []
    reported = asyncio.Event()
    loop = asyncio.get_running_loop()
    call_soon = loop.call_soon_threadsafe

    def capture_report(callback, *args):
        def run_report():
            callback(*args)
            reported.set()
        return call_soon(run_report)

    class Child:
        def wait(self):
            return 0

    monkeypatch.setattr(loop, "call_soon_threadsafe", capture_report)
    monkeypatch.setattr(update_exit_watcher, "_settle_exited_update", lambda *args: called.append(args))
    update_exit_watcher.watch_update_process(Child(), "operation", Path("unused"))
    await asyncio.wait_for(reported.wait(), 5)
    assert called == []


def test_exit_after_loop_closed_is_only_logged(monkeypatch):
    from api import update_exit_watcher

    release = threading.Event()
    logged = threading.Event()

    class Child:
        def wait(self):
            release.wait()
            return 1

    monkeypatch.setattr(update_exit_watcher.logger, "debug", lambda *_args: logged.set())
    loop = asyncio.new_event_loop()
    try:
        async def start():
            update_exit_watcher.watch_update_process(Child(), "operation", Path("unused"))
        loop.run_until_complete(start())
    finally:
        loop.close()
    release.set()
    assert logged.wait(5)


def test_backend_exit_does_not_wait_for_live_child(tmp_path):
    # A separate interpreter proves asyncio shutdown and interpreter exit both finish.
    script = '''\nimport asyncio, threading\nfrom pathlib import Path\nfrom api.update_exit_watcher import watch_update_process\nclass Child:\n    def wait(self):\n        threading.Event().wait()\nasync def main():\n    watch_update_process(Child(), "operation", Path("unused"))\nasyncio.run(main())\n'''
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    workspace.mkdir()
    state.mkdir()
    env = {
        **os.environ,
        "CORSIA_WORKSPACE": str(workspace),
        "CORSIA_STATE_DIR": str(state),
        "LITELLM_LOCAL_MODEL_COST_MAP": "true",
    }
    subprocess.run([sys.executable, "-c", script], check=True, timeout=10, env=env)
