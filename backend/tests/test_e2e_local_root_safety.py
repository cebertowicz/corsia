"""scripts/e2e-local never deletes or creates a root it cannot prove is its own.

Every case runs the real script from a throwaway copy of the repo layout, with
a disposable HOME and ``lsof``/``kill``/``curl`` shims, so no real port,
workspace or state root is ever discovered, killed, booted or written. The
shims record each call: ``lsof`` reports a listener that does not exist and
``kill`` only writes down what it was asked to stop, so a refused run can be
shown to have looked for no listener and stopped none. The ``curl`` shim
answers every health probe, which ends an accepted run at the script's own
stale-listener exit, immediately after workspace prep.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PREP_DONE = "stale listener"


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.repo = tmp_path / "repo"
        self.home = tmp_path / "home"
        self.shims = tmp_path / "shims"
        (self.repo / "scripts").mkdir(parents=True)
        shutil.copy2(REPO_ROOT / "scripts" / "e2e-local", self.repo / "scripts" / "e2e-local")
        shutil.copytree(REPO_ROOT / "scripts" / "corsia.d", self.repo / "scripts" / "corsia.d")
        (self.repo / "backend").symlink_to(REPO_ROOT / "backend")
        (self.repo / "frontend").mkdir()
        self.home.mkdir()
        self.shims.mkdir()
        self.port_log = tmp_path / "port-calls.log"
        self.shim("lsof", f'echo "lsof $*" >> "{self.port_log}"\necho 999999')
        self.shim("kill", f'echo "kill $*" >> "{self.port_log}"')
        self.shim("curl", f'echo "curl $*" >> "{self.port_log}"\nexit 0')
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True, env=self.env())

    def shim(self, name: str, body: str) -> Path:
        path = self.shims / name
        path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def env(self, **extra: str) -> dict[str, str]:
        return {
            "PATH": f"{self.shims}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.home),
            "CORSIA_E2E_MOCK": "1",
            "CORSIA_E2E_PYTHON": sys.executable,
            "PYTHONDONTWRITEBYTECODE": "1",
            **extra,
        }

    def run(self, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(self.repo / "scripts" / "e2e-local"), *args],
            env=self.env(**extra), capture_output=True, text=True, timeout=60,
        )

    def default_root(self, name: str) -> Path:
        return self.repo / "frontend" / ".qa" / name

    def port_calls(self) -> str:
        return self.port_log.read_text(encoding="utf-8") if self.port_log.exists() else ""


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def _output(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


def _assert_refused(harness: Harness, result: subprocess.CompletedProcess[str]) -> None:
    """A refused run stops before it looks for, or stops, anything on the port."""
    assert result.returncode != 0
    assert "refusing" in _output(result), _output(result)
    assert PREP_DONE not in _output(result)
    assert harness.port_calls() == ""


@pytest.mark.parametrize("variable", ["CORSIA_E2E_WORKSPACE", "CORSIA_E2E_STATE_DIR"])
def test_a_caller_root_with_content_is_refused_and_left_intact(harness: Harness, variable: str):
    root = harness.tmp / "caller-root"
    root.mkdir()
    sentinel = root / "keep.txt"
    sentinel.write_text("not disposable", encoding="utf-8")

    result = harness.run(**{variable: str(root)})

    assert sentinel.read_text(encoding="utf-8") == "not disposable", _output(result)
    _assert_refused(harness, result)
    assert not (harness.repo / "frontend" / ".qa").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any directory")
def test_a_caller_root_that_cannot_be_read_is_not_taken_for_empty(harness: Harness):
    root = harness.tmp / "unreadable"
    root.mkdir()
    (root / "keep.txt").write_text("not disposable", encoding="utf-8")
    root.chmod(0o000)
    try:
        result = harness.run(CORSIA_E2E_WORKSPACE=str(root), CORSIA_E2E_STATE_DIR=str(harness.tmp / "state"))
    finally:
        root.chmod(0o700)

    assert (root / "keep.txt").exists()
    _assert_refused(harness, result)
    assert not (harness.tmp / "state").exists()


def test_a_symlinked_caller_root_is_refused_and_its_target_left_intact(harness: Harness):
    target = harness.tmp / "real-tree"
    target.mkdir()
    sentinel = target / "keep.txt"
    sentinel.write_text("not disposable", encoding="utf-8")
    link = harness.tmp / "link"
    link.symlink_to(target)

    result = harness.run(CORSIA_E2E_WORKSPACE=str(link), CORSIA_E2E_STATE_DIR=str(harness.tmp / "state"))

    assert sentinel.exists() and link.is_symlink(), _output(result)
    _assert_refused(harness, result)
    assert not (harness.tmp / "state").exists()


@pytest.mark.parametrize("layout", ["equal", "state-inside-workspace", "workspace-inside-state"])
def test_overlapping_roots_are_refused_before_anything_is_created(harness: Harness, layout: str):
    outer = harness.tmp / "outer"
    inner = outer / "inner"
    workspace, state = {
        "equal": (outer, outer),
        "state-inside-workspace": (outer, inner),
        "workspace-inside-state": (inner, outer),
    }[layout]

    result = harness.run(CORSIA_E2E_WORKSPACE=str(workspace), CORSIA_E2E_STATE_DIR=str(state))

    _assert_refused(harness, result)
    assert not outer.exists()


def test_a_root_the_backend_guard_refuses_is_never_created(harness: Harness):
    """No bootstrap config under the disposable HOME, so a HOME-contained workspace is refused."""
    workspace = harness.home / "workspace"
    state = harness.tmp / "state"

    result = harness.run(CORSIA_E2E_WORKSPACE=str(workspace), CORSIA_E2E_STATE_DIR=str(state))

    assert not workspace.exists(), _output(result)
    assert not state.exists()
    _assert_refused(harness, result)


def test_fresh_caller_roots_are_prepared(harness: Harness):
    workspace = harness.tmp / "fresh" / "workspace"
    state = harness.tmp / "fresh" / "state"
    state.mkdir(parents=True)

    result = harness.run(CORSIA_E2E_WORKSPACE=str(workspace), CORSIA_E2E_STATE_DIR=str(state))

    assert PREP_DONE in _output(result), _output(result)
    assert "refusing" not in _output(result)
    assert workspace.is_dir() and state.is_dir()
    # An accepted run owns the port: it sweeps before boot and again on exit.
    assert harness.port_calls().count("lsof -ti:3110") == 2
    assert harness.port_calls().count("kill 999999") == 2


def test_the_default_roots_are_recreated_empty(harness: Harness):
    stale = harness.default_root("e2e-workspace") / "stale.txt"
    stale.parent.mkdir(parents=True)
    stale.write_text("previous run", encoding="utf-8")

    result = harness.run()

    assert PREP_DONE in _output(result), _output(result)
    assert not stale.exists()
    assert harness.default_root("e2e-workspace").is_dir() and harness.default_root("e2e-state").is_dir()


@pytest.mark.parametrize("redirected", ["parent", "root"])
def test_a_redirected_default_root_never_deletes_the_tree_it_points_at(harness: Harness, redirected: str):
    foreign = harness.tmp / "foreign"
    if redirected == "parent":
        sentinel = foreign / "e2e-workspace" / "keep.txt"
        sentinel.parent.mkdir(parents=True)
        (harness.repo / "frontend" / ".qa").symlink_to(foreign)
    else:
        sentinel = foreign / "keep.txt"
        foreign.mkdir()
        (harness.repo / "frontend" / ".qa").mkdir()
        harness.default_root("e2e-workspace").symlink_to(foreign)
    sentinel.write_text("not disposable", encoding="utf-8")

    result = harness.run()

    assert sentinel.read_text(encoding="utf-8") == "not disposable", _output(result)
    _assert_refused(harness, result)


def test_a_checkout_whose_frontend_leads_elsewhere_writes_nothing_there(harness: Harness):
    """``.qa`` does not exist yet, so only the path above it shows the redirect."""
    foreign = harness.tmp / "foreign-frontend"
    foreign.mkdir()
    (harness.repo / "frontend").rmdir()
    (harness.repo / "frontend").symlink_to(foreign)

    result = harness.run()

    assert not (foreign / ".qa").exists(), _output(result)
    _assert_refused(harness, result)


def test_reuse_neither_deletes_nor_demands_fresh_roots(harness: Harness):
    workspace = harness.tmp / "reused" / "workspace"
    state = harness.tmp / "reused" / "state"
    workspace.mkdir(parents=True)
    state.mkdir()
    sentinel = state / "corsia.db"
    sentinel.write_text("existing run", encoding="utf-8")
    python = harness.shim("fake-python", "echo token-for-reuse")

    result = harness.run(
        "--reuse", CORSIA_E2E_PYTHON=str(python),
        CORSIA_E2E_WORKSPACE=str(workspace), CORSIA_E2E_STATE_DIR=str(state),
    )

    assert sentinel.read_text(encoding="utf-8") == "existing run"
    assert "refusing" not in _output(result), _output(result)
    assert "Playwright is not installed" in _output(result)
    assert harness.port_calls() == ""
