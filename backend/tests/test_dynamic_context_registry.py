from __future__ import annotations

import json
import sqlite3

import pytest


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            title TEXT,
            project_id TEXT,
            model TEXT DEFAULT 'native/test',
            spawned_by TEXT,
            bootloader_files TEXT,
            lifecycle_state TEXT DEFAULT 'active',
            archived_reason TEXT,
            agent_enabled INTEGER DEFAULT 0,
            agent_responsibility TEXT,
            agent_paths TEXT,
            agent_domains TEXT,
            agent_status TEXT DEFAULT 'active',
            agent_notes TEXT,
            history_epoch INTEGER NOT NULL DEFAULT 0,
            execution_epoch INTEGER NOT NULL DEFAULT 0,
            pending_model TEXT,
            pending_execution_epoch INTEGER,
            runtime_context_epoch INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT
        );
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            metadata TEXT,
            created_at TEXT NOT NULL,
            seq INTEGER,
            input_order INTEGER,
            delivery_state TEXT NOT NULL DEFAULT 'delivered'
        );
        CREATE UNIQUE INDEX idx_messages_thread_seq ON messages(thread_id, seq);
        CREATE UNIQUE INDEX idx_messages_thread_input_order
            ON messages(thread_id, input_order) WHERE input_order IS NOT NULL;
        CREATE TABLE checkpoints (
            generation_order INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            thread_id TEXT NOT NULL,
            history_epoch INTEGER NOT NULL,
            status TEXT NOT NULL
        );
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            path TEXT,
            bootloader_files TEXT,
            conductor_thread_id TEXT
        );
        CREATE TABLE artifact_identities (
            id TEXT PRIMARY KEY,
            integration TEXT NOT NULL,
            type TEXT NOT NULL,
            external_id TEXT NOT NULL,
            title TEXT,
            description TEXT,
            path TEXT,
            state TEXT NOT NULL DEFAULT 'active',
            retention TEXT NOT NULL DEFAULT 'durable'
                CHECK (retention IN ('ephemeral', 'durable')),
            metadata TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(integration, type, external_id)
        );
        CREATE TABLE artifact_registrations (
            id TEXT PRIMARY KEY,
            artifact_id TEXT NOT NULL REFERENCES artifact_identities(id),
            parent_thread_id TEXT NOT NULL,
            relationship_state TEXT NOT NULL DEFAULT 'owner-active',
            surfacing TEXT NOT NULL DEFAULT 'visible'
                CHECK (surfacing IN ('visible', 'quiet')),
            metadata TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(artifact_id, parent_thread_id)
        );
        """
    )
    conn.execute(
        "INSERT INTO threads (id, user_id, project_id, updated_at) "
        "VALUES ('sess-1', 'user-1', 'proj-1', 't')"
    )
    return conn


def test_context_registry_includes_bootloader_sources_from_one_collector(
    tmp_path, monkeypatch
):
    from core.dynamic_context_registry import collect_context_sources
    from tests.project_ref_helpers import project_ref, select_workspace

    workspace = tmp_path / "workspace"
    select_workspace(monkeypatch, workspace)
    project_dir = workspace / "projects" / "corsia"
    project_dir.mkdir(parents=True)
    # Directory-as-truth (Phase 1b): static system files are dir-scanned from context/.
    (workspace / "context").mkdir()
    (workspace / "context" / "AGENTS.md").write_text(
        "Always use the shared context collector.", encoding="utf-8"
    )
    (project_dir / "context").mkdir()
    (project_dir / "context" / "PROJECT.md").write_text(
        "Project instructions.", encoding="utf-8"
    )
    (project_dir / "context" / "COORDINATION-MODEL.md").write_text(
        "Project coordination.", encoding="utf-8"
    )

    conn = _db()
    conn.execute(
        "INSERT INTO projects (id, name, path, bootloader_files) VALUES (?, ?, ?, ?)",
        ("proj-1", "CORSIA", project_ref("corsia"), '["PROJECT.md"]'),
    )
    try:
        collection = collect_context_sources(
            thread_id="sess-1",
            project_id="proj-1",
            workspace=workspace,
            db=conn,
        )
    finally:
        conn.close()

    source_ids = {source.source_id for source in collection.sources}
    assert "workspace-context:context/AGENTS.md" in source_ids
    assert "project-bootloader:proj-1:context/PROJECT.md" in source_ids
    workspace_source = next(
        source for source in collection.sources
        if source.source_id == "workspace-context:context/AGENTS.md"
    )
    assert workspace_source.origin_section == "workspace"
    assert workspace_source.prompt_content == (
        "## context/AGENTS.md\n\nAlways use the shared context collector."
    )
    assert collection.project_cwd == str(project_dir)


def test_context_registry_exposes_compact_scoped_owner_map_with_explicit_self(tmp_path):
    from core.dynamic_context_registry import collect_context_sources

    conn = _db()
    conn.execute(
        "UPDATE threads SET title='Current owner', agent_enabled=1, "
        "agent_responsibility='Owns this lane.' WHERE id='sess-1'"
    )
    conn.execute(
        "INSERT INTO threads "
        "(id,user_id,title,project_id,agent_enabled,agent_responsibility,updated_at) "
        "VALUES ('workspace-owner','user-1','Workspace owner',NULL,1,"
        "'Owns shared work.','t')"
    )
    conn.execute(
        "INSERT INTO threads "
        "(id,user_id,title,project_id,agent_enabled,agent_responsibility,updated_at) "
        "VALUES ('project-owner','user-1','Project owner','proj-1',1,"
        "'Owns project work.','t')"
    )
    conn.execute(
        "INSERT INTO threads "
        "(id,user_id,title,project_id,agent_enabled,agent_responsibility,updated_at) "
        "VALUES ('other-user','user-2','Other user owner','proj-1',1,"
        "'Must remain private.','t')"
    )
    try:
        collection = collect_context_sources(
            thread_id="sess-1",
            project_id="proj-1",
            workspace=tmp_path,
            db=conn,
        )
    finally:
        conn.close()

    owner_map = next(
        source for source in collection.sources
        if source.source_id == "role:agent-owner-map"
    )
    content = owner_map.prompt_content or ""
    assert "Self: Current owner (sess-1) — Owns this lane." in content
    assert "Project owner" in content
    assert "Workspace owner" not in content
    assert "Other user owner" not in content
    assert "agent_paths" not in content
    assert not any(
        source.source_id == "dynamic:runtime-context"
        for source in collection.sources
    )


def test_responsibility_map_order_ignores_unrelated_thread_updates(tmp_path):
    from core.dynamic_context_registry import collect_context_sources

    conn = _db()
    for thread_id, title, updated_at in (
        ("agent-b", "Agent B", "t2"),
        ("agent-a", "Agent A", "t1"),
    ):
        conn.execute(
            "INSERT INTO threads "
            "(id,user_id,title,project_id,agent_enabled,agent_responsibility,updated_at) "
            "VALUES (?,'user-1',?,'proj-1',1,'Owns work.',?)",
            (thread_id, title, updated_at),
        )

    def _content() -> str:
        collection = collect_context_sources(
            thread_id="sess-1", project_id="proj-1", workspace=tmp_path, db=conn,
        )
        source = next(
            item for item in collection.sources
            if item.source_id == "role:agent-owner-map"
        )
        return source.prompt_content or ""

    before = _content()
    conn.execute("UPDATE threads SET updated_at='t3' WHERE id='agent-a'")
    after = _content()

    assert before == after
    assert after.index("Agent A") < after.index("Agent B")


@pytest.mark.parametrize(
    "fields",
    (
        "agent_status='inactive'",
        "lifecycle_state='archived',archived_reason='user'",
    ),
)
def test_responsibility_map_does_not_claim_ineligible_self(fields, tmp_path):
    from core.dynamic_context_registry import collect_context_sources

    conn = _db()
    conn.execute(
        "UPDATE threads SET title='Inactive agent',agent_enabled=1,"
        f"agent_responsibility='Old lane.',{fields} WHERE id='sess-1'"
    )

    collection = collect_context_sources(
        thread_id="sess-1", project_id="proj-1", workspace=tmp_path, db=conn,
    )
    source = next(
        item for item in collection.sources
        if item.source_id == "role:agent-owner-map"
    )

    assert "has no registered responsibility" in (source.prompt_content or "")
    assert "Self: Inactive agent (sess-1) — Old lane." not in (
        source.prompt_content or ""
    )


def _collect(conn, workspace, project_id="proj-1"):
    from core.dynamic_context_registry import collect_context_sources

    return collect_context_sources(
        thread_id="sess-1", project_id=project_id, workspace=workspace, db=conn,
    )


def _owner_map(collection):
    return next(
        source for source in collection.sources
        if source.source_id == "role:agent-owner-map"
    )


def _dispatch(conn, workspace, project_id="proj-1"):
    """Collect and retain what was delivered, as a dispatch does."""
    from core.installed_context import persist_installed_delivery

    collection = _collect(conn, workspace, project_id)
    persist_installed_delivery(collection.installed, db=conn)
    return collection


def test_retained_responsibility_map_cannot_cross_project_scope(tmp_path):
    conn = _db()
    conn.execute(
        "INSERT INTO messages "
        "(id,thread_id,role,content,created_at,seq) "
        "VALUES ('input','sess-1','user','question','now',1)"
    )
    conn.execute(
        "INSERT INTO threads "
        "(id,user_id,title,project_id,agent_enabled,agent_responsibility,updated_at) "
        "VALUES ('old-agent','user-1','Old agent','proj-1',1,'Old scope.','t')"
    )
    assert _dispatch(conn, tmp_path).installed.pending
    conn.execute("UPDATE threads SET project_id='proj-2' WHERE id='sess-1'")
    conn.execute(
        "INSERT INTO threads "
        "(id,user_id,title,project_id,agent_enabled,agent_responsibility,updated_at) "
        "VALUES ('new-agent','user-1','New agent','proj-2',1,'New scope.','t')"
    )

    source = _owner_map(_collect(conn, tmp_path, "proj-2"))

    assert source.metadata["responsibility_map_snapshot"]["project_id"] == "proj-2"
    assert "New scope." in (source.prompt_content or "")
    assert "Old scope." not in (source.prompt_content or "")


def test_provider_preparation_can_freeze_map_on_first_unsequenced_input(tmp_path):
    conn = _db()
    conn.execute(
        "UPDATE threads SET model='native/model',title='Current agent',"
        "agent_enabled=1,agent_responsibility='Original lane.' WHERE id='sess-1'"
    )
    conn.execute(
        "INSERT INTO messages "
        "(id,thread_id,role,content,created_at,input_order,delivery_state) "
        "VALUES ('input','sess-1','user','question','now',1,'queued')"
    )
    delivered = _dispatch(conn, tmp_path)
    assert delivered.installed.boundary_id == "input"

    conn.execute(
        "UPDATE threads SET agent_responsibility='Changed lane.' WHERE id='sess-1'"
    )
    retained = _owner_map(_collect(conn, tmp_path))
    metadata = json.loads(conn.execute(
        "SELECT metadata FROM messages WHERE id='input'"
    ).fetchone()["metadata"])
    assert metadata["responsibility_map_snapshot"]["content"] == (
        _owner_map(delivered).prompt_content
    )
    assert "Original lane." in (retained.prompt_content or "")
    assert "Changed lane." not in (retained.prompt_content or "")


def test_context_collection_does_not_persist_responsibility_map(tmp_path):
    from core.dynamic_context_registry import collect_context_sources

    conn = _db()
    conn.execute(
        "INSERT INTO messages "
        "(id,thread_id,role,content,created_at,seq) "
        "VALUES ('input','sess-1','user','question','now',1)"
    )

    collection = collect_context_sources(
        thread_id="sess-1", project_id="proj-1", workspace=tmp_path, db=conn,
    )

    assert collection.agent_facts["responsibility_map_snapshot"] is not None
    assert conn.execute(
        "SELECT metadata FROM messages WHERE id='input'"
    ).fetchone()["metadata"] is None


def test_terminal_receipt_does_not_persist_responsibility_map(tmp_path):
    from core.dynamic_context_registry import collect_context_sources
    from infra.context_receipts import ContextReceiptIdentity, attach_context_receipt

    conn = _db()
    conn.execute(
        "INSERT INTO messages "
        "(id,thread_id,role,content,metadata,created_at,seq) VALUES "
        "('input','sess-1','user','question',NULL,'now',1),"
        "('answer','sess-1','assistant','done','{\"turn_id\":\"turn-1\"}',"
        "'now',2)"
    )
    collection = collect_context_sources(
        thread_id="sess-1", project_id="proj-1", workspace=tmp_path, db=conn,
    )
    attach_context_receipt(
        "sess-1",
        "answer",
        context_usage={"context_snapshot": {"agent_facts": collection.agent_facts}},
        identity=ContextReceiptIdentity("native", "native/test", 1, None),
        db=conn,
    )

    assert conn.execute(
        "SELECT metadata FROM messages WHERE id='input'"
    ).fetchone()["metadata"] is None


@pytest.mark.parametrize("harness", ["native", "claude-code", "codex"])
def test_responsibility_map_anchor_freezes_same_model_but_switch_refreshes(
    tmp_path, harness,
):
    from core.installed_context import persist_installed_delivery
    from core.dynamic_context_registry import collect_context_sources

    conn = _db()
    model = f"{harness}/first"
    conn.execute(
        "UPDATE threads SET model=?,title='Current owner',agent_enabled=1,"
        "agent_responsibility='Owns this lane.' WHERE id='sess-1'",
        (model,),
    )
    conn.execute(
        "INSERT INTO threads "
        "(id,user_id,title,project_id,model,agent_enabled,agent_responsibility,updated_at) "
        "VALUES ('project-owner','user-1','Project owner','proj-1',?,1,"
        "'Original responsibility.','t')",
        (model,),
    )
    conn.execute(
        "INSERT INTO messages "
        "(id,thread_id,role,content,metadata,created_at,seq) "
        "VALUES ('input','sess-1','user','question',NULL,'now',1)"
    )
    first = collect_context_sources(
        thread_id="sess-1", project_id="proj-1", workspace=tmp_path, db=conn,
    )
    owner_map = _owner_map(first)
    assert persist_installed_delivery(first.installed, db=conn)
    anchored = conn.execute(
        "SELECT role,content,seq,metadata FROM messages WHERE id='input'"
    ).fetchone()
    assert (anchored["role"], anchored["content"], anchored["seq"]) == (
        "user", "question", 1,
    )
    assert "responsibility_map_snapshot" in json.loads(
        anchored["metadata"]
    )
    conn.execute(
        "UPDATE threads SET agent_responsibility='Changed responsibility.' "
        "WHERE id='project-owner'"
    )

    after_restart = collect_context_sources(
        thread_id="sess-1", project_id="proj-1", workspace=tmp_path, db=conn,
    )
    retained = next(
        source for source in after_restart.sources
        if source.source_id == "role:agent-owner-map"
    )
    assert retained.prompt_content == owner_map.prompt_content
    assert "Changed responsibility." not in (retained.prompt_content or "")

    assert not persist_installed_delivery(after_restart.installed, db=conn)
    assert conn.execute(
        "SELECT COUNT(*) FROM messages WHERE json_valid(metadata) AND "
        "json_type(metadata,'$.responsibility_map_snapshot')='object'"
    ).fetchone()[0] == 1

    conn.execute(
        "UPDATE threads SET model=?,execution_epoch=1 WHERE id='sess-1'",
        (f"{harness}/next",),
    )
    after_switch = collect_context_sources(
        thread_id="sess-1", project_id="proj-1", workspace=tmp_path, db=conn,
    )
    refreshed = next(
        source for source in after_switch.sources
        if source.source_id == "role:agent-owner-map"
    )
    assert "Changed responsibility." in (refreshed.prompt_content or "")

    conn.execute(
        "UPDATE threads SET model=?,execution_epoch=2,"
        "agent_responsibility='Returned model responsibility.' WHERE id='sess-1'",
        (model,),
    )
    returned = collect_context_sources(
        thread_id="sess-1", project_id="proj-1", workspace=tmp_path, db=conn,
    )
    returned_map = next(
        source for source in returned.sources
        if source.source_id == "role:agent-owner-map"
    )
    assert "Returned model responsibility." in (returned_map.prompt_content or "")


def test_owner_map_is_gracefully_omitted_without_database():
    from core.agent_context_sources import installed_owner_map

    source, snapshot, _adoption = installed_owner_map(
        thread_id="thread", db=None, stored=None,
    )

    assert snapshot is None
    assert source.source_id == "role:agent-owner-map"
    assert source.inclusion == "omitted"
    assert source.freshness == "unavailable"


@pytest.mark.asyncio
async def test_agent_roster_tool_reads_full_details_for_current_scope(monkeypatch):
    from core.agent_context_sources import AgentRosterTool

    conn = _db()
    conn.execute(
        "UPDATE threads SET title='Current owner',agent_enabled=1,"
        "agent_responsibility='Owns this lane.',agent_paths='[\"backend/\"]',"
        "agent_notes='Coordinate before editing.' WHERE id='sess-1'"
    )

    async def _inline(fn):
        return fn()

    monkeypatch.setattr("infra.database.get_db", lambda: conn)
    monkeypatch.setattr("infra.db_async.run_in_db", _inline)

    result = await AgentRosterTool().execute(_thread_id="sess-1")

    assert "Self: Current owner (sess-1) — Owns this lane." in result
    assert "paths/domains=backend/" in result
    assert "notes=Coordinate before editing." in result


@pytest.mark.asyncio
async def test_project_agent_roster_includes_only_workspace_conductor(monkeypatch):
    from core.agent_context_sources import AgentRosterTool

    conn = _db()
    for thread_id, title, enabled, responsibility in (
        ("workspace-conductor", "Workspace conductor", 1, None),
        ("other-workspace-agent", "Other workspace agent", 1, "Other lane."),
    ):
        conn.execute(
            "INSERT INTO threads "
            "(id,user_id,title,project_id,agent_enabled,agent_responsibility,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (thread_id, "user-1", title, None, enabled, responsibility, "t"),
        )
    conn.execute(
        "INSERT INTO settings (key,value,updated_at) VALUES (?,?,?)",
        (
            "workspace_conductor_session_id",
            json.dumps({"session_id": "workspace-conductor"}),
            "t",
        ),
    )

    async def _inline(fn):
        return fn()

    monkeypatch.setattr("infra.database.get_db", lambda: conn)
    monkeypatch.setattr("infra.db_async.run_in_db", _inline)

    result = await AgentRosterTool().execute(_thread_id="sess-1")

    assert "Workspace conductor (workspace-conductor); scope=workspace" in result
    assert "Other workspace agent" not in result


def _designate_workspace_conductor(conn, value) -> None:
    conn.execute(
        "INSERT INTO settings (key,value,updated_at) VALUES (?,?,?)",
        ("workspace_conductor_session_id", value, "t"),
    )


def _insert_workspace_conductor(conn, **overrides) -> None:
    fields = {
        "id": "workspace-conductor", "user_id": "user-1",
        "title": "Workspace conductor", "project_id": None, "agent_enabled": 0,
        "agent_status": "active", "lifecycle_state": "active",
        "archived_reason": None, "updated_at": "t",
    } | overrides
    conn.execute(
        f"INSERT INTO threads ({','.join(fields)}) "
        f"VALUES ({','.join('?' for _ in fields)})",
        tuple(fields.values()),
    )


def test_project_roster_lists_designated_conductor_that_is_not_a_registered_agent():
    """Production shape (#2634): a designated conductor never promoted to an agent."""
    from core.agent_context_sources import current_responsibility_map_snapshot
    from core.agent_roster_queries import agent_rows

    conn = _db()
    _insert_workspace_conductor(conn, agent_enabled=0)
    _designate_workspace_conductor(
        conn, json.dumps({"session_id": "workspace-conductor"}),
    )

    current, rows = agent_rows("sess-1", conn)
    snapshot = current_responsibility_map_snapshot("sess-1", conn)

    assert [row["id"] for row in rows if row["project_id"] is None] == [
        "workspace-conductor"
    ]
    assert "workspace_conductor_gap" not in current
    assert "workspace-conductor" in snapshot["agent_thread_ids"]
    assert (
        "Workspace conductor (workspace-conductor) [workspace] — "
        "Designated workspace conductor." in snapshot["content"]
    )


@pytest.mark.parametrize(
    ("setting", "overrides", "reason"),
    [
        (None, None, "none designated"),
        (json.dumps({"session_id": None}), None, "none designated"),
        ("{not-json", None, "the workspace conductor setting is malformed"),
        (None, {}, "none designated"),
        ("designated", None,
         "designated conductor workspace-conductor does not exist"),
        ("designated", {"user_id": "user-2"},
         "designated conductor belongs to another account"),
        ("designated", {"project_id": "proj-1"},
         "designated conductor workspace-conductor is unreachable: "
         "designated workspace conductor is not a workspace thread"),
        ("designated", {"agent_enabled": 1, "agent_status": "inactive"},
         "designated conductor workspace-conductor is unreachable: "
         "agent is not active"),
        ("designated", {"lifecycle_state": "archived", "archived_reason": "user"},
         "designated conductor workspace-conductor is unreachable: "
         "thread is archived"),
        ("designated", {"lifecycle_state": "purged"},
         "designated conductor workspace-conductor is unreachable: "
         "thread has been purged"),
    ],
)
def test_project_roster_names_why_workspace_conductor_is_missing(
    setting, overrides, reason,
):
    from core.agent_context_sources import (
        _full_roster_content,
        current_responsibility_map_snapshot,
    )
    from core.agent_roster_queries import agent_rows

    conn = _db()
    if overrides is not None:
        _insert_workspace_conductor(conn, **overrides)
    if setting == "designated":
        setting = json.dumps({"session_id": "workspace-conductor"})
    if setting is not None:
        _designate_workspace_conductor(conn, setting)

    current, rows = agent_rows("sess-1", conn)
    snapshot = current_responsibility_map_snapshot("sess-1", conn)
    line = f"Workspace conductor: not reachable ({reason})."

    assert all(row["id"] != "workspace-conductor" for row in rows)
    assert current["workspace_conductor_gap"] == reason
    assert "workspace-conductor" not in snapshot["agent_thread_ids"]
    assert line in snapshot["content"]
    assert line in _full_roster_content(current, rows)


def test_workspace_roster_has_no_conductor_gap_line():
    from core.agent_context_sources import current_responsibility_map_snapshot
    from core.agent_roster_queries import agent_rows

    conn = _db()
    conn.execute("UPDATE threads SET project_id=NULL WHERE id='sess-1'")

    current, _ = agent_rows("sess-1", conn)

    assert "workspace_conductor_gap" not in current
    assert "Workspace conductor:" not in current_responsibility_map_snapshot(
        "sess-1", conn,
    )["content"]


def test_context_registry_rejects_corrupt_workspace_conductor_setting(tmp_path):
    import json

    from core.dynamic_context_registry import collect_context_sources
    from infra.conductor import ConductorSettingError

    conn = _db()
    conn.execute(
        "INSERT INTO settings (key,value,updated_at) VALUES (?,?,?)",
        (
            "workspace_conductor_session_id",
            json.dumps("workspace-conductor"),
            "t",
        ),
    )
    try:
        with pytest.raises(ConductorSettingError, match="workspace conductor"):
            collect_context_sources(
                thread_id="sess-1",
                project_id=None,
                workspace=tmp_path,
                db=conn,
            )
    finally:
        conn.close()


def test_context_registry_selects_agent_role_context_and_skills(tmp_path):
    from core.dynamic_context_registry import collect_context_sources

    role_root = tmp_path / "context" / "roles" / "agent"
    role_root.mkdir(parents=True)
    (role_root / "AGENT.md").write_text("Agent-wide context.", encoding="utf-8")
    skill = role_root / "skills" / "improve" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: improve\ndescription: Use when improving agent behavior.\n---\n"
        "# Improve\n",
        encoding="utf-8",
    )

    conn = _db()
    conn.execute(
        "UPDATE threads SET project_id=NULL, agent_enabled=1 WHERE id='sess-1'"
    )
    try:
        collection = collect_context_sources(
            thread_id="sess-1",
            project_id=None,
            workspace=tmp_path,
            db=conn,
        )
    finally:
        conn.close()

    role_sources = [
        source
        for source in collection.sources
        if source.metadata.get("provider") == "role_context"
    ]
    assert collection.agent_facts["roles"] == ["agent"]
    assert collection.agent_facts["receives_agent_role"] is True
    assert collection.agent_facts["is_conductor"] is False
    assert [(source.metadata["role"], source.metadata["scope"]) for source in role_sources] == [
        ("agent", "workspace")
    ]
    assert role_sources[0].origin_section == "role"
    assert role_sources[0].metadata["relative_path"] == "context/roles/agent/AGENT.md"
    skills = next(
        source
        for source in collection.sources
        if source.source_id == "dynamic:skills-index"
    )
    assert "improve: Use when improving agent behavior." in (skills.prompt_content or "")
    assert not any(
        source.metadata.get("provider") == "conductor_template"
        for source in collection.sources
    )


