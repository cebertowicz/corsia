from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone

from tests.registry_helpers import ARTIFACT_REGISTRY_DDL, seed_artifact


def _empty_projection(thread_id: str):
    from history.types import PortableHistoryProjection

    return PortableHistoryProjection(
        format_version=1,
        thread_id=thread_id,
        history_epoch=0,
        start_exclusive=None,
        sealed_boundary=None,
        turns=(),
        full_raw_replay_available=True,
    )


def _project_db(tmp_path, monkeypatch):
    from tests.project_ref_helpers import project_ref, select_workspace

    workspace = tmp_path / "workspace"
    monkeypatch.setattr("infra.config.WORKSPACE", workspace)
    select_workspace(monkeypatch, workspace)
    project_dir = workspace / "projects" / "project"
    (project_dir / "context").mkdir(parents=True)
    (project_dir / "context" / "PROJECT.md").write_text(
        "Project source instructions", encoding="utf-8"
    )
    (project_dir / "context" / "COORDINATION-MODEL.md").write_text(
        "Project coordination instructions", encoding="utf-8"
    )

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE projects ("
        "id TEXT PRIMARY KEY, name TEXT, path TEXT, bootloader_files TEXT, "
        "conductor_thread_id TEXT)"
    )
    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    conn.executescript(
        ARTIFACT_REGISTRY_DDL + """
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            profile TEXT DEFAULT 'personal',
            title TEXT DEFAULT 'New Chat',
            model TEXT,
            project_id TEXT,
            created_at TEXT,
            updated_at TEXT
        );

        """
    )
    conn.execute(
        "INSERT INTO projects (id, name, path, bootloader_files) VALUES (?, ?, ?, ?)",
        ("p1", "Project", project_ref("project"), json.dumps(["PROJECT.md"])),
    )
    return conn, project_dir


def _seed_tail(conn, project_dir, artifact_id: str, thread_id: str, title: str) -> None:
    seed_artifact(
        conn,
        artifact_id=artifact_id,
        integration="corsia",
        type="document",
        external_id=f"docs/{artifact_id}.md",
        parent_thread_id=thread_id,
        title=title,
        description="artifact",
        path=str(project_dir / "PROJECT.md"),
        metadata="{}",
    )


def test_cc_context_reconstruction_includes_project_bootloader_sources(tmp_path, monkeypatch):
    from subagents.cc_context_reconstruction import build_cc_context_reconstruction

    conn, project_dir = _project_db(tmp_path, monkeypatch)
    _seed_tail(conn, project_dir, "art-cc", "cc-1", "CC Doc")

    reconstruction = build_cc_context_reconstruction(
        thread_id="cc-1",
        base_seed="You are Claude Code.",
        catalog_model_id="claude-code/opus", model_alias="opus",
        working_dir=str(project_dir),
        project_id="p1",
        is_subagent=False,
        db=conn,
    )

    sources = {
        source.source_id: source
        for source in reconstruction.snapshot.source_inventory
    }
    source = sources["project-bootloader:p1:context/PROJECT.md"]
    assert source.owner_surface == "document-viewer"
    assert source.stable_reference == str((project_dir / "context" / "PROJECT.md").resolve())
    assert "dynamic:artifacts-table" not in sources
    assert (
        "Project source instructions"
        in reconstruction.snapshot.frozen_prompt_material["source_text"]
    )
    assert "Project bootloader: context/PROJECT.md" in reconstruction.seed_text
    assert "Project source instructions" in reconstruction.seed_text
    assert "--- BEGIN CORSIA CONTEXT SOURCES ---" in reconstruction.seed_text
    assert "CC Doc" not in reconstruction.seed_text
    assert "--- BEGIN CORSIA EFFECTIVE CONTEXT DIGEST ---" not in reconstruction.seed_text
    assert f"--- CORSIA CONTEXT SNAPSHOT: {reconstruction.snapshot_id} ---" in reconstruction.seed_text


