from __future__ import annotations

import logging

import pytest

from tests.registry_helpers import make_session


@pytest.fixture
def header_scope(isolated_db):
    from infra.database import get_db

    db = get_db()
    db.execute(
        "INSERT INTO projects (id,name,path,status,created_at,updated_at) "
        "VALUES ('project-alpha','Alpha','projects/alpha','active','t','t')",
    )
    lanes = {
        title: make_session(db, project_id="project-alpha", title=title, prefix=prefix)
        for title, prefix in (
            ("Context & Memory", "memory"),
            ("Documents & Artifacts", "documents"),
            ("CORSIA Conductor II", "conductor"),
        )
    }
    worker = make_session(db, project_id="project-alpha", title="Worker", prefix="worker")
    lanes["Agent Substrate"] = make_session(
        db, project_id="project-alpha", title="Agent Substrate", prefix="substrate",
    )
    db.execute("UPDATE threads SET id='b96b3847' WHERE id=?", (lanes["Agent Substrate"],))
    lanes["Agent Substrate"] = "b96b3847"
    db.execute(
        "UPDATE threads SET agent_enabled=1 WHERE id IN (?,?,?,?)", tuple(lanes.values()),
    )
    db.execute(
        "UPDATE projects SET conductor_thread_id=? WHERE id='project-alpha'",
        (lanes["CORSIA Conductor II"],),
    )
    db.commit()
    return {"db": db, "worker": worker, **lanes}


def _resolve(scope, value: str) -> str | None:
    from registry.shared_owner_header import resolve_header_owner

    return resolve_header_owner(value, "project-alpha", scope["db"])


def test_header_shapes_written_in_the_workspace_resolve_to_one_lane(header_scope):
    memory = header_scope["Context & Memory"]
    documents = header_scope["Documents & Artifacts"]
    conductor = header_scope["CORSIA Conductor II"]
    worker = header_scope["worker"]
    cases = {
        f"Context & Memory ({memory})": memory,
        f"Context & Memory (`{memory}`)": memory,
        "Context & Memory": memory,
        f"Context & Memory (`{memory}`); worker `{worker}`": memory,
        f"Context & Memory ({memory}); written by temporary research worker {worker}": memory,
        f"Context & Memory (`{memory}`), plan node c9e96d2d": memory,
        "Context & Memory (audit by temporary worker dbf2e9ed)": memory,
        "Documents & Artifacts. Product Experience accepts the shell parts.": documents,
        f"CORSIA Conductor II (`{conductor}`). **Status:** current.": conductor,
        "conductor": conductor,
        f"Plans repair thread `{memory}`": memory,
        "Substrate lane b96b3847": "b96b3847",
    }
    assert {value: _resolve(header_scope, value) for value in cases} == cases


def test_header_naming_no_or_several_lanes_resolves_to_none(header_scope):
    memory = header_scope["Context & Memory"]
    documents = header_scope["Documents & Artifacts"]
    for value in (
        "DESIGN",
        "<agent or thread>",
        "Claude (this plan, its execution, and its delivery)",
        f"Context & Memory ({memory}) and Documents & Artifacts ({documents})",
    ):
        assert _resolve(header_scope, value) is None, value


def test_startup_failure_in_one_scope_does_not_skip_the_next(tmp_path, monkeypatch, caplog):
    from registry import shared_ownership_assignment as assignment

    class StartupDb:
        def execute(self, _sql):
            return ({"id": "project-a"}, {"id": "project-b"})

        def close(self):
            pass

    calls = []

    def apply(_workspace, project_id, *, apply, db):
        calls.append(project_id)
        if project_id == "project-a":
            raise ValueError("shared ownership assignment has unassignable files: x")

    monkeypatch.setattr(assignment, "get_db", StartupDb)
    monkeypatch.setattr(assignment, "conductor_adopts_unowned", lambda *_a, **_k: False)
    monkeypatch.setattr(assignment, "apply_shared_ownership", apply)
    with caplog.at_level(logging.ERROR, logger=assignment.logger.name):
        assignment.run_startup_assignment(tmp_path)
    assert calls == [None, "project-a", "project-b"]
    assert "failed for scope project-a" in caplog.text