def test_context_registry_composes_agent_then_conductor_context(tmp_path):
    import json

    from core.dynamic_context_registry import collect_context_sources

    for role, filename, content in (
        ("agent", "AGENT.md", "Agent foundation."),
        ("conductor", "CONDUCTOR.md", "Conductor addition."),
    ):
        path = tmp_path / "context" / "roles" / role / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    conn = _db()
    conn.execute(
        "UPDATE threads SET project_id=NULL, agent_enabled=0 WHERE id='sess-1'"
    )
    conn.execute(
        "INSERT INTO settings (key,value,updated_at) VALUES (?,?,?)",
        (
            "workspace_conductor_session_id",
            json.dumps({"session_id": "sess-1"}),
            "t",
        ),
    )
    try:
        collection = collect_context_sources(
            thread_id="sess-1",
            project_id=None,
            workspace=tmp_path,
            db=conn,
        )
    finally:
        conn.close()

    composed = []
    for source in collection.sources:
        if source.metadata.get("provider") == "role_context":
            composed.append(("role", source.metadata["role"]))
        elif source.metadata.get("provider") == "conductor_template":
            composed.append(("template", source.metadata["template_kind"]))
    assert composed == [
        ("role", "agent"),
        ("template", "core"),
        ("template", "workspace"),
        ("role", "conductor"),
    ]
    assert collection.agent_facts["roles"] == ["agent", "conductor"]
    assert collection.agent_facts["receives_agent_role"] is True
    assert collection.agent_facts["is_conductor"] is True
    assert collection.agent_facts["conductor_scopes"] == [
        {"scope": "workspace", "scope_type": "workspace"}
    ]


