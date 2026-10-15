from __future__ import annotations

import hashlib
import json

import pytest

from tests.project_ref_helpers import project_ref


_TEMPLATE_TEXT = {
    "core": (
        "# Conductor operating contract\n\n"
        "You are the designated conductor for this scope. Maintain a coherent view of its purpose, current authority, work and unresolved decisions. Coordinate within the user's commission; designation never supplies new grants.\n\n"
        "Keep one accountable owner for each authorized whole outcome, including its integration and verification. Ownership remains with you where your commission includes an outcome that has not been explicitly assigned; an unowned objective does not authorize new work. Name the decision authority and contributors when a consequential outcome spans owners, and preserve each domain's constraints.\n\n"
        "Maintain the coordination model, responsibility boundaries, context/skill organization and supported integrations that reduce user attention. Use skill `conductor-bootstrap` when establishing or materially revising them. Prefer the smallest effective team; skill `agent-team-decomposition` defines the cut.\n\n"
        "Keep policy, responsibility context, plans, task briefs and references distinct. Refresh affected context after material changes. Keep source-checked scope notices dated with an expiry; remove stale notices rather than accumulating them. Route domain failures to their owners and mechanical gaps to the responsible product owner; more instructions cannot repair missing capabilities.\n"
    ),
    "corsia_agent": (
        "# CORSIA agent operating contract\n"
        "\n"
        "You are the CORSIA agent: the person's one workspace-level agent. Maintain a coherent view of the workspace's purpose, current authority, work and unresolved decisions. Coordinate within the user's commission; designation never supplies new grants.\n"
        "\n"
        "Keep one accountable owner for each authorized whole outcome, including its integration and verification. Ownership remains with you where your commission includes an outcome that has not been explicitly assigned; an unowned objective does not authorize new work. Name the decision authority and contributors when a consequential outcome spans owners, and preserve each domain's constraints.\n"
        "\n"
        "Maintain the coordination model, responsibility boundaries, context/skill organization and supported integrations that reduce user attention. Use skill `conductor-bootstrap` when establishing or materially revising them. Prefer the smallest effective team; skill `agent-team-decomposition` defines the cut.\n"
        "\n"
        "Keep policy, responsibility context, plans, task briefs and references distinct. Refresh affected context after material changes. Keep source-checked scope notices dated with an expiry; remove stale notices rather than accumulating them. Route domain failures to their owners and mechanical gaps to the responsible product owner; more instructions cannot repair missing capabilities.\n"
        "\n"
        "## Workspace responsibilities\n"
        "\n"
        "- Establish the workspace purpose, shared operating context and actual commissioning authority. Keep project-specific goals and execution inside projects.\n"
        "- Steward cross-project context, skills, document organization and coordination health. Check the health and availability signals the instance actually exposes; record missing observation or repair routes rather than claiming healthy by absence of evidence.\n"
        "- Route authorized work to the appropriate project or workspace owner and make cross-project dependencies visible. Use projects for distinct purposes, sources, participants or disclosure boundaries; use workspace agents for recurring cross-project stewardship. Scope choice grants no extra access.\n"
        "- Own the workspace category registry at `shared/INDEX.md`. Reuse adequate categories; create a category index and registry entry when classification is clear within authority. Category owners steward contents. Ask only for a material missing decision.\n"
        "- Review whether shared policy and team structure reduce setup, correction and attention costs while preserving verified outcomes. Preserve project-specific choices within the common invariants.\n"
        "- Own operations and cost: keep providers, models, tools and keys working; route work to models by cost per accepted result; review how agents perform (corrections, rework, cost per accepted result) and improve their context and skills, carrying what one agent or project learns to the rest. A project's coordination belongs to its own conductor.\n"
        "- Daily health check: on a wake whose payload kind is `corsia_agent.health_check`, call `model_sources` with `recheck: true` and run one `web_search` if a web search key is set. Send one `post_update` only when something needs the person (a sign-in expired, a provider at its limit with nothing else ready, a key refused), saying what and the one thing to do; otherwise say nothing.\n"
        "- Friction in CORSIA itself (a confusing step, a failure, a missing capability): when GitHub is set up and the person allowed agents to post, search CORSIA's open issues first and add to a match, otherwise file one. Give steps, expected vs actual and the CORSIA version. State technical facts only: no personal data, names, paths or conversation text, because the repository is public.\n"
    ),
    "project": (
        "# Project conductor responsibilities\n\n"
        "- Elicit and maintain the user's purpose, vision, position and authority, constraints, acceptance evidence and current initiatives. Keep the charter in `context/PROJECT.md` and operating projection in `context/COORDINATION-MODEL.md`; live status stays in plans.\n"
        "- Agree a review horizon and revisit alignment when objectives, evidence or constraints materially change. Resolve conflicting objectives with the user; propagate the resulting direction to affected agents.\n"
        "- Own measures of whole project outcomes and user attention, with quality and rework guardrails. Detect local optimization that damages the combined result. Review team effectiveness and adjust coordination within the commission; do not equate activity or agent count with value.\n"
        "- Maintain project context, responsibility design, integrations and triggered skills; do not force software workflows onto other activities or duplicate domain execution.\n"
        "- Own the category registry at `shared/INDEX.md`. Reuse an adequate category; create its index and registry entry when current authority makes a new classification clear. Category owners steward their contents and freshness. Ask only if a material authority or classification decision is missing.\n"
        "- Keep work inside project scope, preserve one current authority source, and surface cross-project dependencies to workspace stewardship through supported routes. Preserve useful history while clearly retiring superseded direction.\n"
        "- Friction in CORSIA itself (a confusing step, a failure, a missing capability): when GitHub is set up and the person allowed agents to post, search CORSIA's open issues first and add to a match, otherwise file one. Give steps, expected vs actual and the CORSIA version. State technical facts only: no personal data, names, paths or conversation text, because the repository is public.\n"
    ),
}

