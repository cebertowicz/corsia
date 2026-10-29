"""Lifecycle failures keep their own output and cleanup result."""

import asyncio
import os
import shutil

import pytest


@pytest.mark.asyncio
async def test_each_failed_update_keeps_its_lifecycle_and_cleanup_log(tmp_path, monkeypatch):
    from api.settings_restart import _launch_settings_restart

    curl = tmp_path / "curl"
    curl.write_text(
        "#!/bin/bash\ncase \"$*\" in */api/health*) ;; *) exit 90 ;; esac\n"
        "printf 'Lifecycle backend health HTTP 503\\n'\nprintf 'curl: server error\\n' >&2\nexit 22\n"
    )
    curl.chmod(0o755)
    script = tmp_path / "corsia"
    script.write_text("#!/bin/bash\nprintf 'Safe update refused\\n'\nexit 17\n")
    script.chmod(0o755)
    monkeypatch.setattr(shutil, "which", lambda *_args, **_kwargs: str(curl))
    ids = ["a" * 32, "b" * 32]
    for operation_id in ids:
        environment = {**os.environ, "CORSIA_RESTART_BACKEND_PORT": "4111"}
        process = await _launch_settings_restart(
            operation_id, True, script, environment, ["restart-prepared"],
            lambda name: tmp_path / name, caller_token="test-only-credential",
        )
        assert await asyncio.to_thread(process.wait, 5) == 17

    for operation_id in ids:
        text = (tmp_path / f"restart-{operation_id}.log").read_text()
        assert "Safe update refused" in text
        assert "Lifecycle backend health HTTP 503" in text
        assert "curl: server error" not in text
        assert "Lifecycle backend health curl exit 22" in text
        assert "test-only-credential" not in text


@pytest.mark.asyncio
async def test_watcher_setup_failure_does_not_abort_a_launched_restart(tmp_path, monkeypatch):
    from api import settings_restart
    from automation import restart

    child = object()
    monkeypatch.setattr(settings_restart, "spawn_restart_lifecycle", lambda *_a, **_kw: child)
    aborted = []

    async def abort(*args, **kwargs):
        aborted.append((args, kwargs))
        return True

    def watcher(*_args):
        raise OSError("watcher setup failed")

    monkeypatch.setattr(restart, "abort_restart_control", abort)
    with pytest.raises(OSError, match="watcher setup failed"):
        await settings_restart._launch_settings_restart(
            "a" * 32, True, tmp_path / "corsia", {}, [],
            lambda name: tmp_path / name, on_launch=watcher,
        )
    assert not aborted