def test_context_registry_exposes_thread_bootloader_source(tmp_path):
    import json

    from core.dynamic_context_registry import collect_context_sources

    thread_file = tmp_path / "thread.md"
    thread_file.write_text("Thread-specific standing context.", encoding="utf-8")

    conn = _db()
    conn.execute(
        "UPDATE threads SET bootloader_files = ? WHERE id = ?",
        (json.dumps([str(thread_file)]), "sess-1"),
    )
    try:
        collection = collect_context_sources(
            thread_id="sess-1",
            project_id=None,
            workspace=tmp_path,
            db=conn,
        )
    finally:
        conn.close()

    thread_source = next(
        source for source in collection.sources
        if source.source_id.startswith("thread-bootloader:sess-1:")
    )
    assert thread_source.origin_section == "thread"
    assert thread_source.inclusion == "included"
    assert thread_source.stable_reference == str(thread_file.resolve())
    assert "Thread-specific standing context." in (thread_source.prompt_content or "")


def test_unresolved_project_does_not_load_workspace_relative_thread_bootloader(tmp_path):
    """Project-scoped threads must not silently reinterpret relative paths at workspace scope."""
    import json

    from core.dynamic_context_registry import collect_context_sources

    workspace = tmp_path / "workspace"
    fallback = workspace / "bootloaders" / "thread.md"
    fallback.parent.mkdir(parents=True)
    fallback.write_text("must not leak from workspace scope", encoding="utf-8")

    conn = _db()
    conn.execute(
        "UPDATE threads SET bootloader_files = ? WHERE id = ?",
        (json.dumps(["bootloaders/thread.md"]), "sess-1"),
    )
    try:
        collection = collect_context_sources(
            thread_id="sess-1",
            project_id="proj-1",
            workspace=workspace,
            db=conn,
        )
    finally:
        conn.close()

    assert not any(
        source.source_id.startswith("thread-bootloader:sess-1:")
        for source in collection.sources
    )


