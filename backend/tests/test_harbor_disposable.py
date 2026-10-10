from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

# benchmarks/ sits at the repository root, outside backend/. The L3 job runs
# pytest from backend/, where the root is not on sys.path.
sys.path.append(str(Path(__file__).resolve().parents[2]))

from benchmarks.harbor.disposable import RESERVED_PORTS, DisposableCORSIA  # noqa: E402
from benchmarks.harbor.meter import JsonlMeter, export_meter_totals  # noqa: E402

WORKTREE = Path(__file__).resolve().parents[2]


def test_disposable_env_isolated_from_serving_instance(tmp_path, monkeypatch):
    monkeypatch.setenv("CORSIA_STATE_DIR", "/serving/state")
    monkeypatch.setenv("CORSIA_DB_PATH", "/serving/corsia.db")
    instance = DisposableCORSIA(
        worktree=WORKTREE, python=Path(sys.executable), tool_allowlist=["harbor_exec"],
        bridge_socket=tmp_path / "b.sock", root=tmp_path / "root",
    )

    env = instance.env()

    assert env["CORSIA_STATE_DIR"] == str(tmp_path / "root/state")
    assert "CORSIA_DB_PATH" not in env
    assert env["CORSIA_TEST_MODE"] == "1"
    assert json.loads(env["CORSIA_EVAL_TOOL_ALLOWLIST"]) == ["harbor_exec"]
    assert instance.port not in RESERVED_PORTS


def test_bootstrap_refuses_outside_test_mode():
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": f"{WORKTREE / 'backend'}:{WORKTREE}"}
    result = subprocess.run(
        [sys.executable, "-m", "benchmarks.harbor.bootstrap_instance", "--server-id", "ns-x", "--port", "1"],
        cwd=WORKTREE, env=env, capture_output=True, text=True,
    )

    assert result.returncode != 0
    assert "CORSIA_TEST_MODE=1" in result.stderr


def test_harbor_tokens_come_only_from_meter_receipt(tmp_path):
    receipt = tmp_path / "r.jsonl"
    meter = JsonlMeter(receipt, unit_id="u", call_cap=3)
    for usage in ({"input_tokens": 10, "output_tokens": 2}, {"prompt_tokens": 5, "completion_tokens": 1}):
        meter.record_response(meter.reserve_call(), {"usage": usage}, time.monotonic())
    context = SimpleNamespace(n_input_tokens=999, n_cache_tokens=None, n_output_tokens=999, metadata=None)

    export_meter_totals(context, receipt, "u")

    assert (context.n_input_tokens, context.n_cache_tokens, context.n_output_tokens) == (15, 0, 3)
    assert context.metadata["meter_totals"]["calls"] == 2
    assert context.metadata["median_seconds_per_call"] >= 0
