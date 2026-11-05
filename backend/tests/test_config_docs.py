"""Keep the generated configuration reference in sync with named reads."""

from collections import defaultdict
import importlib.util
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]


def test_names_and_literal_defaults_are_discovered(tmp_path):
    spec = importlib.util.spec_from_file_location("config_generator", ROOT / "scripts/docs/generate_config.py")
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    generator.ROOT = tmp_path
    source = tmp_path / "reader.py"
    source.write_text(
        'import os\nKEY = "CORSIA_LOCAL"\n'
        'os.getenv(KEY, "ignored")\n'
        'os.getenv("CORSIA_LITERAL", "fallback")\n'
        'os.getenv("CORSIA_COMPUTED", str(1956))\n'
        'os.getenv("HOME")\n'
    )
    found = defaultdict(lambda: (set(), set()))
    generator.python_reads(source, found)
    assert found["CORSIA_LOCAL"] == ({"reader.py"}, set())
    assert found["CORSIA_LITERAL"][1] == {repr("fallback")}
    assert found["CORSIA_COMPUTED"][1] == set()
    assert found["HOME"][1] == set()


def test_only_whole_nonempty_fallbacks_are_defaults(tmp_path):
    spec = importlib.util.spec_from_file_location("config_generator", ROOT / "scripts/docs/generate_config.py")
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    generator.ROOT = tmp_path
    shell = tmp_path / "reader.sh"
    shell.write_text(
        'grace="${CORSIA_GRACE:-5}"\n'
        'echo "see ${CORSIA_LOG:-the log} for details"\n'
        '[ -n "${CORSIA_GUARD:-}" ]\n'
    )
    source = tmp_path / "reader.py"
    source.write_text('import os\nos.environ.get("CORSIA_EMPTY", "")\n')
    found = defaultdict(lambda: (set(), set()))
    generator.shell_reads(shell, found)
    generator.python_reads(source, found)
    assert found["CORSIA_GRACE"][1] == {repr("5")}
    assert found["CORSIA_LOG"] == ({"reader.sh"}, set())
    assert found["CORSIA_GUARD"][1] == set()
    assert found["CORSIA_EMPTY"][1] == set()


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
