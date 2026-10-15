from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


def _insert_session(db, thread_id: str) -> None:
    db.execute(
        "INSERT INTO threads "
        "(id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?)",
        (thread_id, "steve", thread_id, "t", "t"),
    )


def test_snapshot_merge_counts_delivered_conductor_tokens():
    from infra.context_usage_attribution import merge_context_usage_with_snapshot

    snapshot = {
        "snapshot_id": "ctx-conductor-accounting",
        "harness_model_facts": {
            "harness": "claude-code",
            "model_context_window": 200_000,
        },
        "source_inventory": [
            {
                "source_id": "conductor-template:core:v2",
                "origin_section": "conductor",
                "inclusion": "included",
                "prompt_tokens": 123,
            }
        ],
        "frozen_prompt_material": {},
    }

    merged = merge_context_usage_with_snapshot(
        {"model_context_window": 200_000, "token_source": "estimated"},
        snapshot=snapshot,
    )

    assert merged is not None
    assert merged["context_source_tokens"] == 123
    assert merged["total_used"] == 123
    assert merged["attribution_delta_tokens"] == 0


def test_next_turn_preview_keeps_conductor_attribution_counted(
    isolated_db,
    monkeypatch,
    tmp_path,
):
    from core.context_preview import build_next_turn_context_preview
    from infra.conductor import set_conductor
    from infra.database import get_db
    from llm.resolution import ExecutionTarget

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr("core.context_preview.WORKSPACE", workspace)
    monkeypatch.setattr(
        "core.context_preview.resolve_execution_target",
        lambda _model, db=None: ExecutionTarget(
            catalog_id="claude-code/opus",
            harness="claude-code",
            provider="claude-code",
            backend="cc",
            transport_model="opus",
            expected_reported_model="opus",
            context_window=200_000,
            available=True,
            availability_reason=None,
            display_name="Claude Code Opus",
            supports_reasoning=True,
        ),
    )
    monkeypatch.setattr(
        "llm.capabilities.get_model_max_output_tokens",
        lambda _model, fallback: fallback,
    )

    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads "
            "(id,user_id,title,model,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            (
                "workspace-conductor",
                "steve",
                "Workspace conductor",
                "claude-code/opus",
                "t",
                "t",
            ),
        )
        db.commit()
        assert set_conductor("workspace", "workspace-conductor", db=db) is True
        thread_row = db.execute(
            "SELECT * FROM threads WHERE id='workspace-conductor'"
        ).fetchone()

        preview = build_next_turn_context_preview(
            "workspace-conductor",
            thread_row,
            db=db,
            current_user_content="coordinate the next turn",
        )
    finally:
        db.close()

    conductor_rows = [
        row
        for row in preview["context_snapshot"]["source_inventory"]
        if row["origin_section"] == "conductor"
    ]
    expected_tokens = sum(row["prompt_tokens"] or 0 for row in conductor_rows)
    # Per-origin attribution now lives on the snapshot inventory — the surface
    # the CTX sheet renders. Paid delivery counting is covered by
    # test_native_cold_render_delivers_and_counts_role_bytes_once.
    assert expected_tokens > 0


@pytest.mark.asyncio
async def test_native_cold_render_delivers_and_counts_role_bytes_once(
    isolated_db,
    monkeypatch,
    tmp_path,
):
    from core.conductor_templates import conductor_template_sources
    from core.context_builder import ContextBuilder
    from core.turn_context import prepare_turn_context
    from infra.database import get_db

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for role, filename, content in (
        ("agent", "AGENT.md", "NATIVE AGENT ROLE"),
        ("corsia_agent", "CORSIA_AGENT.md", "NATIVE CONDUCTOR ROLE"),
    ):
        path = workspace / "context" / "roles" / role / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    context = ContextBuilder(workspace)
    monkeypatch.setattr(context, "_get_identity", lambda: "NATIVE IDENTITY")
    monkeypatch.setattr(
        "llm.capabilities.get_model_context_window",
        lambda _model: 200_000,
    )

    db = get_db()
    try:
        _insert_session(db, "workspace-conductor")
        db.execute(
            "INSERT INTO settings (key,value,updated_at) VALUES (?,?,?)",
            (
                "corsia_agent_thread_id",
                json.dumps({"session_id": "workspace-conductor"}),
                "t",
            ),
        )
        db.commit()
        conductor_sources = conductor_template_sources("workspace-conductor", db=db)
        from core.role_context_sources import resolve_role_context

        role_context = resolve_role_context(
            thread_id="workspace-conductor",
            project_id=None,
            workspace=workspace,
            db=db,
            strict_db=True,
        )
    finally:
        db.close()

    role_sources = list(role_context.sources)
    sources = [role_sources[0], *conductor_sources, role_sources[1]]
    collection = SimpleNamespace(
        installed=None, project_cwd=None,
        agent_facts=role_context.facts(),
        sources=sources,
    )
    monkeypatch.setattr(
        "core.native_context_sources._context_sources_for_prefix",
        lambda *_args, **_kwargs: collection,
    )
    agent = SimpleNamespace(
        context=context,
        tools=SimpleNamespace(get_definitions=lambda: []),
        memory_window=100,
        max_tokens=1024,
    )

    messages, usage = await prepare_turn_context(
        agent=agent,
        thread_id="workspace-conductor",
        user_content="USER MESSAGE",
        turn_model="anthropic/claude-test",
        fresh_context=True,
        project_id=None,
        channel="web",
        chat_id="workspace-conductor",
        media=None,
    )

    inventory = usage["context_snapshot"]["source_inventory"]
    system_text = messages[0]["content"]
    for source in sources:
        assert system_text.count(source.prompt_content) == 1
        proofs = [
            row
            for row in inventory
            if row["source_id"] == source.source_id
            and row["inclusion"] == "proof"
            and row["prompt_tokens"] is None
        ]
        assert len(proofs) == 1
    assert usage["source_section_tokens"]["conductor"] > 0
    assert usage["source_section_tokens"]["role"] > 0
    delivered_sections = (
        usage["source_section_tokens"]["conductor"]
        + usage["source_section_tokens"]["role"]
    )
    assert usage["context_source_tokens"] == delivered_sections
    assert delivered_sections == sum(
        row["prompt_tokens"] or 0
        for row in inventory
        if row["origin_section"] in {"conductor", "role"}
    )


