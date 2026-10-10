"""Disposable HTTP preview: real source scan, database, builder and JSON export."""
from __future__ import annotations

import json
import sqlite3
import statistics
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.project_ref_helpers import select_workspace
from test_context_preview_contracts import _target
from test_lesson_index import _activate


def test_http_preview_keeps_lesson_projection_on_a_real_scanned_source(isolated_db, tmp_path, monkeypatch):
    from api import thread_runtime
    from core import context_preview
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
            [lesson] = skills["metadata"]["lessons"]
            assert lesson["path"] == str(path)
            [estimate] = costs["sources"][skills["source_id"]]
            assert estimate["behavioral_sha256"] == lesson["behavioral_sha256"]
            assert 0 <= estimate["prompt_tokens"] <= skills["prompt_tokens"]
    db.close()
    print(json.dumps({"calls":len(samples), "cold_ms":samples[0], "warm_median_ms":statistics.median(samples[1:]),
                      "max_ms":max(samples), "environment":"disposable HTTP; one real scanned trial; catalog/auth fixtures; no production latency claim"}))
