"""Keep the generated configuration reference in sync with named reads."""

from collections import defaultdict
import importlib.util
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]


def test_constant_keys_and_defaults_are_discovered(tmp_path):
    spec = importlib.util.spec_from_file_location("config_generator", ROOT / "scripts/docs/generate_config.py")
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    generator.ROOT = tmp_path
    backend = tmp_path / "backend"
    backend.mkdir()
    constants = backend / "constants.py"
    constants.write_text('KEY = "CORSIA_IMPORTED"\nDEFAULT = 1956\n')
    source = backend / "reader.py"
    source.write_text(
        'import os\nimport constants as settings\nfrom constants import KEY, DEFAULT\n'
        'LOCAL = "CORSIA_LOCAL"\n'
        'os.getenv(KEY, DEFAULT)\nos.environ[LOCAL]\n'
        'os.getenv(settings.KEY, settings.DEFAULT)\n'
        'def read(name):\n    return os.environ.get(name)\n'
        'read(LOCAL)\nread(name=KEY)\n'
        'os.getenv("CORSIA_COMPUTED", str(DEFAULT))\n'
    )
    tables = generator.module_tables([constants, source])
    found = defaultdict(lambda: (set(), set()))
    generator.python_reads(source, found, tables)
    assert found["CORSIA_IMPORTED"][1] == {"1956", "unset"}
    assert found["CORSIA_LOCAL"][1] == {"unset"}
    assert found["CORSIA_COMPUTED"][1] == {"computed"}


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