def test_thread_context_dir_auto_loads_as_bootloader(tmp_path, monkeypatch):
    """``.threads/{id}/context/*.md`` auto-load as thread bootloader,
    mirroring the system/project context/ dir-scan."""
    from core.dynamic_context_registry import collect_context_sources
    from tests.project_ref_helpers import project_ref, select_workspace

    workspace = tmp_path / "workspace"
    select_workspace(monkeypatch, workspace)
    project_dir = workspace / "projects" / "corsia"
    (project_dir / "context").mkdir(parents=True)
    (project_dir / "context" / "PROJECT.md").write_text(
        "Project instructions.", encoding="utf-8"
    )
    (project_dir / "context" / "COORDINATION-MODEL.md").write_text(
        "Project coordination.", encoding="utf-8"
    )
    ctx_dir = project_dir / ".threads" / "sess-1" / "context"
    ctx_dir.mkdir(parents=True)
    (ctx_dir / "standing.md").write_text("Thread standing note.", encoding="utf-8")

    conn = _db()
    conn.execute(
        "INSERT INTO projects (id, name, path) VALUES (?, ?, ?)",
        ("proj-1", "CORSIA", project_ref("corsia")),
    )
    try:
        collection = collect_context_sources(
            thread_id="sess-1",
            project_id="proj-1",
            workspace=workspace,
            db=conn,
        )
    finally:
        conn.close()

    thread_source = next(
        source for source in collection.sources
        if source.source_id.startswith("thread-bootloader:sess-1:")
        and source.label == "Thread bootloader: standing.md"
    )
    assert thread_source.origin_section == "thread"
    assert thread_source.inclusion == "included"
    assert thread_source.stable_reference == str((ctx_dir / "standing.md").resolve())
    assert "Thread standing note." in (thread_source.prompt_content or "")


