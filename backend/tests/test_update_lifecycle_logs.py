"""Lifecycle output from successive operations remains available in restart.log."""

import asyncio
import os
import shutil

import pytest


@pytest.mark.asyncio
async def test_failed_updates_append_lifecycle_and_cleanup_output(tmp_path, monkeypatch):
    from api.settings_restart import _launch_settings_restart

    curl = tmp_path / "curl"
    curl.write_text("#!/bin/bash\nprintf '503'\nexit 22\n")
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
            lambda name: tmp_path / name, caller_token="test-credential",
        )
        assert await asyncio.to_thread(process.wait, 5) == 17

    text = (tmp_path / "restart.log").read_text()
    assert all(f"lifecycle operation {operation_id}" in text for operation_id in ids)
    assert text.count("Safe update refused") == 2
    assert text.count("Lifecycle drain cleanup curl exit 22") == 2
    assert "test-credential" not in text