_TEMPLATE_SHA256 = {
    "core": "ee8f9e475e2e5b17b17886c3a4caacdf737279f30536391fed0b0720cc542e86",
    "corsia_agent": "9cc6a9e82aef61d6ec703ed12366cf6073b637b6e79a746392d8bf2ac1e817c4",
    "project": "d6e7b3af3b98c20666dae82b40646bfb84d86109c5374fcf7a740454a1c466ae",
}

_V1_TEMPLATE_SHA256 = {
    "core": "8e2eec04295859a2345e45dfde932e1d3f0301e5a0de08abf5008e9bca791303",
    "workspace": "b07749375874268b2974884c15c7cedce5a1caf9181615a129394d2fdffe1b29",
    "project": "08ae5728470a9d961979acfa00e0bc8282515d852318f98f1f82a74fc29a5ab7",
}


def _insert_session(db, thread_id: str, *, project_id: str | None = None) -> None:
    db.execute(
        "INSERT INTO threads "
        "(id,user_id,title,created_at,updated_at,project_id) VALUES (?,?,?,?,?,?)",
        (thread_id, "steve", thread_id, "t", "t", project_id),
    )


def _insert_project(db, project_id: str, name: str, path: str) -> None:
    db.execute(
        "INSERT INTO projects (id,name,path,created_at,updated_at) VALUES (?,?,?,?,?)",
        (project_id, name, path, "t", "t"),
    )


def _source_kinds(sources) -> list[str]:
    return [source.metadata["template_kind"] for source in sources]


def test_v1_template_resources_remain_present_and_byte_identical():
    from importlib import resources

    package = resources.files("prompts.conductor")
    for kind, expected_sha256 in _V1_TEMPLATE_SHA256.items():
        raw = package.joinpath(f"{kind}-v1.md").read_bytes()
        assert hashlib.sha256(raw).hexdigest() == expected_sha256


