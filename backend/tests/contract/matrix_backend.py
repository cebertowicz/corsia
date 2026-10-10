"""Isolated process owner for the real HTTP/WebSocket contract matrix."""

from __future__ import annotations

import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

from tests.contract.matrix_environment import guarded_command, isolated_environment
from tests.contract.matrix_harness_fixture import install_fixture_binaries

BACKEND_ROOT = Path(__file__).resolve().parents[2]


class BackendUnavailable(RuntimeError):
    """The isolated backend could not be booted or reached in this environment."""


class IsolatedBackend:
    """A uvicorn ``e2e_app`` process on scratch workspace/state roots."""

    def __init__(self, root: Path):
        self.root = root
        self.workspace = root / "workspace"
        self.state = root / "state"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.state.mkdir(parents=True, exist_ok=True)
        self.fixture_binaries = install_fixture_binaries(root)
        self.log_path = root / "backend.log"
        self.port = 0
        self.process: subprocess.Popen | None = None

    @property
    def base_url(self) -> str:
        """HTTP origin of the running process (valid only after ``boot``)."""
        return f"http://127.0.0.1:{self.port}"

    @property
    def ws_url(self) -> str:
        """Chat WebSocket URL of the running process."""
        return f"ws://127.0.0.1:{self.port}/ws"

    def _env(self) -> dict[str, str]:
        """Child env: scratch roots, test mode, mocked LLM — never prod state."""
        return isolated_environment(self.root, self.workspace, self.state) | {
            "BACKEND_PORT": str(self.port),
            "CORSIA_E2E_CC_BINARY": self.fixture_binaries["claude-code"],
            "CORSIA_E2E_CODEX_BINARY": self.fixture_binaries["codex"],
        }

    def command(self, arguments: list[str]) -> list[str]:
        """Launch app and seeding children under the same fixture-only network boundary."""
        return guarded_command(arguments, self.port)

    def boot(self, timeout: float = 90.0) -> None:
        """Start uvicorn on a fresh port and block until ``/api/health`` answers."""
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = int(probe.getsockname()[1])
        command = self.command([
            sys.executable, "-m", "uvicorn", "e2e_app:app",
            "--host", "127.0.0.1", "--port", str(self.port),
        ])
        with self.log_path.open("ab") as handle:
            self.process = subprocess.Popen(
                command, cwd=str(BACKEND_ROOT), env=self._env(),
                stdout=handle, stderr=subprocess.STDOUT,
            )
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.process.poll() is not None:
                raise BackendUnavailable(
                    f"isolated backend exited rc={self.process.returncode}\n"
                    f"{self.log_tail()}")
            try:
                if httpx.get(f"{self.base_url}/api/health", timeout=2, trust_env=False).status_code == 200:
                    self._seed_accounts()
                    return
            except httpx.HTTPError:
                pass  # not listening yet; the loop's deadline is the real check
            time.sleep(0.5)
        self.stop(hard=True)
        raise BackendUnavailable(
            f"isolated backend silent on /api/health for {timeout:.0f}s\n"
            f"{self.log_tail()}")

    def _seed_accounts(self) -> None:
        """Give this process's database the accounts the matrix signs in as.

        A token authenticates only if it is a row in the serving process's own
        store, so the seed runs in a child against these scratch roots, after
        startup has created the schema. It is idempotent, so a crash-restart
        over the same state reseeds harmlessly.

        A failure here raises ``RuntimeError``, never ``BackendUnavailable``:
        the matrix test skips on the latter, and a missing account would then
        hide every cell as "cannot boot here" instead of failing.
        """
        completed = subprocess.run(
            self.command([
                sys.executable, "-c",
                "import sys; sys.path.insert(0, 'tests'); "
                "from auth_fixtures import seed_test_accounts; "
                "seed_test_accounts()",
            ]),
            cwd=str(BACKEND_ROOT), env=self._env(), check=False,
            capture_output=True, text=True, timeout=30,
        )
        if completed.returncode != 0:
            self.stop(hard=True)
            raise RuntimeError(
                "could not seed the matrix accounts\n"
                f"{completed.stdout}{completed.stderr}"
            )

    def log_tail(self, lines: int = 25) -> str:
        """Last lines of the backend's own log — the evidence for a failed cell."""
        if not self.log_path.exists():
            return "(no log)"
        return "\n".join(self.log_path.read_text(errors="replace").splitlines()[-lines:])

    def stop(self, *, hard: bool = False) -> None:
        """End the process. ``hard`` is SIGKILL — a real crash, no shutdown hook."""
        if self.process is None or self.process.poll() is not None:
            return
        self.process.send_signal(signal.SIGKILL if hard else signal.SIGTERM)
        try:
            self.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.process.send_signal(signal.SIGKILL)
            self.process.wait(timeout=30)

    def restart_after_crash(self, timeout: float = 90.0) -> None:
        """Crash the process and boot a new one over the same durable state."""
        self.stop(hard=True)
        self.boot(timeout=timeout)
