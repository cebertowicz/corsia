"""Test-app-only coordination for an outside write during shared publication."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
from threading import Event, Lock
from unittest.mock import patch

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from doceditor import live_runtime
from infra import config
from infra.guidance_targets import HistoryTarget
from registry import durable_writes


class Arm(BaseModel):
    path: str


class Gate:
    def __init__(self) -> None:
        self.arrived = Event()
        self.release = Event()


_gates: dict[Path, Gate] = {}
_lock = Lock()
router = APIRouter(prefix="/api/test/shared-save-race")


def _target(value: str) -> Path:
    if any(os.environ.get(name) != "1" for name in ("CORSIA_TEST_MODE", "CORSIA_E2E_MOCK")) or not os.environ.get("CORSIA_WORKSPACE") or not os.environ.get("CORSIA_STATE_DIR"):
        raise HTTPException(403, "This control requires isolated mock E2E roots")
    path = Path(value).resolve()
    shared = (Path(config.WORKSPACE) / "shared").resolve()
    if not path.is_relative_to(shared) or not path.is_file():
        raise HTTPException(400, "An existing file in the test workspace's shared directory is required")
    return path


@router.post("/arm")
def arm(body: Arm) -> dict:
    path = _target(body.path)
    with _lock:
        if path in _gates:
            raise HTTPException(409, "A gate is already armed for this file")
        _gates[path] = Gate()
    return {"armed": True}


@router.get("/status")
def status(path: str) -> dict:
    target = _target(path)
    with _lock:
        gate = _gates.get(target)
    return {"arrived": bool(gate and gate.arrived.is_set())}


@router.post("/release")
def release(body: Arm) -> dict:
    target = _target(body.path)
    with _lock:
        gate = _gates.pop(target, None)
    if gate is None:
        raise HTTPException(404, "No gate is armed for this file")
    gate.release.set()
    return {"released": True}


@contextmanager
def install_gate():
    """Pause once after staging, before the real publisher replaces the file."""
    original = durable_writes.stage_bound_bytes
    original_guidance = live_runtime.guidance_target

    def non_live(path, workspace):
        # Exercise the real legacy writer for this file while its test gate is armed.
        with _lock:
            armed = Path(path).resolve() in _gates
        return HistoryTarget("test") if armed else original_guidance(path, workspace)

    def stage(target, *args, **kwargs):
        result = original(target, *args, **kwargs)
        with _lock:
            gate = _gates.get(Path(target).resolve())
        if gate is not None:
            gate.arrived.set()
            if not gate.release.wait(30):
                with _lock:
                    _gates.pop(Path(target).resolve(), None)
                raise TimeoutError("The test did not release the shared-save gate")
        return result

    with patch.object(durable_writes, "stage_bound_bytes", stage), patch.object(live_runtime, "guidance_target", non_live):
        try:
            yield
        finally:
            with _lock:
                gates = list(_gates.values())
                _gates.clear()
            for gate in gates:
                gate.release.set()