def test_codex_context_reconstruction_includes_project_bootloader_sources(tmp_path, monkeypatch):
    from subagents.codex_context_reconstruction import build_codex_context_reconstruction

    conn, project_dir = _project_db(tmp_path, monkeypatch)
    _seed_tail(conn, project_dir, "art-codex", "codex-1", "Codex Doc")

    reconstruction = build_codex_context_reconstruction(
        thread_id="codex-1",
        user_text="current task",
        working_dir=str(project_dir),
        project_id="p1",
        model="codex/gpt-5-codex",
        model_context_window=200000,
        history_projection=_empty_projection("codex-1"),
        db=conn,
    )

    sources = {
        source.source_id: source
        for source in reconstruction.snapshot.source_inventory
    }
    source = sources["project-bootloader:p1:context/PROJECT.md"]
    assert source.owner_surface == "document-viewer"
    assert source.stable_reference == str((project_dir / "context" / "PROJECT.md").resolve())
    assert "dynamic:artifacts-table" not in sources
    assert (
        "Project source instructions"
        in reconstruction.snapshot.frozen_prompt_material["source_text"]
    )
    assert "Project bootloader: context/PROJECT.md" in reconstruction.task_text
    assert "Project source instructions" in reconstruction.task_text
    assert "Codex Doc" not in reconstruction.task_text
    assert "--- BEGIN CORSIA EFFECTIVE CONTEXT DIGEST ---" not in reconstruction.task_text
    assert f"--- CORSIA CONTEXT SNAPSHOT: {reconstruction.snapshot_id} ---" in reconstruction.task_text


