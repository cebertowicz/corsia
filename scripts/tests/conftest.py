"""Pytest fixtures for the file-size ratchet tests.

`ratchet` is the shared fixture: points the script at a synthesized tmp_path
repo root instead of the real one, so tests can write scratch files and a
scratch baseline without touching anything real.

The scan asks Git which files are tracked, so the fixture stubs that one
process boundary to report the scratch tree. Everything downstream of it --
source/test classification, sizing, function extraction, the baseline guards
-- runs for real against the synthesized files.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make `import file_size_limits` work without an editable install. The
# module lives at `<repo>/scripts/file_size_limits.py`.
_TESTS_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _TESTS_DIR.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


def _apply_tracked_stub(monkeypatch, root: Path) -> None:
    """Report every file under `root` as Git-tracked, in sorted order."""
    import size_limit_scan

    def _tracked(_root: Path) -> list[str]:
        return sorted(
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_file()
        )

    monkeypatch.setattr(size_limit_scan, "tracked_paths", _tracked)


@pytest.fixture
def stub_tracked_paths(monkeypatch):
    """Return a callable that stubs Git tracking for an arbitrary scratch root."""

    def _apply(root: Path) -> None:
        _apply_tracked_stub(monkeypatch, root)

    return _apply


class Ratchet:
    """Thin harness around `file_size_limits` pointed at a scratch root."""

    def __init__(self, module, root: Path):
        self.module = module
        self.root = root

    def write(self, rel_path: str, lines: int, *, suffix: str | None = None) -> None:
        """Write a scanned source file with exactly `lines` lines of body."""
        target = self.root / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        ext = suffix if suffix is not None else target.suffix
        body_line = "x = 1" if ext == ".py" else "line;"
        target.write_text("\n".join([body_line] * lines) + "\n", encoding="utf-8")

    def delete(self, rel_path: str) -> None:
        (self.root / rel_path).unlink()

    def write_undecodable(self, rel_path: str) -> None:
        """Write a scanned file containing a byte sequence that is not valid UTF-8."""
        target = self.root / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x = 1\n\xff\xfe invalid utf-8 \xff\n")

    def write_source(self, rel_path: str, text: str) -> None:
        """Write exact source text, for function-shape and parser cases."""
        target = self.root / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def write_function(self, rel_path: str, name: str, body_lines: int) -> None:
        """Write a Python file holding one function of a known total span."""
        self.write_source(
            rel_path, f"def {name}():\n" + "    pass\n" * body_lines
        )

    def write_baseline(
        self,
        limits: dict[str, int] | None = None,
        append_only: dict[str, str] | None = None,
        function_limits: dict[str, int] | None = None,
    ) -> None:
        import json

        payload: dict = {"_comment": ["test baseline"], "limits": limits or {}}
        if function_limits:
            payload["function_limits"] = function_limits
        if append_only:
            payload["append_only"] = append_only
        self.module._LIMITS.parent.mkdir(parents=True, exist_ok=True)
        self.module._LIMITS.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def read_baseline(self) -> dict:
        import json

        return json.loads(self.module._LIMITS.read_text(encoding="utf-8"))

    def run(self, argv: list[str]) -> int:
        return self.module.main(argv)


@pytest.fixture
def ratchet(tmp_path, monkeypatch):
    import file_size_limits as module

    monkeypatch.setattr(module, "_ROOT", tmp_path)
    monkeypatch.setattr(module, "_LIMITS", tmp_path / "quality" / "file-size-limits.json")
    _apply_tracked_stub(monkeypatch, tmp_path)
    return Ratchet(module, tmp_path)
