import asyncio
import json
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from e2e_live_faults import FaultConfiguration, FaultConfigurationError, FaultController, _active_artifact, build_router


def fault_environment(tmp_path: Path) -> dict[str, str]:
    state = tmp_path / "state"
    run = state / "live-fault-run"
    run.mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (run / "identity.json").write_text(json.dumps({"run_id": "run-1", "workspace": str(workspace), "state_dir": str(state)}))
    (run / "restart.json").write_text(json.dumps({"case_id": None, "restart_count": 0}))
    return {
        "CORSIA_E2E_LIVE_FAULTS": "1",
        "CORSIA_TEST_MODE": "1",
        "CORSIA_E2E_MOCK": "1",
        "CORSIA_E2E_FRESH_ROOTS": "1",
        "CORSIA_WORKSPACE": str(workspace),
        "CORSIA_STATE_DIR": str(state),
        "CORSIA_E2E_LIVE_FAULT_RUN_DIR": str(run),
        "CORSIA_E2E_LIVE_FAULT_RUN_ID": "run-1",
    }


def test_live_fault_configuration_requires_every_test_gate(tmp_path):
    environment = fault_environment(tmp_path)
    del environment["CORSIA_E2E_FRESH_ROOTS"]
    with pytest.raises(FaultConfigurationError, match="CORSIA_E2E_FRESH_ROOTS=1"):
        FaultConfiguration.from_environ(environment)


def test_live_fault_configuration_rejects_control_directory_outside_state(tmp_path):
    environment = fault_environment(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    (other / "identity.json").write_text(json.dumps({"run_id": "run-1", "workspace": environment["CORSIA_WORKSPACE"], "state_dir": environment["CORSIA_STATE_DIR"]}))
    environment["CORSIA_E2E_LIVE_FAULT_RUN_DIR"] = str(other)
    with pytest.raises(FaultConfigurationError, match="inside CORSIA_STATE_DIR"):
        FaultConfiguration.from_environ(environment)


def test_live_fault_control_arms_one_known_cut_and_kills_only_matching_checkpoint(tmp_path, monkeypatch):
    controller = FaultController(FaultConfiguration.from_environ(fault_environment(tmp_path)))
    monkeypatch.setattr(controller, "_bind", lambda artifact_id: object())
    with pytest.raises(HTTPException, match="Begin a fresh"):
        controller.arm("artifact-1", "choice_rename")
    case = controller.begin_case()
    armed = controller.arm("artifact-1", "choice_rename")
    assert armed == {"run_id": "run-1", "pid": controller.pid, "case_id": case["case_id"], "artifact_id": "artifact-1", "stage": "choice_rename", "generation": 1}
    assert controller.status() == {"run_id": "run-1", "pid": controller.pid, "armed": True, "case_id": case["case_id"], "restart_count": 0}
    killed = []
    monkeypatch.setattr("e2e_live_faults.os.kill", lambda pid, signal: killed.append((pid, signal)))
    token = _active_artifact.set("artifact-1")
    try:
        asyncio.run(controller.checkpoint("choice_record"))
        assert killed == []
        asyncio.run(controller.checkpoint("choice_rename"))
    finally:
        _active_artifact.reset(token)
    assert killed == [(controller.pid, 9)]
    with pytest.raises(Exception, match="already armed"):
        controller.arm("artifact-1", "choice_apply")


def test_live_fault_router_is_absent_without_opt_in_and_forbidden_when_the_gate_is_incomplete(tmp_path):
    controller = FaultController(FaultConfiguration.from_environ(fault_environment(tmp_path)))
    assert build_router(None, opt_in_requested=False) is None
    router = build_router(None, opt_in_requested=True)
    app = FastAPI()
    app.include_router(router)
    assert TestClient(app).get("/api/test/live-faults/status").status_code == 403
    assert {route.path for route in build_router(controller, opt_in_requested=True).routes} == {
        "/api/test/live-faults/status", "/api/test/live-faults/case", "/api/test/live-faults/{artifact_id}/prepare-held",
        "/api/test/live-faults/{artifact_id}/refuse/{kind}", "/api/test/live-faults/{artifact_id}/arm/{stage}",
        "/api/test/live-faults/{artifact_id}/evidence",
    }
