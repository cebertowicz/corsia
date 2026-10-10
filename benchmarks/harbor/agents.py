"""Harbor external agents for the local CORSIA vs vanilla Codex validation.

Both arms send every model call through a per-unit ``MeteringProxy``; Harbor's
token fields are filled only from that proxy's raw receipts, never from
CORSIA's ``llm_usage`` ledger (contract v2, "Harbor token meter").

Run with the worktree root on ``PYTHONPATH`` and, for example::

    harbor run --agent-import-path benchmarks.harbor.agents:CORSIAHarborAgent ...
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from harbor.agents.base import BaseAgent
from harbor.agents.installed.codex import Codex, CodexOptions
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from benchmarks.harbor.bridge import HarborBridge
from benchmarks.harbor.disposable import DisposableCORSIA, default_python
from benchmarks.harbor.meter import JsonlMeter, export_meter_totals
from benchmarks.harbor.proxy import MeteringProxy

DEFAULT_UPSTREAM = "http://127.0.0.1:5200"
DEFAULT_SELECTOR = "omlx@ns-f378151a51/Qwen3.8-Flash-Next-MLX-oQ6-MTP"
COORDINATION_TOOLS = ["plan_ensure", "plan_read", "plan_update", "continuity_edit"]
BRIDGE_TOOLS = [f"harbor_{op}" for op in ("exec", "read", "write", "upload", "download")]
RECEIPT_NAME = "meter-receipt.jsonl"
CODEX_VERSION_PIN = "0.158.0"
CODEX_VERSION_RECEIPT = "codex-version.json"


class CORSIAHarborAgent(BaseAgent):
    """Drive one fresh thread on a disposable CORSIA instance against the task container."""

    def __init__(
        self,
        logs_dir: Path,
        model_name: str | None = None,
        *args: Any,
        corsia_worktree: str | None = None,
        selector: str = DEFAULT_SELECTOR,
        upstream: str = DEFAULT_UPSTREAM,
        call_cap: int = 200,
        turn_timeout_sec: float = 1200.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(logs_dir, model_name, *args, **kwargs)
        self._worktree = Path(corsia_worktree or Path(__file__).resolve().parents[2])
        self._selector = selector
        self._upstream = upstream
        self._call_cap = int(call_cap)
        self._turn_timeout = float(turn_timeout_sec)

    @staticmethod
    def name() -> str:
        return "corsia-harbor"

    def version(self) -> str:
        return "0.1.0"

    async def setup(self, environment: BaseEnvironment) -> None:
        """Nothing is installed in the container; the solver stays on the host."""

    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        unit_id = self.session_id or "corsia-unit"
        receipt = self.logs_dir / RECEIPT_NAME
        meter = JsonlMeter(receipt, unit_id=unit_id, call_cap=self._call_cap)
        proxy = MeteringProxy(meter, self._upstream)
        # Unix socket paths are capped near 104 bytes; keep the socket under /tmp.
        socket_path = Path(tempfile.mkdtemp(prefix="hb", dir="/tmp")) / "b.sock"
        bridge = HarborBridge(
            environment, socket_path, self.logs_dir / "bridge-trace.jsonl",
            staging_dir=self.logs_dir / "bridge-staging",
        )
        instance = DisposableCORSIA(
            worktree=self._worktree,
            python=default_python(self._worktree),
            tool_allowlist=BRIDGE_TOOLS + COORDINATION_TOOLS,
            bridge_socket=socket_path,
        )
        result: dict[str, Any] = {}
        try:
            await proxy.start()
            await bridge.start()
            await instance.start()
            instance.bootstrap(server_id=self._selector.split("@", 1)[1].split("/", 1)[0], proxy_port=proxy.port)
            await instance.wait_model_ready(self._selector)
            result = await instance.eval_turn(instruction, model=self._selector, timeout_sec=self._turn_timeout)
        finally:
            await instance.stop()
            await bridge.stop()
            await proxy.stop()
            self._write_receipts(instance, result, unit_id)
            export_meter_totals(context, receipt, unit_id)

    def _write_receipts(self, instance: DisposableCORSIA, result: dict[str, Any], unit_id: str) -> None:
        """Keep the ledger and effective-context receipt beside the meter receipt."""
        (self.logs_dir / "eval-turn.json").write_text(json.dumps({
            key: result.get(key) for key in ("content", "tool_calls", "usage", "model", "ms_total", "error")
        }, indent=2, default=str))
        ledger = instance.ledger() if instance.db_path.exists() else {"llm_usage": [], "context_manifests": []}
        ledger["instance_root"] = str(instance.root)
        ledger["unit_id"] = unit_id
        (self.logs_dir / "disposable-ledger.json").write_text(json.dumps(ledger, indent=2, default=str))


class MeteredCodexOptions(CodexOptions):
    """Harbor validates ``--ak`` against ``options_model``; declare the meter's kwargs.

    Plain fields carry no ``Cli(...)`` annotation, so none is rendered as a Codex flag.
    """

    upstream: str = DEFAULT_UPSTREAM
    call_cap: int = 200
    proxy_host: str = "host.docker.internal"
    proxy_port: int = 18555
    proxy_bind: str = "127.0.0.1"


class MeteredCodexAgent(Codex):
    """Harbor's built-in Codex agent with its OpenAI traffic routed through the meter.

    ``proxy_host`` is the host address the task container uses to reach the
    host loopback (for Colima, ``host.docker.internal``); the proxy itself
    binds only ``proxy_bind`` (loopback by default).
    """

    options_model = MeteredCodexOptions

    def __init__(
        self,
        logs_dir: Path,
        *args: Any,
        upstream: str = DEFAULT_UPSTREAM,
        call_cap: int = 200,
        proxy_host: str = "host.docker.internal",
        proxy_port: int = 18555,
        proxy_bind: str = "127.0.0.1",
        version: str | None = CODEX_VERSION_PIN,
        **kwargs: Any,
    ) -> None:
        extra_env = dict(kwargs.pop("extra_env", None) or {})
        extra_env["OPENAI_BASE_URL"] = f"http://{proxy_host}:{int(proxy_port)}/v1"
        extra_env.setdefault("OPENAI_API_KEY", "harbor-local")
        super().__init__(logs_dir, *args, extra_env=extra_env, version=version, **kwargs)
        self._upstream = upstream
        self._call_cap = int(call_cap)
        self._proxy_port = int(proxy_port)
        self._proxy_bind = proxy_bind

    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        unit_id = self.session_id or "codex-unit"
        receipt = self.logs_dir / RECEIPT_NAME
        meter = JsonlMeter(receipt, unit_id=unit_id, call_cap=self._call_cap)
        proxy = MeteringProxy(meter, self._upstream, host=self._proxy_bind, port=self._proxy_port)
        await proxy.start()
        try:
            await self._record_installed_version(environment)
            await super().run(instruction, environment, context)
        finally:
            await proxy.stop()

    async def _record_installed_version(self, environment: BaseEnvironment) -> None:
        """Write the pinned and the actually installed Codex version beside the meter receipt."""
        installed = None
        result = await environment.exec(command=self.get_version_command())
        if result.return_code == 0 and result.stdout:
            installed = self.parse_version(result.stdout)
        (self.logs_dir / CODEX_VERSION_RECEIPT).write_text(json.dumps({
            "pinned": self.version(), "installed": installed,
        }, indent=2))

    def populate_context_post_run(self, context: AgentContext) -> None:
        super().populate_context_post_run(context)
        export_meter_totals(context, self.logs_dir / RECEIPT_NAME, self.session_id or "codex-unit")
