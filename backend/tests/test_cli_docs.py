"""Keep the generated CLI reference in sync with the command definitions."""

import argparse
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def test_cli_reference_is_current():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/docs/generate_cli.py"), "--check"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def check_with_shell(shell):
    # A subprocess: the generator repoints workspace paths and imports the CLI modules.
    code = ("import sys; from pathlib import Path; import generate_cli; "
            "generate_cli.SHELL = Path(sys.argv[1]); sys.argv[1:] = ['--check']; "
            "raise SystemExit(generate_cli.main())")
    return subprocess.run(
        [sys.executable, "-c", code, str(shell)],
        cwd=ROOT / "scripts/docs", capture_output=True, text=True,
    )


def test_source_help_change_makes_reference_stale(tmp_path):
    shell = tmp_path / "corsia"
    text = (ROOT / "scripts/corsia").read_text()
    shell.write_text(text)
    result = check_with_shell(shell)
    assert result.returncode == 0, result.stdout + result.stderr
    shell.write_text(text.replace("Open the local CORSIA app", "Open the local application"))
    assert check_with_shell(shell).returncode == 1


def test_parser_defaults_are_readable(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts/docs"))
    from generate_cli import describe_parser

    parser = argparse.ArgumentParser()
    parser.add_argument("--enabled", action="store_true")
    parser.add_argument("--label", default="")
    parser.add_argument("--count", default=5, help="Count (default 5).")
    parser.add_argument("--mode", default="one", help="Mode.")
    lines = []
    describe_parser(lines, parser, "corsia example")
    output = "\n".join(lines)
    assert "Default: False" not in output
    assert "Default:  |" not in output
    assert "Count (default 5)" in output and "Default: 5" not in output
    assert "Mode; Default: one" in output
    assert ".;" not in output
