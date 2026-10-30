"""Disposable HTTP preview: real source scan, database, builder and JSON export."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import statistics
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.project_ref_helpers import select_workspace
from test_context_preview_contracts import _target
from test_lesson_index import LESSON, _activate


def test_http_preview_keeps_lesson_projection_on_a_real_scanned_source(isolated_db, tmp_path, monkeypatch):
    from api import thread_runtime
    from core import context_preview, lesson_files
    from core.lesson_delivery import draw, start_trials
    import llm.resolution

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    select_workspace(monkeypatch, workspace)
    monkeypatch.setattr(context_preview, "WORKSPACE", workspace)
    db = sqlite3.connect(isolated_db)
    db.row_factory = sqlite3.Row
    db.execute("INSERT INTO threads(id,user_id,model,created_at,updated_at) VALUES('owner','user','claude-code/opus','t','t')")
    db.execute(
        """INSERT INTO artifact_identities(id,integration,type,external_id,title,state,created_at,updated_at)
           VALUES('artifact','workspace','skill','workspace://artifact','Lesson','active','t','t')""",
    )
    path, _review = _activate(db, workspace)
    db.commit()
    trial = lesson_files.read_trial(path.read_text(encoding="utf-8"))
    node = next(f"task-{i}" for i in range(1000)
                if draw(LESSON, trial.behavioral_sha256, f"task-{i}") == "with")
    [offer] = start_trials(db, thread_id="owner", node_id=node)
    receipt = db.execute("SELECT * FROM guidance_loads WHERE node_id=?", (node,)).fetchone()
    assert offer["lesson_id"] == receipt["lesson_id"] == LESSON
    assert receipt["path"] == str(path)
    assert receipt["lesson_behavioral_sha256"] == trial.behavioral_sha256
    assert receipt["delivered_sha256"] == hashlib.sha256(offer["guidance"].encode()).hexdigest()
    target = _target(harness="cc", model="claude-code/opus")
    # Only identity/catalog selection is a deterministic fixture. No preview,
    # source scan, snapshot, usage projection or JSON response is substituted.
    monkeypatch.setattr(thread_runtime, "authenticate", lambda _: {"id": "user"})
    monkeypatch.setattr(thread_runtime, "resolve_execution_target", lambda *_a, **_k: target)
    monkeypatch.setattr(context_preview, "resolve_execution_target", lambda *_a, **_k: target)
    monkeypatch.setattr(llm.resolution, "resolve_execution_target", lambda *_a, **_k: target)
    app = FastAPI()
    app.include_router(thread_runtime.router)
    samples = []
    with TestClient(app) as client:
        for _ in range(6):
            start = time.perf_counter_ns()
            response = client.get("/api/threads/owner/context-snapshot/next-turn")
            samples.append((time.perf_counter_ns() - start) / 1_000_000)
            assert response.status_code == 200, response.text
            usage = response.json()["preview"]
            snapshot = usage["context_snapshot"]
            costs = usage["lesson_index_estimates"]
            assert costs["snapshot_id"] == snapshot["snapshot_id"] == usage["snapshot_id"]
            skills = next(row for row in snapshot["source_inventory"] if row.get("metadata", {}).get("provider") == "skills")
            assert skills["metadata"]["lessons"] == []
            assert costs["sources"].get(skills["source_id"], []) == []
            assert str(path) in offer["guidance"]
    db.close()
    print(json.dumps({"calls":len(samples), "cold_ms":samples[0], "warm_median_ms":statistics.median(samples[1:]),
                      "max_ms":max(samples), "environment":"disposable HTTP; one real scanned trial; catalog/auth fixtures; no production latency claim"}))