def test_thread_context_dir_deduped_by_configured_basename(tmp_path):
    """A configured (DB) thread bootloader entry wins over a same-named
    context/ dir-scan file — no double injection."""
    import json

    from core.dynamic_context_registry import collect_context_sources

    workspace = tmp_path / "workspace"
    ctx_dir = workspace / ".threads" / "sess-1" / "context"
    ctx_dir.mkdir(parents=True)
    (ctx_dir / "note.md").write_text("dir-scan body", encoding="utf-8")

    configured = workspace / "configured-note.md"
    configured.write_text("configured body", encoding="utf-8")

    conn = _db()
    # Configured entry shares the basename 'note.md' via a different file path.
    dupe = workspace / "note.md"
    dupe.write_text("configured note body", encoding="utf-8")
    conn.execute(
        "UPDATE threads SET bootloader_files = ? WHERE id = ?",
        (json.dumps([str(dupe)]), "sess-1"),
    )
    try:
        collection = collect_context_sources(
            thread_id="sess-1", project_id=None, workspace=workspace, db=conn
        )
    finally:
        conn.close()

    note_sources = [
        s for s in collection.sources
        if s.origin_section == "thread" and s.label == "Thread bootloader: note.md"
    ]
    assert len(note_sources) == 1
    assert note_sources[0].stable_reference == str(dupe.resolve())


