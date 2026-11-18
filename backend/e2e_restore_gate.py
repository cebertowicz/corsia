"""Disposable mock-E2E gate for the deleted restore control."""
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

router = APIRouter(prefix='/api/test/shared-save-race')
_lock = Lock()
_gates = {}

class Arm(BaseModel):
    path: str

def target(value, require_file=True):
    if any(os.environ.get(name) != '1' for name in ('CORSIA_TEST_MODE', 'CORSIA_E2E_MOCK')) or not os.environ.get('CORSIA_WORKSPACE') or not os.environ.get('CORSIA_STATE_DIR'):
        raise HTTPException(403)
    path = Path(value).resolve()
    if not path.is_relative_to((Path(config.WORKSPACE) / 'shared').resolve()) or (require_file and not path.is_file()):
        raise HTTPException(400)
    return path

@router.post('/arm')
def arm(body: Arm):
    path = target(body.path)
    with _lock:
        _gates[path] = (Event(), Event())
    return {'armed': True}

@router.get('/status')
def status(path: str):
    path = target(path)
    with _lock:
        gate = _gates.get(path)
    return {'arrived': bool(gate and gate[0].is_set())}

@router.post('/release')
def release(body: Arm):
    path = target(body.path, False)
    with _lock:
        gate = _gates.pop(path, None)
    if gate is None:
        raise HTTPException(404)
    gate[1].set()
    return {'released': True}

@contextmanager
def install_gate():
    original = durable_writes.stage_bound_bytes
    original_guidance = live_runtime.guidance_target
    def non_live(path, workspace):
        with _lock:
            armed = Path(path).resolve() in _gates
        return HistoryTarget('test') if armed else original_guidance(path, workspace)
    def stage(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        with _lock:
            gate = _gates.get(Path(path).resolve())
        if gate:
            gate[0].set()
            if not gate[1].wait(30):
                raise TimeoutError('restore proof gate timed out')
        return result
    with patch.object(durable_writes, 'stage_bound_bytes', stage), patch.object(live_runtime, 'guidance_target', non_live):
        try:
            yield
        finally:
            with _lock:
                gates = list(_gates.values())
                _gates.clear()
            for gate in gates:
                gate[1].set()