def test_packaged_templates_select_scope_dedupe_and_preserve_raw_provenance(
    isolated_db,
):
    from core.conductor_templates import conductor_template_sources
    from infra.database import get_db

    db = get_db()
    try:
        _insert_session(db, "ordinary")
        _insert_session(db, "workspace-only")
        _insert_session(db, "project-only", project_id="project-a")
        _insert_session(db, "combined", project_id="project-a")
        _insert_project(db, "project-a", "Project A", "/workspace/projects/a")
        _insert_project(db, "project-b", "Project B", "/workspace/projects/b")
        db.execute(
            "INSERT INTO settings (key,value,updated_at) VALUES (?,?,?)",
            (
                "corsia_agent_thread_id",
                json.dumps({"session_id": "workspace-only"}),
                "t",
            ),
        )
        db.execute(
            "UPDATE projects SET conductor_thread_id='project-only' WHERE id='project-a'"
        )
        db.commit()

        assert conductor_template_sources("ordinary", db=db) == []
        assert _source_kinds(conductor_template_sources("workspace-only", db=db)) == [
            "corsia_agent",
        ]
        assert _source_kinds(conductor_template_sources("project-only", db=db)) == [
            "core",
            "project",
        ]

        db.execute(
            "UPDATE settings SET value=? WHERE key='corsia_agent_thread_id'",
            (json.dumps({"session_id": "combined"}),),
        )
        db.execute("UPDATE projects SET conductor_thread_id='combined'")
        db.commit()
        combined = conductor_template_sources("combined", db=db)
    finally:
        db.close()

    assert _source_kinds(combined) == ["core", "project", "corsia_agent"]
    assert [source.source_id for source in combined] == [
        "conductor-template:core:v2",
        "conductor-template:project:v2",
        "corsia-agent-template:v2",
    ]
    for source in combined:
        kind = source.metadata["template_kind"]
        expected_bytes = _TEMPLATE_TEXT[kind].encode("utf-8")
        assert source.origin_section == "conductor"
        assert source.inclusion == "included"
        assert source.prompt_content == _TEMPLATE_TEXT[kind]
        assert source.metadata["template_version"] == "v2"
        assert hashlib.sha256(expected_bytes).hexdigest() == _TEMPLATE_SHA256[kind]
        assert source.metadata["raw_sha256"] == _TEMPLATE_SHA256[kind]
        assert source.metadata["raw_byte_count"] == len(expected_bytes)
        assert source.metadata["package"] == "prompts.conductor"
        assert source.metadata["resource"] == f"{kind.replace('_', '-')}-v2.md"


@pytest.mark.parametrize("kind", ["corsia_agent", "project"])
def test_scope_templates_assign_category_index_ownership(kind):
    from core.conductor_templates import _load_template

    source = _load_template(
        kind,
        [{"scope": kind, "scope_type": kind}],
    )
    content = source.prompt_content or ""

    assert "category registry at `shared/INDEX.md`" in content
    assert "Category owners steward" in content
    assert "Ask only" in content


def test_packaged_template_loading_rejects_non_utf8_bytes(isolated_db, monkeypatch):
    from core import conductor_templates
    from infra.database import get_db

    class _InvalidResource:
        def joinpath(self, _name):
            return self

        def read_bytes(self):
            return b"\xff"

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
        monkeypatch.setattr(
            conductor_templates.resources,
            "files",
            lambda _package: _InvalidResource(),
        )
        with pytest.raises(UnicodeDecodeError):
            conductor_templates.conductor_template_sources(
                "workspace-conductor",
                db=db,
            )
    finally:
        db.close()


