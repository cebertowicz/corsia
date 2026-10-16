"""The desktop app and backend must keep update terminal codes aligned."""

from __future__ import annotations

import re
from pathlib import Path


def test_desktop_update_stage_exit_codes_match_backend_prepare_contract():
    root = Path(__file__).resolve().parents[2]
    desktop = (root / "desktop" / "src-tauri" / "src" / "update_stage.rs").read_text()
    backend = (root / "backend" / "api" / "settings_restart.py").read_text()

    codes = {
        name: int(value)
        for name, value in re.findall(
            r"pub const (UPDATE_STAGED|FETCH_FAILED|BUILD_FAILED): i32 = (\d+);", desktop
        )
    }
    assert codes == {"UPDATE_STAGED": 0, "FETCH_FAILED": 41, "BUILD_FAILED": 42}
    assert re.search(r"\b41:\s*\"fetch_failed\"", backend)
    assert re.search(r"\b42:\s*\"build_failed\"", backend)