def test_provider_reconstructions_include_skills_index_not_skill_body(tmp_path, monkeypatch):
    from subagents.cc_context_reconstruction import build_cc_context_reconstruction
    from subagents.codex_context_reconstruction import build_codex_context_reconstruction

    workspace = tmp_path / "workspace"
    skill_path = workspace / "skills" / "review" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text(
        "---\n"
        "name: review\n"
        "description: Use when reviewing code.\n"
        "---\n"
        "# Review\n\n"
        "Inspect the diff and report defects first.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("infra.config.WORKSPACE", workspace)
    conn, project_dir = _project_db(tmp_path, monkeypatch)

    cc = build_cc_context_reconstruction(
        thread_id="cc-skills",
        base_seed="You are Claude Code.",
        catalog_model_id="claude-code/opus", model_alias="opus",
        working_dir=str(project_dir),
        project_id="p1",
        is_subagent=False,
        db=conn,
    )
    codex = build_codex_context_reconstruction(
        thread_id="codex-skills",
        user_text="current task",
        working_dir=str(project_dir),
        project_id="p1",
        model="codex/gpt-5-codex",
        model_context_window=200000,
        history_projection=_empty_projection("codex-skills"),
        db=conn,
    )

    expected_skills_header = (
        "Available skills. Before answering, compare the user request to the skill descriptions below. "
        "If the user names a skill or the request matches a skill description, you MUST load that skill's "
        "SKILL.md before responding. Its path is on its line, or else in its group's header with <name> "
        "replaced by the skill's name. Load it with your file-read "
        "tool: `mcp__corsia__read_file` on Claude Code"
    )
    for provider_text in (cc.seed_text, codex.task_text):
        assert expected_skills_header in provider_text
        assert "name=read_file" in provider_text
        assert "review: Use when reviewing code." in provider_text
        root = f"{skill_path.parent.parent.resolve()}/"
        assert f"Skills in {root} (read {root}<name>/SKILL.md):" in provider_text
        assert "Inspect the diff and report defects first." not in provider_text


def _summary_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            profile TEXT DEFAULT 'personal',
            title TEXT DEFAULT 'New Chat',
            model TEXT,
            project_id TEXT,
            history_epoch INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            path TEXT,
            conductor_thread_id TEXT
        );
        CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value TEXT,
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
            active_turn_id TEXT,
            delivery_state TEXT NOT NULL DEFAULT 'delivered'
        );
        CREATE TABLE active_turns (
            id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            status TEXT NOT NULL,
            turn_order INTEGER NOT NULL
        );

        """
    )
    now = datetime.now(timezone.utc).isoformat()
    current = "current-summary-target"
    related = "related-summary-source"
    conn.execute(
        "INSERT INTO projects (id, name, path) VALUES (?, ?, NULL)",
        ("proj-a", "Summary Project"),
    )
    conn.execute(
        "INSERT INTO threads (id, user_id, title, project_id, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (current, "steve", "Current target", "proj-a", now, now),
    )
    conn.execute(
        "INSERT INTO threads (id, user_id, title, project_id, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (related, "steve", "Prior CTX Thread", "proj-a", now, now),
    )

    conn.commit()
    return conn, current


def _insert_sealed_turn(
    conn: sqlite3.Connection,
    *,
    thread_id: str,
    turn_id: str,
    turn_order: int,
    first_seq: int,
    messages: list[tuple[str, str, str, dict]],
) -> None:
    conn.execute(
        "INSERT INTO active_turns (id,thread_id,status,turn_order) "
        "VALUES (?,?,'completed',?)",
        (turn_id, thread_id, turn_order),
    )
    now = datetime.now(timezone.utc).isoformat()
    for offset, (message_id, role, content, extra_metadata) in enumerate(messages):
        metadata = {"turn_id": turn_id, **extra_metadata}
        conn.execute(
            "INSERT INTO messages "
            "(id,thread_id,role,content,metadata,created_at,seq) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                message_id,
                thread_id,
                role,
                content,
                json.dumps(metadata),
                now,
                first_seq + offset,
            ),
        )


def test_cc_context_reconstruction_excludes_related_summary_sources():
    # Related cross-thread summaries are NOT auto-injected into delivery — agents
    # pull summaries proactively via the search tool instead. See tech-debt note
    # on the fragmented summary-injection paths.
    from subagents.cc_context_reconstruction import build_cc_user_frame_reconstruction

    conn, current = _summary_db()

    reconstruction = build_cc_user_frame_reconstruction(
        thread_id=current,
        user_text="continue",
        catalog_model_id="claude-code/sonnet", model_alias="sonnet",
        working_dir="/workspace",
        project_id="proj-a",
        is_subagent=False,
        db=conn,
    )

    sources = {
        source.source_id: source
        for source in reconstruction.snapshot.source_inventory
    }
    assert all(source.origin_section != "summary" for source in sources.values())
    assert '"origin_section": "summary"' not in reconstruction.frame_text


def test_codex_context_reconstruction_excludes_related_summary_sources():
    # Related cross-thread summaries are NOT auto-injected into delivery.
    from subagents.codex_context_reconstruction import build_codex_context_reconstruction

    conn, current = _summary_db()

    reconstruction = build_codex_context_reconstruction(
        thread_id=current,
        user_text="continue",
        working_dir="/workspace",
        project_id="proj-a",
        model="codex/gpt-5-codex",
        model_context_window=200000,
        history_projection=_empty_projection(current),
        db=conn,
    )

    sources = {
        source.source_id: source
        for source in reconstruction.snapshot.source_inventory
    }
    assert all(source.origin_section != "summary" for source in sources.values())
    assert '"origin_section": "summary"' not in reconstruction.task_text


def test_cc_reconstructed_transcript_keeps_related_summary_context_out_of_history(tmp_path):
    from history.projection import render_portable_history
    from subagents.cc_context_reconstruction import reconstruct_cc_transcript_from_corsia

    conn, current = _summary_db()
    _insert_sealed_turn(
        conn,
        thread_id=current,
        turn_id="prior-live-turn",
        turn_order=1,
        first_seq=1,
        messages=[
            (str(uuid.uuid4()), "user", "Prior live user turn", {}),
            (str(uuid.uuid4()), "assistant", "Prior live assistant turn", {}),
        ],
    )
    conn.commit()
    projection = render_portable_history(current, db=conn)

    transcript = reconstruct_cc_transcript_from_corsia(
        projection=projection,
        cc_session_id="cc-summary-proof",
        path=str(tmp_path / "cc-summary-proof.jsonl"),
        working_dir="/workspace",
    )

    text = (tmp_path / "cc-summary-proof.jsonl").read_text(encoding="utf-8")
    assert transcript.line_count == 2
    assert "Related prior thread summaries" not in text
    assert "Prior CTX thread proved related summaries" not in text
    assert "Prior live user turn" in text


def test_cc_reconstructed_transcript_uses_exact_portable_tail_boundary(tmp_path):
    from history.projection import render_portable_history
    from subagents.cc_context_reconstruction import reconstruct_cc_transcript_from_corsia

    conn, current = _summary_db()
    for i in range(8):
        _insert_sealed_turn(
            conn,
            thread_id=current,
            turn_id=f"old-turn-{i}",
            turn_order=i + 1,
            first_seq=i * 2 + 1,
            messages=[
                (f"old-user-{i}", "user", f"old user {i}", {}),
                (f"old-assistant-{i}", "assistant", f"old assistant {i}", {}),
            ],
        )
    _insert_sealed_turn(
        conn,
        thread_id=current,
        turn_id="recent-turn",
        turn_order=9,
        first_seq=17,
        messages=[
            ("recent-user", "user", "recent anchor", {}),
            ("recent-assistant", "assistant", "recent assistant", {
                "llm_tool_calls": [{
                    "id": "toolu_recent",
                    "function": {"name": "Bash", "arguments": '{"cmd":"pwd"}'},
                }],
            }),
            ("recent-tool", "tool", "tool output", {
                "assistant_step_id": "recent-assistant",
                "tool_call_id": "toolu_recent",
                "name": "Bash",
            }),
        ],
    )
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO active_turns (id,thread_id,status,turn_order) "
        "VALUES (?,?,?,?)",
        ("current-turn", current, "running", 10),
    )
    conn.execute(
        "INSERT INTO messages "
        "(id,thread_id,role,content,metadata,created_at,seq,"
        "delivery_state,active_turn_id) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "current-user",
            current,
            "user",
            "current request",
            "{}",
            now,
            None,
            "queued",
            "current-turn",
        ),
    )
    conn.commit()
    full = render_portable_history(
        current,
        db=conn,
        exclude_message_id="current-user",
    )
    tail = render_portable_history(
        current,
        db=conn,
        start_exclusive=full.turns[-2].boundary,
        exclude_message_id="current-user",
    )

    transcript = reconstruct_cc_transcript_from_corsia(
        projection=tail,
        cc_session_id="cc-bounded-proof",
        path=str(tmp_path / "cc-bounded-proof.jsonl"),
        working_dir="/workspace",
    )

    text = (tmp_path / "cc-bounded-proof.jsonl").read_text(encoding="utf-8")
    assert transcript.line_count == 3
    assert "current request" not in text
    assert "old user 7" not in text
    assert "recent anchor" in text
    assert '"type":"tool_use"' in text
    assert '"type":"tool_result"' in text
    assert "tool output" in text
    assert transcript.start_exclusive == full.turns[-2].boundary
    assert transcript.sealed_boundary == full.turns[-1].boundary


def test_cc_reconstructed_transcript_translates_only_frozen_corsia_tools(
    tmp_path,
):
    from history.projection import render_portable_history
    from subagents.cc_context_reconstruction import (
        prepare_cc_transcript_from_corsia,
    )

    conn, current = _summary_db()
    tool_calls = [
        ("call-glob", "glob"),
        ("call-normalizable-but-disallowed", "Bash"),
        ("call-native-unknown", "NotebookEdit"),
        ("call-foreign-mcp", "mcp__foreign__glob"),
    ]
    _insert_sealed_turn(
        conn,
        thread_id=current,
        turn_id="codex-tool-turn",
        turn_order=1,
        first_seq=1,
        messages=[
            ("tool-user", "user", "find the matching files", {}),
            (
                "tool-assistant",
                "assistant",
                "",
                {
                    "llm_tool_calls": [
                        {
                            "id": call_id,
                            "function": {
                                "name": name,
                                "arguments": '{"pattern":"**/*.py"}',
                            },
                        }
                        for call_id, name in tool_calls
                    ],
                },
            ),
            *[
                (
                    f"result-{index}",
                    "tool",
                    f"result for {name}",
                    {
                        "assistant_step_id": "tool-assistant",
                        "tool_call_id": call_id,
                        "name": name,
                    },
                )
                for index, (call_id, name) in enumerate(tool_calls)
            ],
        ],
    )
    conn.commit()
    projection = render_portable_history(current, db=conn)

    prepared = prepare_cc_transcript_from_corsia(
        projection=projection,
        cc_session_id="cc-tool-name-proof",
        path=str(tmp_path / "cc-tool-name-proof.jsonl"),
        working_dir="/workspace",
        allowed_corsia_tool_names=frozenset({"glob"}),
    )

    rows = [json.loads(line) for line in prepared.content.splitlines()]
    assistant_blocks = next(
        row["message"]["content"]
        for row in rows
        if row["type"] == "assistant"
    )
    rendered_calls = [
        (block["id"], block["name"])
        for block in assistant_blocks
        if block["type"] == "tool_use"
    ]
    assert rendered_calls == [
        ("call-glob", "mcp__corsia__glob"),
        ("call-normalizable-but-disallowed", "Bash"),
        ("call-native-unknown", "NotebookEdit"),
        ("call-foreign-mcp", "mcp__foreign__glob"),
    ]
    rendered_result_ids = [
        block["tool_use_id"]
        for row in rows
        if row["type"] == "user" and isinstance(row["message"]["content"], list)
        for block in row["message"]["content"]
        if block["type"] == "tool_result"
    ]
    assert rendered_result_ids == [call_id for call_id, _name in tool_calls]
    source_assistant = next(
        message
        for message in projection.messages
        if message.message_id == "tool-assistant"
    )
    assert [
        call["function"]["name"] for call in source_assistant.tool_calls
    ] == [name for _call_id, name in tool_calls]