def test_registry_orders_base_scope_then_conductor_thread_and_skills(
    isolated_db,
    tmp_path,
    monkeypatch,
):
    from core.dynamic_context_registry import collect_context_sources
    from infra.database import get_db
    from tests.project_ref_helpers import select_workspace

    workspace = tmp_path / "workspace"
    select_workspace(monkeypatch, workspace)
    project = workspace / "projects" / "demo"
    (workspace / "context").mkdir(parents=True)
    (workspace / "context" / "SYSTEM.md").write_text("system", encoding="utf-8")
    (project / "context").mkdir(parents=True)
    (project / "context" / "PROJECT.md").write_text("project", encoding="utf-8")
    (project / "context" / "COORDINATION-MODEL.md").write_text(
        "coordination", encoding="utf-8"
    )
    thread_context = project / ".threads" / "combined" / "context"
    thread_context.mkdir(parents=True)
    (thread_context / "THREAD.md").write_text("thread", encoding="utf-8")
    skill = project / "skills" / "audit" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: audit\ndescription: Use when auditing context.\n---\n# Audit\n",
        encoding="utf-8",
    )

    db = get_db()
    try:
        _insert_project(db, "project-a", "Demo", project_ref("demo"))
        _insert_session(db, "combined", project_id="project-a")
        db.execute(
            "INSERT INTO settings (key,value,updated_at) VALUES (?,?,?)",
            (
                "corsia_agent_thread_id",
                json.dumps({"session_id": "combined"}),
                "t",
            ),
        )
        db.execute(
            "UPDATE projects SET conductor_thread_id='combined' WHERE id='project-a'"
        )
        db.commit()
        collection = collect_context_sources(
            thread_id="combined",
            project_id="project-a",
            workspace=workspace,
            db=db,
        )
    finally:
        db.close()

    ordered = []
    for source in collection.sources:
        if source.origin_section in {"corsia", "system", "workspace", "conductor", "project", "thread"}:
            ordered.append(source.origin_section)
        elif source.metadata.get("provider") == "skills":
            ordered.append("skills")
    assert ordered == [
        "corsia",
        "corsia",
        "corsia",
        "corsia",
        "corsia",
        "corsia",
        "corsia",
        "corsia",
        "corsia",
        "corsia",
        "workspace",
        "project",
        "project",
        "conductor",
        "conductor",
        "conductor",
        "thread",
        "skills",
    ]


def _assert_exact_template_delivery(provider_text: str, inventory) -> None:
    template_rows = [
        row for row in inventory
        if row.source_id.startswith(("conductor-template:", "corsia-agent-template:"))
    ]
    assert [row.metadata["template_kind"] for row in template_rows] == [
        "core",
        "project",
        "corsia_agent",
    ]
    for row in template_rows:
        content = row.prompt_content
        assert content is not None
        assert provider_text.count(content) == 1
        owners = [
            candidate
            for candidate in inventory
            if candidate.inclusion in {"included", "summarized"}
            and candidate.prompt_content
            and content in candidate.prompt_content
        ]
        assert len(owners) == 1


def _assert_exact_role_delivery(provider_text: str, inventory) -> None:
    role_rows = [
        row for row in inventory if row.metadata.get("provider") == "role_context"
    ]
    assert [
        (row.metadata["role"], row.metadata["scope"])
        for row in role_rows
    ] == [
        ("agent", "workspace"),
        ("agent", "project"),
        ("conductor", "workspace"),
        ("conductor", "project"),
        ("corsia_agent", "workspace"),
    ]
    for row in role_rows:
        content = row.prompt_content
        assert content is not None
        assert provider_text.count(content) == 1
        owners = [
            candidate
            for candidate in inventory
            if candidate.source_id == row.source_id
            and candidate.inclusion in {"included", "summarized"}
        ]
        assert len(owners) == 1


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