def test_context_registry_exposes_compact_agent_owner_map(tmp_path):
    import json

    from core.dynamic_context_registry import collect_context_sources

    conn = _db()
    conn.execute(
        """
        INSERT INTO threads (
            id, user_id, title, project_id, updated_at, agent_enabled,
            agent_responsibility, agent_paths, agent_domains,
            agent_status, agent_notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "owner-1",
            "user-1",
            "Release Coordinator",
            "proj-1",
            "t2",
            1,
            "Coordinates release-specific work.",
            json.dumps(["releases/"]),
            json.dumps(["release coordination"]),
            "active",
            "Ping for release sequencing.",
        ),
    )
    try:
        collection = collect_context_sources(
            thread_id="sess-1",
            project_id="proj-1",
            workspace=tmp_path,
            db=conn,
        )
    finally:
        conn.close()

    agents = next(
        source for source in collection.sources
        if source.source_id == "role:agent-owner-map"
    )
    assert agents.origin_section == "role"
    assert agents.inclusion == "included"
    assert agents.metadata["provider"] == "agent_owner_map"
    assert agents.metadata["agent_count"] == 1
    assert "Release Coordinator" in (agents.prompt_content or "")
    assert "Coordinates release-specific work." in (agents.prompt_content or "")
    assert "release coordination" not in (agents.prompt_content or "")


def test_context_registry_owner_map_omits_with_repair_owner_when_schema_missing(tmp_path):
    from core.dynamic_context_registry import collect_context_sources

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE threads (id TEXT PRIMARY KEY, project_id TEXT);
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            conductor_thread_id TEXT
        );
        CREATE TABLE artifact_identities (
            id TEXT PRIMARY KEY,
            integration TEXT NOT NULL,
            type TEXT NOT NULL,
            external_id TEXT NOT NULL,
            title TEXT,
            description TEXT,
            path TEXT,
            state TEXT NOT NULL DEFAULT 'active',
            retention TEXT NOT NULL DEFAULT 'durable'
                CHECK (retention IN ('ephemeral', 'durable')),
            metadata TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(integration, type, external_id)
        );
        CREATE TABLE artifact_registrations (
            id TEXT PRIMARY KEY,
            artifact_id TEXT NOT NULL REFERENCES artifact_identities(id),
            parent_thread_id TEXT NOT NULL,
            relationship_state TEXT NOT NULL,
            surfacing TEXT NOT NULL DEFAULT 'visible'
                CHECK (surfacing IN ('visible', 'quiet')),
            metadata TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(artifact_id, parent_thread_id)
        );
        """
    )
    conn.execute("INSERT INTO threads (id, project_id) VALUES ('sess-1', NULL)")
    try:
        collection = collect_context_sources(
            thread_id="sess-1",
            project_id=None,
            workspace=tmp_path,
            db=conn,
        )
    finally:
        conn.close()

    agents = next(
        source for source in collection.sources
        if source.source_id == "role:agent-owner-map"
    )
    assert agents.inclusion == "omitted"
    assert agents.freshness == "unavailable"
    assert agents.metadata["repair_owner"] == "agents"
