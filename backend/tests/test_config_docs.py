"""Keep the generated configuration reference in sync with named reads."""

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]


def test_configuration_reference_is_current():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/docs/generate_config.py"), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "Configuration reference is out of date. Regenerate with:\n"
        "python scripts/docs/generate_config.py\n"
        f"{result.stdout}{result.stderr}"
    )
