"""Disposable HTTP preview of a Codex provider resume: real route, real receipt.

Only identity/catalog selection is a fixture. Source collection, the registry
row, the runtime-truth read, the identity-matched retained receipt and the
estimate are the production paths; nothing in the capacity read is patched.
"""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.project_ref_helpers import select_workspace
from test_context_preview_contracts import _target

THREAD = "resume-owner"
CODEX_ID = "11111111-2222-4333-8444-555555555555"
RETAINED = 41_000


def _seed_terminal_receipt(db, handle_fingerprint: str, *, total: int) -> None:
    db.execute(
        "INSERT INTO active_turns(id,thread_id,execution_epoch,model,status,started_at,"
        "boot_epoch) VALUES ('turn-1',?,0,'codex/test','completed','t','b')",
        (THREAD,),
    )
    usage = {
        "token_usage_available": True, "token_source": "measured", "harness": "codex",
        "catalog_model_id": "codex/test", "runtime_context_epoch": 0,
        "producer_handle_fingerprint": handle_fingerprint,
        "total_used": total, "model_context_window": 258_000,
    }
    db.execute(
        "INSERT INTO messages(id,thread_id,role,content,created_at,seq,active_turn_id,metadata) "
        "VALUES ('msg-1',?,'assistant','done','t',1,'turn-1',?)",
        (THREAD, json.dumps({"turn_id": "turn-1", "context_usage": usage})),
    )


def test_http_resume_preview_counts_the_retained_receipt_once(isolated_db, tmp_path, monkeypatch):
    from api import thread_runtime
    from core import context_preview
    from infra.context_receipts import producer_handle_fingerprint
    from registry.service import register_codex_thread
    from subagents import codex_runtime_state as state
    import llm.resolution

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    select_workspace(monkeypatch, workspace)
    monkeypatch.setattr(context_preview, "WORKSPACE", workspace)
    db = sqlite3.connect(isolated_db)
    db.row_factory = sqlite3.Row
    db.execute(
        f"INSERT INTO threads(id,user_id,model,created_at,updated_at) "
        f"VALUES ('{THREAD}','user','codex/test','t','t')"
    )
    db.execute(
        "INSERT INTO models(id,display_name,litellm_model,provider,provider_model_id,harness,"
        "created_at,updated_at) VALUES ('codex/test','Test','codex/test','codex','test','codex','t','t')"
    )
    register_codex_thread(THREAD, CODEX_ID, db=db)
    session = SimpleNamespace(
        returncode=None, codex_thread_id=CODEX_ID, catalog_model_id="codex/test",
        runtime_context_epoch=0, model="test", working_dir=str(workspace),
        context_snapshot_id="snap",
    )
    monkeypatch.setitem(state._APP_SESSIONS, THREAD, session)
    monkeypatch.setitem(state._CODEX_THREADS, THREAD, CODEX_ID)
    handle = state.trusted_handle_fingerprint(THREAD)
    assert handle is not None
    _seed_terminal_receipt(db, producer_handle_fingerprint(handle), total=RETAINED)
    db.commit()

    target = _target(harness="codex", model="codex/test")
    monkeypatch.setattr(thread_runtime, "authenticate", lambda _: {"id": "user"})
    monkeypatch.setattr(thread_runtime, "resolve_execution_target", lambda *_a, **_k: target)
    monkeypatch.setattr(context_preview, "resolve_execution_target", lambda *_a, **_k: target)
    monkeypatch.setattr(llm.resolution, "resolve_execution_target", lambda *_a, **_k: target)
    app = FastAPI()
    app.include_router(thread_runtime.router)
    with TestClient(app) as client:
        response = client.get(f"/api/threads/{THREAD}/context-snapshot/next-turn")
    db.close()

    assert response.status_code == 200, response.text
    estimate = response.json()["preview"]["preflight"]
    assert estimate["continuity_mode"] == "provider_resume"
    assert estimate["checkpoint_tokens"] == 0
    tail = estimate["total_tokens"] - estimate["current_user_tokens"] - RETAINED
    assert 0 <= tail < 200  # retained + one small terminal tail, never retained + sources
    assert estimate["history_tokens"] == max(0, RETAINED + tail - estimate["source_tokens"])