def test_cc_and_codex_cold_reconstructions_share_collector_and_deliver_once(
    isolated_db,
    monkeypatch,
    tmp_path,
):
    from infra.database import get_db
    from subagents.cc_context_reconstruction import build_cc_context_reconstruction
    from subagents.codex_context_reconstruction import (
        build_codex_context_reconstruction,
    )
    from tests.project_ref_helpers import select_workspace

    workspace = tmp_path / "workspace"
    select_workspace(monkeypatch, workspace)
    project = workspace / "projects" / "demo"
    (project / "context").mkdir(parents=True)
    (project / "context" / "PROJECT.md").write_text("project", encoding="utf-8")
    (project / "context" / "COORDINATION-MODEL.md").write_text(
        "coordination", encoding="utf-8"
    )
    for root, role, filename, content in (
        (workspace, "agent", "AGENT.md", "CC CODEX GLOBAL AGENT"),
        (project, "agent", "AGENT.md", "CC CODEX PROJECT AGENT"),
        (workspace, "conductor", "CONDUCTOR.md", "CC CODEX GLOBAL CONDUCTOR"),
        (project, "conductor", "CONDUCTOR.md", "CC CODEX PROJECT CONDUCTOR"),
        (workspace, "corsia_agent", "CORSIA_AGENT.md", "CC CODEX CORSIA AGENT"),
    ):
        path = root / "context" / "roles" / role / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    monkeypatch.setattr("infra.config.WORKSPACE", workspace)

    db = get_db()
    try:
        _insert_project(db, "project-a", "Demo", project_ref("demo"))
        _insert_session(db, "combined", project_id="project-a")
        db.execute(
            "INSERT INTO settings (key,value,updated_at) VALUES (?,?,?)",
            (
                "corsia_agent_thread_id",
                json.dumps({"session_id": "combined"}),
                "t",
            ),
        )
        db.execute(
            "UPDATE projects SET conductor_thread_id='combined' WHERE id='project-a'"
        )
        db.commit()

        cc = build_cc_context_reconstruction(
            thread_id="combined",
            base_seed="CC BASE SEED",
            catalog_model_id="claude-code/opus",
            model_alias="opus",
            working_dir=str(project),
            project_id="project-a",
            is_subagent=False,
            db=db,
        )
        codex = build_codex_context_reconstruction(
            thread_id="combined",
            user_text="CODEX USER TASK",
            working_dir=str(project),
            project_id="project-a",
            model="codex/gpt-5-codex",
            history_projection=_empty_projection("combined"),
            db=db,
        )
        persisted_message_count = db.execute(
            "SELECT COUNT(*) AS count FROM messages WHERE thread_id='combined'"
        ).fetchone()["count"]
    finally:
        db.close()

    _assert_exact_template_delivery(cc.seed_text, cc.snapshot.source_inventory)
    _assert_exact_template_delivery(codex.task_text, codex.snapshot.source_inventory)
    _assert_exact_role_delivery(cc.seed_text, cc.snapshot.source_inventory)
    _assert_exact_role_delivery(codex.task_text, codex.snapshot.source_inventory)
    assert "CC BASE SEED" in cc.persisted_seed_text
    assert (
        f"--- CORSIA CONTEXT SNAPSHOT: {cc.snapshot_id} ---"
        in cc.persisted_seed_text
    )
    for template_text in _TEMPLATE_TEXT.values():
        assert template_text not in cc.persisted_seed_text
    cc_templates = [
        (row.source_id, row.prompt_content, row.metadata["raw_sha256"])
        for row in cc.snapshot.source_inventory
        if row.source_id.startswith(("conductor-template:", "corsia-agent-template:"))
    ]
    codex_templates = [
        (row.source_id, row.prompt_content, row.metadata["raw_sha256"])
        for row in codex.snapshot.source_inventory
        if row.source_id.startswith(("conductor-template:", "corsia-agent-template:"))
    ]
    assert cc_templates == codex_templates
    cc_roles = [
        (row.source_id, row.prompt_content, row.metadata)
        for row in cc.snapshot.source_inventory
        if row.metadata.get("provider") == "role_context"
    ]
    codex_roles = [
        (row.source_id, row.prompt_content, row.metadata)
        for row in codex.snapshot.source_inventory
        if row.metadata.get("provider") == "role_context"
    ]
    assert cc_roles == codex_roles
    from infra.context_usage_attribution import _snapshot_usage_sections

    for reconstruction in (cc, codex):
        snapshot = reconstruction.snapshot.to_dict()
        role_tokens = sum(
            row["prompt_tokens"] or 0
            for row in snapshot["source_inventory"]
            if row["origin_section"] == "role"
        )
        assert role_tokens > 0
        assert _snapshot_usage_sections(snapshot)["context_source_tokens"] >= role_tokens
    assert persisted_message_count == 0
