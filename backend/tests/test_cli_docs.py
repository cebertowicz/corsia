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
