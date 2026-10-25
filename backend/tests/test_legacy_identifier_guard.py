"""The old app identifier names data folders only where CORSIA finds or moves them.

`com.cebertowicz.corsia` was the bundle identifier, and an install adopted from a
source checkout kept its data under it. New code must use `CORSIA/state`; the
old name may appear only to detect, move or test that legacy folder, or to
verify an older signed build. A new match outside this list fails here.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LEGACY = "com.cebertowicz.corsia"

ALLOWED = {
    # Legacy-folder detection and its fallbacks.
    "backend/infra/platform_paths.py",
    "desktop/src-tauri/src/first_run.rs",
    "scripts/corsia-backup-db",
    # The one-time move and its tests.
    "desktop/src-tauri/src/state_move.rs",
    "desktop/src-tauri/src/state_move_tests.rs",
    # Older signed builds carry the old identifier in their code signature.
    "desktop/src-tauri/src/update_stage.rs",
    # Fixtures shaped like a pre-move install.
    "backend/tests/fixtures/state_dir_validity.txt",
    "backend/tests/test_corsia_app_mode.py",
    "backend/tests/test_desktop_service_launcher.py",
    "backend/tests/test_moved_state_heal.py",
    "backend/tests/test_platform_paths.py",
    "backend/tests/test_stop_recorded_processes.py",
    "backend/tests/test_legacy_identifier_guard.py",
    "desktop/src-tauri/src/adoption.rs",
    "desktop/src-tauri/src/app_paths.rs",
    "desktop/src-tauri/src/start_fresh.rs",
}


@pytest.mark.skipif(shutil.which("git") is None or not (REPO / ".git").exists(), reason="needs a git checkout")
def test_the_old_identifier_appears_only_where_legacy_data_is_found_or_moved():
    listed = subprocess.run(
        ["git", "grep", "-l", "-F", LEGACY],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert listed.returncode in (0, 1), listed.stderr
    found = set(listed.stdout.split())

    assert found - ALLOWED == set(), "new uses of the old identifier: use CORSIA/state, or add a legacy-only use here"
