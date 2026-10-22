"""Loads ``scripts/verdict_calibration.py`` for the verdict tests; scripts/ is not a package."""
from __future__ import annotations

import importlib.util
from pathlib import Path


def calibration_script():
    path = Path(__file__).resolve().parents[2] / "scripts" / "verdict_calibration.py"
    spec = importlib.util.spec_from_file_location("verdict_calibration", path)
    assert spec and spec.loader
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    return script
