"""Keep the generated data-model reference in sync with the store schemas."""

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
REGENERATE = "PYTHONPATH=backend .venv/bin/python scripts/docs/generate_data_model.py"


def test_data_model_reference_is_current():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/docs/generate_data_model.py"), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"Data-model reference is out of date. Regenerate with:\n{REGENERATE}\n"
        f"{result.stdout}{result.stderr}"
    )
