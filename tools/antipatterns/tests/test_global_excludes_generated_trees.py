"""Tests for `[global].exclude_globs` in the shipped default config.

These cover the whole-repo `--all` scan path (`cli._walk_repo` plus the
engine's global-exclude filter), not one rule. The regression they guard:
a worktree that has built the desktop app also holds a generated Cargo
tree at `desktop/src-tauri/target/`, and `--all` scanned it. That tree is
dependency and build output, not author code — it carries a copy of the
Python runtime and every vendored package, so the blocking error-severity
gate reported third-party code as if this repo authored it.

The paired guard on every exclusion here is over-matching: a path that only
looks generated (`targets/`, `target.py`) must still be scanned.
"""

from __future__ import annotations

import json

import pytest

# Fires `silent-error-swallow` (severity=error) wherever it is scanned, so a
# generated copy and an author copy of the same code differ only by path.
SWALLOW_SRC = (
    "def risky():\n"
    "    try:\n"
    "        go()\n"
    "    except:\n"
    "        pass\n"
)

# Plain author code: scanned, and fires nothing.
QUIET_SRC = "def ok():\n    return 1\n"

# The real shape: a bundled Python stdlib module inside a built app bundle.
GENERATED_NESTED = (
    "desktop/src-tauri/target/release/bundle/macos/CORSIA.app/Contents/"
    "Resources/python/lib/python3.11/idlelib/debugger.py"
)
GENERATED_NESTED_DEP = "desktop/src-tauri/target/debug/build/foo/out/bindings.rs"
# A crate at the repo root writes `target/` with no parent directory. The
# `--all` walker matches with fnmatch, where a leading `**/` requires a parent,
# so this depth needs its own entry in the config.
GENERATED_ROOT = "target/release/build/foo/out/bindings.py"

AUTHOR_FILE = "backend/services/real.py"
AUTHOR_LOOKALIKE_DIR = "backend/targets/list.py"
AUTHOR_LOOKALIKE_FILE = "desktop/src-tauri/src/target.py"


@pytest.fixture
def repo(tmp_path):
    """Synthesize a repo containing generated and author copies of one file.

    Returns:
        Callable that writes `{rel_path: source}` under a fresh repo root and
        returns that root as a `Path`.
    """

    def _build(sources: dict[str, str]):
        for rel, source in sources.items():
            path = tmp_path / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source, encoding="utf-8")
        return tmp_path

    return _build


@pytest.fixture
def scanned_paths(repo):
    """Return the repo root and the paths `--all` would scan, via the walker."""
    from antipatterns.cli import _walk_repo
    from antipatterns.config import global_exclude_globs, load_config

    def _scan(sources: dict[str, str]):
        root = repo(sources)
        # No `.antipatterns.toml` is written, so this is the shipped default.
        excludes = global_exclude_globs(load_config(root))
        return root, _walk_repo(root, excludes)

    return _scan


def test_default_config_ships_target_tree_excludes(tmp_path):
    """The built-in config excludes generated `target/` trees at any depth."""
    from antipatterns.config import global_exclude_globs, load_config

    # tmp_path holds no `.antipatterns.toml`, so this is the shipped default.
    excludes = global_exclude_globs(load_config(tmp_path))
    assert "**/target/**" in excludes
    assert "target/**" in excludes


def test_all_scan_skips_generated_target_trees(scanned_paths):
    """Nothing under a generated `target/` tree reaches the scan."""
    _, scanned = scanned_paths(
        {
            GENERATED_NESTED: SWALLOW_SRC,
            GENERATED_NESTED_DEP: SWALLOW_SRC,
            GENERATED_ROOT: SWALLOW_SRC,
        }
    )
    assert scanned == []


def test_all_scan_still_scans_target_lookalikes(scanned_paths):
    """The exclusion does not swallow author paths that merely look similar."""
    _, scanned = scanned_paths(
        {
            AUTHOR_FILE: QUIET_SRC,
            AUTHOR_LOOKALIKE_DIR: QUIET_SRC,
            AUTHOR_LOOKALIKE_FILE: QUIET_SRC,
        }
    )
    assert set(scanned) == {
        AUTHOR_FILE,
        AUTHOR_LOOKALIKE_DIR,
        AUTHOR_LOOKALIKE_FILE,
    }


def test_engine_reports_author_copy_but_not_generated_copy(repo, monkeypatch, capsys):
    """`--all` reports the author file and stays silent on its generated twin.

    End-to-end through the CLI, so the assertion covers the walker, the
    config, and the engine's own global-exclude filter together.

    Note: `--all` hands the engine repo-relative paths, and the engine
    resolves them against the process cwd, so this runs from the repo root
    exactly as CI and `check-pr` do.
    """
    from antipatterns import cli

    monkeypatch.setenv("CORSIA_ANALYTICS_DISABLE", "1")
    root = repo(
        {
            GENERATED_NESTED: SWALLOW_SRC,
            GENERATED_ROOT: SWALLOW_SRC,
            AUTHOR_FILE: SWALLOW_SRC,
        }
    )
    monkeypatch.chdir(root)

    code = cli.main(["--all", "--severity", "error", "--format", "json",
                     "--repo-root", str(root)])
    payload = json.loads(capsys.readouterr().out)

    files = {finding["file"] for finding in payload["findings"]}
    assert AUTHOR_FILE in files
    assert not [f for f in files if "target/" in f]
    # The author finding still blocks: the exclusion did not disarm the gate.
    assert code == 1
