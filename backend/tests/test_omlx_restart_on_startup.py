"""CORSIA's managed oMLX is started again at service startup when its process is gone."""
from __future__ import annotations

import asyncio
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llm import omlx_startup
from llm import managed_omlx_binary as binary
from llm.providers import omlx_supervisor as sup

LOG = logging.getLogger("test")
LOG.info = lambda *_a, **_k: None  # type: ignore[method-assign]
MANAGED = {"id": "own", "provider": "omlx", "kind": "managed", "source": "corsia",
           "instance_id": binary.MANAGED_INSTANCE_ID, "status": "offline"}
PERSONS = {"id": "mine", "provider": "omlx", "kind": "local", "source": "manual",
           "instance_id": "mine", "status": "offline"}


@pytest.fixture
def state(tmp_path, monkeypatch):
    root = tmp_path / "CORSIA" / "state"
    (root / "runtime").mkdir(parents=True)
    import infra.config as cfg
    monkeypatch.setattr(cfg, "STATE_ROOT", root)
    monkeypatch.setattr(sup, "RUNTIME_DIR", root / "runtime")
    return root


def _model(root, name="org/model"):
    folder = root / binary.MANAGED_DIR / "models" / name
    folder.mkdir(parents=True)
    (folder / "config.json").write_text("{}")
    return folder


def test_gone_process_on_the_current_root_is_started(state):
    _model(state)
    assert omlx_startup.reconcile_omlx_servers([MANAGED], LOG) == ["own"]


def test_live_process_is_adopted_not_started(state, monkeypatch):
    _model(state)
    sup._write_pid("own", os.getpid())
    monkeypatch.setattr(sup, "_mark_endpoint_alive", lambda *_a: None)
    monkeypatch.setattr(sup, "_resolve_endpoint_via_freshness", lambda _c: ("127.0.0.1", 8210))
    monkeypatch.setattr(sup, "_port_owner_pid", lambda *_a: os.getpid())
    assert omlx_startup.reconcile_omlx_servers([MANAGED], LOG) == []


def test_a_server_the_person_stopped_stays_stopped(state):
    _model(state)
    sup.mark_person_stopped("own")
    assert omlx_startup.reconcile_omlx_servers([MANAGED], LOG) == []


def test_a_persons_own_omlx_is_never_started(state):
    _model(state)
    assert omlx_startup.reconcile_omlx_servers([PERSONS], LOG) == []


def test_no_model_on_disk_or_a_partial_download_is_not_started(state):
    assert omlx_startup.reconcile_omlx_servers([MANAGED], LOG) == []
    (_model(state) / "weights.safetensors.incomplete").write_text("x")
    assert omlx_startup.reconcile_omlx_servers([MANAGED], LOG) == []


def test_after_a_move_the_model_is_found_on_the_current_root_only(state, tmp_path):
    legacy = tmp_path / "com.cebertowicz.corsia" / "state"
    _model(legacy)  # the old folder is never consulted; the current root has no model yet
    assert omlx_startup.reconcile_omlx_servers([MANAGED], LOG) == []
    _model(state)
    assert omlx_startup.reconcile_omlx_servers([MANAGED], LOG) == ["own"]


def test_a_failed_start_is_only_logged(state, monkeypatch):
    calls = []

    async def fake_start(cfg):
        calls.append(cfg["server_id"])
        return {"ok": False, "message": "no binary"}

    monkeypatch.setattr(sup, "start_server", fake_start)
    asyncio.run(omlx_startup.start_managed_omlx(["own"], LOG))
    assert calls == ["own"]


def test_settings_stop_marks_the_person_stop_and_a_start_clears_it(state, monkeypatch):
    from api import network_server_lifecycle as route
    from llm import network_discovery as nd

    async def stopped(_cfg):
        return {"ok": True}

    monkeypatch.setattr(nd, "get_server", lambda _id: {"provider": "omlx"})
    monkeypatch.setattr(nd, "update_server_status", lambda *_a, **_k: None)
    monkeypatch.setattr(sup, "stop_server", stopped)
    asyncio.run(route.lifecycle_action("own", "stop"))
    assert sup.person_stopped("own")
    sup._person_stopped_path("own").unlink()
    assert not sup.person_stopped("own")