def test_native_structured_aggregate_orders_project_before_conductor_layer(
    tmp_path, monkeypatch,
):
    from core.conductor_templates import _load_template
    from core.context_builder import ContextBuilder
    from core.effective_context import ContextSourceEntry
    from core.native_system_context import _native_system_core

    context = ContextBuilder(tmp_path)
    context._get_identity = lambda: "SYSTEM"
    monkeypatch.setattr(
        "core.context_builder.installed_project_context",
        lambda _workspace, _project_id, _rows: "PROJECT",
    )
    conductor = _load_template(
        "core",
        [{"scope": "project-a", "scope_type": "project"}],
    )
    project = ContextSourceEntry(
        source_id="project-bootloader:project-a:context/PROJECT.md",
        origin_section="project",
        label="Project",
        stable_reference="/workspace/projects/a/context/PROJECT.md",
        freshness="fresh",
        inclusion="included",
        reason="project context",
        owner_surface="document-viewer",
        prompt_content="PROJECT",
    )
    prompt, rows, handled = _native_system_core(
        context,
        project_id="project-a",
        collection=SimpleNamespace(sources=[conductor, project]),
    )

    exact = [row for row in rows if row.inclusion == "included"]
    proofs = [row for row in rows if row.inclusion == "proof"]
    assert [row.origin_section for row in exact] == [
        "system",
        "project",
        "conductor",
    ]
    assert prompt.index("PROJECT") < prompt.index(conductor.prompt_content)
    assert [row.source_id for row in proofs] == [
        "project-bootloader:project-a:context/PROJECT.md",
        "conductor-template:core:v2",
    ]
    assert handled == {source.source_id for source in (conductor, project)}


def test_preflight_counts_the_same_packaged_conductor_sources(
    isolated_db,
    monkeypatch,
    tmp_path,
):
    from core.agent_context_sources import installed_owner_map
    from core.conductor_templates import conductor_template_sources
    from core.context_admission import check_context_preflight
    from infra.database import get_db

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    role_file = workspace / "context" / "roles" / "agent" / "AGENT.md"
    role_file.parent.mkdir(parents=True)
    role_file.write_text("PREFLIGHT AGENT ROLE", encoding="utf-8")
    monkeypatch.setattr("infra.config.WORKSPACE", workspace)
    db = get_db()
    try:
        _insert_session(db, "workspace-conductor")
        db.execute(
            "INSERT INTO settings (key,value,updated_at) VALUES (?,?,?)",
            (
                "corsia_agent_thread_id",
                json.dumps({"session_id": "workspace-conductor"}),
                "t",
            ),
        )
        db.commit()
        sources = conductor_template_sources("workspace-conductor", db=db)
        from core.role_context_sources import resolve_role_context

        role_sources = resolve_role_context(
            thread_id="workspace-conductor",
            project_id=None,
            workspace=workspace,
            db=db,
            strict_db=True,
        ).sources
        owner_map, _map, _adoption = installed_owner_map(
            thread_id="workspace-conductor", db=db, stored=None,
        )
        result = check_context_preflight(
            thread_id="workspace-conductor",
            model="anthropic/claude-test",
            current_user_content="USER MESSAGE",
            db=db,
            model_context_window=200_000,
        )
    finally:
        db.close()

    assert result.status == "allowed"
    assert result.source_section_tokens["conductor"] == sum(
        source.to_inventory_dict()["prompt_tokens"] or 0 for source in sources
    )
    assert result.source_section_tokens["role"] == sum(
        source.to_inventory_dict()["prompt_tokens"] or 0
        for source in (*role_sources, owner_map)
    )
