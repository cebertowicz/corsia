"""Build the fixture and run the harness against one backend tree, each in a clean subprocess.

Every subprocess gets a disposable workspace and state root and test mode, so
nothing here can reach a real instance or a production database.
"""

from __future__ import annotations

import collections
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

class MeasurementError(RuntimeError):
    """The fixture build or a harness run failed; there is nothing to compare."""


#: What a measured process may find on PATH: the operating system's own tools (git, curl) and nothing a user installed.
SYSTEM_PATH = "/usr/bin:/bin"
PACKAGE_ROOT = Path(__file__).resolve().parent.parent  # the ``scripts`` directory holding this package


def _env(root: Path) -> dict[str, str]:
    workspace, state = root / "workspace", root / "state"
    workspace.mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)
    home = root / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.pop("CODEX_HOME", None)
    # An empty HOME and a system-only PATH: read routes look for the user's own tool configs and installed CLIs
    # (Claude Code, Codex, oMLX), and the model-source routes change shape with what they find, so the counts
    # must not depend on what this host has installed. The interpreter is run by absolute path.
    env.update({"CORSIA_WORKSPACE": str(workspace), "CORSIA_STATE_DIR": str(state), "CORSIA_TEST_MODE": "1",
                "CORSIA_ANALYTICS_DISABLE": "1", "PYTHONPATH": str(PACKAGE_ROOT), "HOME": str(home),
                "PATH": SYSTEM_PATH,
                # Measurement-only: one pooled connection per lane, so a call never depends on which one served it.
                "CORSIA_DB_POOL_WORKERS": "1"})
    return env


#: A fixture build or harness run takes well under a minute; one that takes this long is stuck, and the job
#: should say where instead of being cancelled by the workflow timeout with no output.
STAGE_TIMEOUT_SECONDS = 300


def _run(module: str, args: list[str], root: Path, timeout: float = STAGE_TIMEOUT_SECONDS) -> None:
    """Run one stage, streaming its output (stderr included) to ours so a stuck stage shows where it stopped."""
    started = time.monotonic()
    print(f"[speed-gate] {module} started", file=sys.stderr, flush=True)
    child = subprocess.Popen([sys.executable, "-m", module, *args], env=_env(root), stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True)
    timer = threading.Timer(timeout, child.kill)
    timer.start()
    tail: collections.deque[str] = collections.deque(maxlen=25)  # the last lines, for the failure report
    try:
        for line in child.stdout:
            tail.append(line.rstrip())
            if line.startswith("route "):  # progress only; the rest is kept for a failure report
                print(f"[speed-gate] {module}: {line.rstrip()}", file=sys.stderr, flush=True)
        code = child.wait()
    finally:
        timed_out = not timer.is_alive()
        timer.cancel()
    seconds = time.monotonic() - started
    if timed_out and code != 0:
        raise MeasurementError(f"{module} was stopped after {timeout:.0f}s without finishing; last output:\n" + "\n".join(tail))
    print(f"[speed-gate] {module} finished in {seconds:.0f}s", file=sys.stderr, flush=True)
    if code != 0:
        raise MeasurementError(f"{module} failed with exit {code}:\n" + "\n".join(tail))


def build_fixture(backend: Path, root: Path) -> tuple[Path, dict]:
    """Build the synthetic database with ``backend``'s own schema; return its path and manifest."""
    manifest_file = root / "fixture-manifest.json"
    _run("speed_gate.fixture_build", ["--backend", str(backend), "--manifest", str(manifest_file)], root / "build")
    manifest = json.loads(manifest_file.read_text())
    return Path(manifest["db_path"]), manifest


def measure(backend: Path, fixture_db: Path, root: Path, label: str) -> dict:
    """One harness run of ``backend`` against ``fixture_db``."""
    out = root / f"measure-{label}.json"
    _run("speed_gate.harness", ["--backend", str(backend), "--db", str(fixture_db), "--out", str(out)],
         root / f"run-{label}")
    return json.loads(out.read_text())


def build_and_measure(backend: Path, repeats: int = 1) -> tuple[list[dict], dict, dict]:
    """Fixture build plus ``repeats`` independent harness runs.

    Returns the runs, the fixture manifest and informational timings (never a verdict input).
    """
    backend = backend.resolve()
    with tempfile.TemporaryDirectory(prefix="speed-gate-") as scratch:
        root = Path(scratch)
        started = time.monotonic()
        fixture_db, manifest = build_fixture(backend, root)
        built = time.monotonic()
        runs = [measure(backend, fixture_db, root, str(index)) for index in range(repeats)]
        finished = time.monotonic()
    timings = {"fixture_build_seconds": round(built - started, 1),
               "harness_seconds": round((finished - built) / max(repeats, 1), 1)}
    return runs, manifest, timings
