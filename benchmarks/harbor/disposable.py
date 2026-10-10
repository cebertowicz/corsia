"""Lifecycle of one disposable CORSIA worktree instance for a Harbor unit.

The instance gets a fresh workspace, state root (and so a fresh database), a
loopback port other than 1956/3001, test mode, the eval tool allowlist and the
bridge socket. Its only model route is the metering proxy.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

RESERVED_PORTS = {1956, 3001}


def free_port() -> int:
    """Pick a free loopback port that is never a serving CORSIA port."""
    while True:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        if port not in RESERVED_PORTS:
            return port


@dataclass
class DisposableCORSIA:
    """One throwaway backend process; nothing it writes outlives ``root``."""

    worktree: Path
    python: Path
    tool_allowlist: list[str]
    bridge_socket: Path
    root: Path = field(default_factory=lambda: Path(tempfile.mkdtemp(prefix="hb-corsia-")))
    port: int = field(default_factory=free_port)
    _process: subprocess.Popen | None = None
    _token: str | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def db_path(self) -> Path:
        return self.root / "state" / "corsia.db"

    def env(self) -> dict[str, str]:
        """Environment for the instance and its bootstrap; never inherits a serving target."""
        base = {k: v for k, v in os.environ.items() if not k.startswith("CORSIA_")}
        return {
            **base,
            "CORSIA_WORKSPACE": str(self.root / "workspace"),
            "CORSIA_STATE_DIR": str(self.root / "state"),
            "CORSIA_TEST_MODE": "1",
            "CORSIA_ANALYTICS_DISABLE": "1",
            "CORSIA_EVAL_TOOL_ALLOWLIST": json.dumps(self.tool_allowlist),
            "CORSIA_HARBOR_BRIDGE_SOCKET": str(self.bridge_socket),
            "PYTHONPATH": f"{self.worktree / 'backend'}{os.pathsep}{self.worktree}",
            "XDG_CACHE_HOME": str(self.root / "cache"),
        }

    async def start(self, *, ready_timeout: float = 120.0) -> None:
        """Start uvicorn on the private port and wait for health."""
        for sub in ("workspace", "state", "cache"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        log = (self.root / "backend.log").open("ab")
        self._process = subprocess.Popen(
            [str(self.python), "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=self.worktree / "backend", env=self.env(), stdout=log, stderr=subprocess.STDOUT,
        )
        await self._wait_for(lambda body: True, "/api/health", ready_timeout)

    def bootstrap(self, *, server_id: str, proxy_port: int) -> None:
        """Register the proxy as the model server and mint a disposable token."""
        result = subprocess.run(
            [str(self.python), "-m", "benchmarks.harbor.bootstrap_instance",
             "--server-id", server_id, "--port", str(proxy_port)],
            cwd=self.worktree, env=self.env(), capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"disposable bootstrap failed: {result.stderr[-2000:]}")
        self._token = json.loads(result.stdout.strip().splitlines()[-1])["token"]

    async def wait_model_ready(self, selector: str, timeout: float = 180.0) -> None:
        """Wait for the instance's own catalog to report the exact selector ready."""
        def ready(body: Any) -> bool:
            rows = body.get("models", []) if isinstance(body, dict) else []
            return any(r.get("litellm_model") == selector and r.get("status") == "ready" for r in rows)
        await self._wait_for(ready, "/api/models", timeout)

    async def eval_turn(self, instruction: str, *, model: str, timeout_sec: float) -> dict[str, Any]:
        """Run one fresh-context turn through the existing eval endpoint."""
        async with httpx.AsyncClient(timeout=timeout_sec + 60) as client:
            response = await client.post(
                f"{self.base_url}/api/eval/turn",
                params={"token": self._token},
                json={"input": instruction, "metadata": {"model": model, "timeout_seconds": timeout_sec}},
            )
        response.raise_for_status()
        return response.json()

    def ledger(self) -> dict[str, Any]:
        """Read the instance's own ``llm_usage`` rows and their context manifests."""
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            usage = [dict(r) for r in conn.execute(
                "SELECT id, thread_id, turn_id, model, prompt_tokens, completion_tokens, "
                "cache_creation_tokens, cache_read_tokens, total_tokens, source, created_at, "
                "context_manifest_id FROM llm_usage ORDER BY created_at"
            )]
            ids = sorted({r["context_manifest_id"] for r in usage if r["context_manifest_id"]})
            manifests = [dict(r) for r in conn.execute(
                f"SELECT id, harness, selector, resolved_model, entries_json, entry_count FROM context_manifests "
                f"WHERE id IN ({','.join('?' * len(ids))})", ids,
            )] if ids else []
        finally:
            conn.close()
        for manifest in manifests:
            manifest["entries"] = json.loads(manifest.pop("entries_json"))
        return {"llm_usage": usage, "context_manifests": manifests}

    async def stop(self) -> None:
        """Terminate the process; the caller keeps ``root`` for receipt export."""
        if self._process is None:
            return
        self._process.terminate()
        try:
            await asyncio.to_thread(self._process.wait, 20)
        except subprocess.TimeoutExpired:
            self._process.kill()
        self._process = None

    async def _wait_for(self, check, path: str, timeout: float) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        last = "no response"
        async with httpx.AsyncClient(timeout=5) as client:
            while asyncio.get_running_loop().time() < deadline:
                if self._process is not None and self._process.poll() is not None:
                    raise RuntimeError(f"disposable backend exited {self._process.returncode}")
                try:
                    params = {"token": self._token} if self._token else None
                    response = await client.get(f"{self.base_url}{path}", params=params)
                    if response.status_code == 200 and check(response.json()):
                        return
                    last = f"HTTP {response.status_code}"
                except (httpx.HTTPError, ValueError) as exc:
                    last = type(exc).__name__
                await asyncio.sleep(1)
        raise TimeoutError(f"{path} not ready after {timeout}s ({last})")


def default_python(worktree: Path) -> Path:
    """The worktree's backend interpreter (Python 3.11 venv)."""
    candidate = worktree / ".venv" / "bin" / "python"
    return candidate if candidate.exists() else Path(sys.executable)
