"""T8: after the data folder move, the Devices listener still restores.

`DevicesRuntime.restore` reopens the listener only when this process's state
root is the one the installed bootstrap selects. The desktop move rewrites the
version 2 bootstrap's `state_root` to `CORSIA/state` and starts the service
there, so both sides name the new folder.
"""

import json

from core import devices_runtime
from infra import config


def _bootstrap(path, state_root, workspace):
    path.write_text(
        json.dumps({
            "version": 2,
            "workspace": {"id": "w1", "name": "Corsia", "root": str(workspace)},
            "state_root": str(state_root),
        }),
        encoding="utf-8",
    )


def test_a_moved_install_still_owns_its_bootstrapped_state_root(tmp_path, monkeypatch):
    support = tmp_path / "Library" / "Application Support"
    legacy = support / "com.cebertowicz.corsia" / "state"
    moved = support / "CORSIA" / "state"
    moved.mkdir(parents=True)
    bootstrap = tmp_path / ".corsia" / "config.json"
    bootstrap.parent.mkdir()
    monkeypatch.setattr(config, "BOOTSTRAP_CONFIG_PATH", bootstrap)

    # Before the move the bootstrap and the service both name the old folder.
    _bootstrap(bootstrap, legacy, tmp_path / "Corsia")
    monkeypatch.setattr(devices_runtime, "STATE_ROOT", legacy.resolve())
    assert devices_runtime._matches_bootstrapped_state_root()

    # The move rewrites the bootstrap and the service starts on the new folder.
    _bootstrap(bootstrap, moved, tmp_path / "Corsia")
    monkeypatch.setattr(devices_runtime, "STATE_ROOT", moved.resolve())
    assert devices_runtime._matches_bootstrapped_state_root()

    # A service still on the old folder after the rewrite would not reopen the listener.
    monkeypatch.setattr(devices_runtime, "STATE_ROOT", legacy.resolve())
    assert not devices_runtime._matches_bootstrapped_state_root()
