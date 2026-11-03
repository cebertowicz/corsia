from __future__ import annotations

import json
import shutil
from pathlib import PurePosixPath
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.system_tier import system_tier_manifest
from infra.system_package import system_package_dir


class _FakeContext:
    def build_system_prompt(self, project_id=None):
        return f"SYSTEM project={project_id or 'none'}"

    def build_messages(
        self,
        history,
        current_message,
        channel=None,
        chat_id=None,
        project_id=None,
        model=None,
        media=None,
    ):
        return [
            {"role": "system", "content": self.build_system_prompt(project_id)},
            *history,
            {"role": "user", "content": current_message},
        ]


class _FakeTools:
    def get_definitions(self):
        return []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("supports_reasoning", "expected_effort"),
    [(True, "medium"), (False, None)],
)
async def test_event_turn_setup_preserves_supported_reasoning_effort(
    monkeypatch, supports_reasoning, expected_effort,
):
    """Event ingress keeps the resolved effort unless its target cannot reason."""
    from core import agent as agent_module
    from core.turn_core import TurnIdentity
    from ws import turn as ws_turn

    async def get_agent():
        return SimpleNamespace(tools=_FakeTools())

    async def validate(*_args, **_kwargs):
        return None

    async def bind(*_args, **_kwargs):
        return None

    monkeypatch.setattr(agent_module, "get_agent", get_agent)
    monkeypatch.setattr(ws_turn, "validate_native_target", validate)
    monkeypatch.setattr(ws_turn, "make_provider_params", lambda _model: {})
    monkeypatch.setattr(ws_turn, "native_provider_params", lambda _target, _params: {})
    monkeypatch.setattr(ws_turn, "make_runtime_bindings", lambda *_args: (lambda *_a: None, lambda *_a: None))
    monkeypatch.setattr(ws_turn, "bind_preadmitted_turn", bind)
    target = SimpleNamespace(
        transport_model="omlx@test/model",
        display_name="Test model",
        backend="network",
        supports_reasoning=supports_reasoning,
    )

    prepared = await ws_turn._setup_turn(
        "event-effort", "input", [], target=target, turn_id="event-turn",
        ingress="event", reasoning_effort="medium", broadcast=AsyncMock(),
        preadmitted_turn=TurnIdentity("event-turn", 1),
    )

    assert prepared.reasoning_effort == expected_effort


def _empty_replay(thread_id: str):
    from core.context_replay import ReplaySelection
    from history.types import PortableHistoryProjection

    projection = PortableHistoryProjection(
        format_version=1,
        thread_id=thread_id,
        history_epoch=0,
        start_exclusive=None,
        sealed_boundary=None,
        turns=(),
        full_raw_replay_available=True,
    )
    return ReplaySelection(
        mode="exact",
        history_epoch=0,
        sealed_boundary=None,
        tail=projection,
    )


def _record_context_event_turn(
    db,
    *,
    thread_id: str,
    turn_id: str,
    event_id: str,
    content: str,
    disposition: str,
) -> None:
    from infra.queries import insert_message

    db.execute(
        "INSERT INTO active_turns "
        "(id,thread_id,execution_epoch,model,status,started_at,ended_at,boot_epoch) "
        "VALUES (?,?,0,'omlx/test','completed','start','end','boot')",
        (turn_id, thread_id),
    )
    insert_message(
        thread_id,
        "system",
        content,
        {
            "type": "context_event",
            "event_disposition": disposition,
            "turn_id": turn_id,
        },
        msg_id=event_id,
        db=db,
    )
    insert_message(
        thread_id,
        "assistant",
        f"acknowledged {event_id}",
        {"turn_id": turn_id},
        msg_id=f"answer-{event_id}",
        db=db,
    )


async def _native_project_context_usage(monkeypatch):
    from core.context_builder import SystemPromptComponent, SystemPromptRender
    from core.turn_context import prepare_turn_context

    class ProjectContext(_FakeContext):
        @staticmethod
        def render_system_prompt(project_id=None, kb_query=None):
            assert project_id == "project-ledger"
            assert kb_query is None
            components = (
                SystemPromptComponent(
                    component_id="identity",
                    origin_section="system",
                    label="Native identity",
                    stable_reference="corsia://agent/identity",
                    owner_surface="context-builder",
                    content="Native identity",
                ),
                SystemPromptComponent(
                    component_id="project:project-ledger",
                    origin_section="project",
                    label="Project context",
                    stable_reference="project://project-ledger/rendered-context",
                    owner_surface="context-builder",
                    content="\n\n---\n\nProject ledger rules",
                ),
            )
            return SystemPromptRender(
                content="".join(component.content for component in components),
                components=components,
            )

        def build_system_prompt(self, project_id=None):
            return self.render_system_prompt(project_id=project_id).content

    monkeypatch.setattr(
        "llm.capabilities.get_model_context_window",
        lambda _model: 200000,
    )
    monkeypatch.setattr(
        "core.native_context_sources._context_sources_for_prefix",
        lambda *_args, **_kwargs: SimpleNamespace(
            installed=None, project_cwd="/workspace/project-ledger",
            agent_facts={},
            sources=[],
        ),
    )
    agent = SimpleNamespace(
        context=ProjectContext(),
        tools=_FakeTools(),
        memory_window=100,
        max_tokens=1024,
    )

    _, usage = await prepare_turn_context(
        agent=agent,
        thread_id="native-project-ledger",
        user_content="inspect the project ledger",
        turn_model="anthropic/claude-test",
        fresh_context=True,
        project_id="project-ledger",
        channel="web",
        chat_id="native-project-ledger",
        media=None,
    )
    return usage


@pytest.mark.asyncio
async def test_native_unmeasured_terminal_keeps_non_overlapping_project_buckets(
    monkeypatch,
):
    from core.turn_measured_usage import context_usage_from_latest_call

    preflight = await _native_project_context_usage(monkeypatch)
    terminal = context_usage_from_latest_call(preflight, {})
    sections = terminal["source_section_tokens"]

    assert terminal["token_usage_available"] is False
    assert terminal["total_used"] is None
    assert terminal["utilization"] is None
    assert terminal["system_tokens"] == sections["system"]
    assert terminal["context_source_tokens"] == sections["project"]


def test_native_live_usage_does_not_walk_the_frozen_prompt_snapshot(monkeypatch):
    from core import turn_measured_usage

    monkeypatch.setattr(
        turn_measured_usage,
        "merge_context_usage_with_snapshot",
        lambda *_args, **_kwargs: pytest.fail(
            "live usage must not walk the frozen prompt snapshot"
        ),
    )
    live = turn_measured_usage.context_usage_from_latest_call(
        {
            "model_context_window": 262_000,
            "context_snapshot": {"frozen_prompt_material": {"history": ["large"]}},
            "source_artifacts": [{"content": "large"}],
        },
        {"prompt_tokens": 123_000},
        include_context_details=False,
    )

    assert live["total_used"] == 123_000
    assert "context_snapshot" not in live
    assert "source_artifacts" not in live


def test_context_builder_structured_render_is_the_native_ledger_authority(
    monkeypatch,
    tmp_path,
):
    from core.context_builder import ContextBuilder
    from core.effective_context import ContextSourceEntry
    from core.token_estimation import _estimate_tokens
    from core.native_system_context import (
        _native_system_core,
        _retoken_native_system_components,
    )
    from core.system_tier import system_tier_sources

    context = ContextBuilder(tmp_path)
    monkeypatch.setattr(context, "_get_identity", lambda: "IDENTITY")
    monkeypatch.setattr(context, "_load_workspace_files", lambda: "BOOTSTRAP")
    monkeypatch.setattr(context, "_load_project_context", lambda _project: "PROJECT")
    monkeypatch.setattr(
        "core.context_builder.installed_project_context",
        lambda _workspace, _project, _rows: "PROJECT",
    )
    collection = SimpleNamespace(sources=[
        *system_tier_sources((), workspace=tmp_path),
        ContextSourceEntry(
            source_id="workspace-context:AGENTS.md",
            origin_section="workspace",
            label="AGENTS.md",
            stable_reference="/workspace/AGENTS.md",
            freshness="fresh",
            inclusion="included",
            reason="system bootstrap file",
            owner_surface="document-viewer",
            prompt_content="BOOTSTRAP",
        ),
        ContextSourceEntry(
            source_id="project-bootloader:p1:PROJECT.md",
            origin_section="project",
            label="PROJECT.md",
            stable_reference="/workspace/PROJECT.md",
            freshness="fresh",
            inclusion="included",
            reason="project bootstrap file",
            owner_surface="document-viewer",
            prompt_content="PROJECT",
        ),
    ])

    render = context.render_system_prompt(project_id="p1")
    prompt, rows, handled = _native_system_core(
        context,
        project_id="p1",
        collection=collection,
    )

    assert context.build_system_prompt(project_id="p1") == render.content
    assert prompt == render.content
    assert {
        "workspace-context:AGENTS.md",
        "project-bootloader:p1:PROJECT.md",
    }.issubset(handled)
    assert any(source_id.startswith("corsia:v1:") for source_id in handled)
    exact_rows = [row for row in rows if row.inclusion == "included"]
    proof_rows = [row for row in rows if row.inclusion == "proof"]
    assert [row.prompt_content for row in exact_rows] == [
        component.content for component in render.components
    ]
    assert exact_rows[0].source_id == "native:system:corsia"
    assert "native:workspace" in {row.source_id for row in exact_rows}
    assert {row.source_id for row in proof_rows} == handled
    assert all(row.to_inventory_dict()["prompt_tokens"] is None for row in proof_rows)
    rows = _retoken_native_system_components(rows, system_prompt=prompt)
    assert sum(
        int(row.to_inventory_dict().get("prompt_tokens") or 0)
        for row in rows if row.inclusion == "included"
    ) == _estimate_tokens(render.content) + 4


@pytest.mark.asyncio
async def test_real_send_prefix_consumes_effective_snapshot_for_native(monkeypatch, tmp_path):
    from core.native_context_prefix import build_real_send_prefix

    monkeypatch.setattr(
        "llm.capabilities.get_model_context_window",
        lambda _model: 200000,
    )
    workspace = tmp_path / "empty-workspace"
    workspace.mkdir()
    context = _FakeContext()
    context.workspace = workspace

    agent = SimpleNamespace(
        context=context,
        tools=_FakeTools(),
        memory_window=100,
        max_tokens=1024,
    )

    prefix, info = await build_real_send_prefix(
        agent=agent,
        thread_id="sess-native-1",
        turn_model="anthropic/claude-test",
        fresh_context=True,
        project_id="corsia",
    )

    snapshot = info["effective_context_snapshot"]
    native_payload = snapshot["frozen_prompt_material"]["provider_payload"]

    assert prefix == native_payload["native_prefix_messages"]
    assert prefix[0]["role"] == "system"
    assert prefix[0]["content"].startswith("SYSTEM project=corsia")
    assert info["snapshot_id"] == snapshot["snapshot_id"]
    assert info["content_hash"] == snapshot["content_hash"]
    assert snapshot["scope_facts"]["project_id"] == "corsia"
    assert snapshot["harness_model_facts"]["harness"] == "native"
    assert snapshot["harness_model_facts"]["model"] == "anthropic/claude-test"
    assert snapshot["delivery_leaves"][0]["harness"] == "native"
    assert snapshot["delivery_leaves"][0]["destination_mode"] == "prefix-snapshot"
    assert snapshot["source_inventory"][0]["origin_section"] == "corsia"
    assert any(
        row["source_id"] == "native:system:core"
        for row in snapshot["source_inventory"]
    )
    skills_component = next(
        row
        for row in snapshot["source_inventory"]
        if row["source_id"] == "native:skills-index"
    )
    assert skills_component["inclusion"] == "included"
    assert skills_component["prompt_tokens"] > 0
    system_skills_index = next(
        row
        for row in snapshot["source_inventory"]
        if row["source_id"] == "dynamic:skills-index"
    )
    assert system_skills_index["metadata"]["provider"] == "skills"
    assert system_skills_index["metadata"]["system_unavailable"] is None
    # Listed System skills are read from the workspace projection.
    assert system_skills_index["metadata"]["skill_count"] > 0
    assert str(workspace / ".corsia") in prefix[0]["content"]
    assert str(system_package_dir()) not in prefix[0]["content"]
    packaged_names = {
        PurePosixPath(item.path).parts[1]
        for item in system_tier_manifest().files
        if item.kind == "skill"
    }
    delivered = [name for name in packaged_names if f"- {name}:" in prefix[0]["content"]]
    assert delivered, "the Native prefix must list the packaged System skills"
    from core.token_estimation import _estimate_tokens

    rendered_system_tokens = _estimate_tokens(prefix[0]["content"]) + 4
    assert info["system_tokens"] == rendered_system_tokens
    inventoried_system_tokens = sum(
        int(row.get("prompt_tokens") or 0)
        for row in snapshot["source_inventory"]
        # The exact Native system message carries the context components and,
        # since System skills are packaged, the rendered skills component too.
        if row["origin_section"] in {"system", "project", "dynamic"}
        and row["inclusion"] == "included"
    )
    assert inventoried_system_tokens == rendered_system_tokens
    assert not any(
        row["source_id"] == "native-system-prompt"
        for row in snapshot["source_inventory"]
    )


@pytest.mark.asyncio
async def test_real_send_prefix_surfaces_unreadable_system_skills(monkeypatch, tmp_path):
    """Unreadable System skills stay visible as one fixed-text row."""
    from core.native_context_prefix import build_real_send_prefix

    package = tmp_path / "package-without-skills"
    shutil.copytree(system_package_dir(), package, ignore=shutil.ignore_patterns("skills"))
    monkeypatch.setattr("infra.system_package._PACKAGE_DIR", package)
    monkeypatch.setattr(
        "llm.capabilities.get_model_context_window",
        lambda _model: 200000,
    )
    workspace = tmp_path / "broken-workspace"
    workspace.mkdir()
    context = _FakeContext()
    context.workspace = workspace
    agent = SimpleNamespace(
        context=context,
        tools=_FakeTools(),
        memory_window=100,
        max_tokens=1024,
    )

    _, info = await build_real_send_prefix(
        agent=agent,
        thread_id="sess-native-broken",
        turn_model="anthropic/claude-test",
        fresh_context=True,
        project_id="corsia",
    )

    snapshot = info["effective_context_snapshot"]
    skills_index = next(
        row
        for row in snapshot["source_inventory"]
        if row["source_id"] == "dynamic:skills-index"
    )
    assert skills_index["metadata"]["system_unavailable"] == "System skills unavailable"
    assert any(
        row["source_id"] == "skill-error:system:unavailable"
        and row["reason"] == "System skills unavailable"
        for row in snapshot["source_inventory"]
    )


@pytest.mark.asyncio
async def test_native_skills_are_injected_and_counted_once(monkeypatch):
    from core.effective_context import ContextSourceEntry
    from core.token_estimation import _estimate_tokens
    from core.native_context_prefix import build_real_send_prefix

    monkeypatch.setattr(
        "llm.capabilities.get_model_context_window",
        lambda _model: 200000,
    )
    skill_text = "- audit: inspect the exact ledger (/skills/audit/SKILL.md)"
    collection = SimpleNamespace(
        installed=None, project_cwd=None,
        agent_facts={},
        sources=[
            ContextSourceEntry(
                source_id="dynamic:skills-index",
                origin_section="dynamic",
                label="Skills",
                stable_reference="thread://skills",
                freshness="fresh",
                inclusion="included",
                reason="resolved skills index",
                owner_surface="skills",
                prompt_content=skill_text,
                metadata={"provider": "skills", "skill_count": 1},
            )
        ],
    )
    monkeypatch.setattr(
        "core.native_context_sources._context_sources_for_prefix",
        lambda *_args, **_kwargs: collection,
    )
    agent = SimpleNamespace(
        context=_FakeContext(),
        tools=_FakeTools(),
        memory_window=100,
        max_tokens=1024,
    )

    prefix, info = await build_real_send_prefix(
        agent=agent,
        thread_id="native-skills",
        turn_model="anthropic/claude-test",
        fresh_context=True,
        project_id=None,
    )

    assert prefix[0]["content"].count(skill_text) == 1
    assert "[Skills]" in prefix[0]["content"]
    inventory = info["effective_context_snapshot"]["source_inventory"]
    exact_skill = next(
        row for row in inventory if row["source_id"] == "native:skills-index"
    )
    component_skill = next(
        row for row in inventory if row["source_id"] == "dynamic:skills-index"
    )
    assert exact_skill["inclusion"] == "included"
    assert exact_skill["prompt_tokens"] > 0
    assert component_skill["inclusion"] == "proof"
    assert component_skill["prompt_tokens"] is None
    assert sum(
        int(row.get("prompt_tokens") or 0)
        for row in inventory
        if row["source_id"].startswith("native:system:")
        or row["source_id"] == "native:skills-index"
    ) == _estimate_tokens(prefix[0]["content"]) + 4


@pytest.mark.asyncio
async def test_native_agent_send_framing_is_counted_as_managed_provider_input(
    monkeypatch,
):
    from core.message_history import render_agent_envelope
    from core.token_estimation import _estimate_tokens
    from core.turn_context import prepare_turn_context

    monkeypatch.setattr(
        "llm.capabilities.get_model_context_window",
        lambda _model: 200000,
    )
    monkeypatch.setattr(
        "core.native_context_sources._context_sources_for_prefix",
        lambda *_args, **_kwargs: SimpleNamespace(
            installed=None, project_cwd=None,
            agent_facts={},
            sources=[],
        ),
    )
    sender_text = "inspect the exact provider ledger"
    envelope = render_agent_envelope(
        sender_text,
        {
            "sender_kind": "agent",
            "from_thread": "sender-thread",
            "from_title": "Planner",
        },
    )
    agent = SimpleNamespace(
        context=_FakeContext(),
        tools=_FakeTools(),
        memory_window=100,
        max_tokens=1024,
    )

    messages, usage = await prepare_turn_context(
        agent=agent,
        thread_id="native-agent-envelope",
        user_content=envelope.content,
        turn_model="anthropic/claude-test",
        fresh_context=True,
        project_id=None,
        channel="web",
        chat_id="native-agent-envelope",
        media=None,
        user_source_content=sender_text,
        agent_envelope=envelope,
    )

    assert messages[-1]["content"] == envelope.content
    assert usage["user_tokens"] == _estimate_tokens(sender_text)
    expected_framing_tokens = (
        _estimate_tokens(envelope.content)
        - _estimate_tokens(envelope.body_content)
    )
    inventory = usage["context_snapshot"]["source_inventory"]
    framing_rows = [
        row for row in inventory
        if row["source_id"] == "native:agent-send-envelope"
    ]
    assert len(framing_rows) == 1
    assert framing_rows[0]["origin_section"] == "provider"
    assert framing_rows[0]["prompt_tokens"] == expected_framing_tokens
    assert usage["source_section_tokens"]["provider"] == expected_framing_tokens
    assert usage["context_source_tokens"] == expected_framing_tokens


@pytest.mark.asyncio
async def test_prepare_turn_context_reports_effective_snapshot_id(monkeypatch):
    from core.turn_context import prepare_turn_context

    monkeypatch.setattr(
        "llm.capabilities.get_model_context_window",
        lambda _model: 200000,
    )

    agent = SimpleNamespace(
        context=_FakeContext(),
        tools=_FakeTools(),
        memory_window=100,
        max_tokens=1024,
    )

    messages, usage = await prepare_turn_context(
        agent=agent,
        thread_id="sess-native-2",
        user_content="hello",
        turn_model="anthropic/claude-test",
        fresh_context=True,
        project_id=None,
        channel="web",
        chat_id="chat-1",
        media=None,
    )

    assert messages[0]["role"] == "system"
    assert usage["snapshot_id"].startswith("ctx-")
    assert len(usage["context_snapshot"]["content_hash"]) == 64
    assert usage["context_snapshot"]["delivery_leaves"][0]["harness"] == "native"
    assert usage["continuity_mode"] == "full"
    assert usage["source_artifacts"][0]["kind"] == "provider-bound-payload"
    assert usage["source_artifacts"][0]["harness"] == "native"
    assert usage["source_artifacts"][0]["method"] == "native-prefix"
    assert usage["source_artifacts"][0]["snapshot_id"] == usage["snapshot_id"]
    assert usage["source_artifacts"][1]["kind"] == "provider-bound-payload"
    assert usage["source_artifacts"][1]["harness"] == "native"
    assert usage["source_artifacts"][1]["method"] == "native-turn-messages"
    assert usage["source_artifacts"][1]["snapshot_id"] == usage["snapshot_id"]
    assert usage["source_artifacts"][1]["payload"]["messages"] == messages
    assert (
        usage["context_snapshot"]["frozen_prompt_material"]["current_user_content"]
        == "hello"
    )


@pytest.mark.asyncio
async def test_native_replay_preserves_canonical_turn_and_assistant_step_shape(
    isolated_db,
    monkeypatch,
):
    from core.context_replay import ReplaySelection
    from history.types import (
        HistoryBoundary,
        PortableHistoryProjection,
        PortableMessage,
        PortableTurn,
    )
    from core.turn_context import prepare_turn_context
    from infra.database import get_db
    from infra.time import utc_now_iso

    monkeypatch.setattr(
        "llm.capabilities.get_model_context_window",
        lambda _model: 200000,
    )
    now = utc_now_iso()
    db = get_db()
    db.execute(
        "INSERT INTO threads (id, user_id, title, created_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        ("native-replay", "steve", "Native replay", now, now),
    )
    db.commit()
    db.close()

    def _message(
        message_id: str,
        seq: int,
        turn_id: str,
        role: str,
        content: str,
        *,
        assistant_step_id: str | None = None,
    ) -> PortableMessage:
        return PortableMessage(
            message_id=message_id,
            seq=seq,
            turn_id=turn_id,
            role=role,
            content=content,
            created_at=now,
            assistant_step_id=assistant_step_id,
        )

    first_boundary = HistoryBoundary("native-replay", 0, 3, "assistant-2")
    final_boundary = HistoryBoundary("native-replay", 0, 4, "user-2")
    projection = PortableHistoryProjection(
        format_version=1,
        thread_id="native-replay",
        history_epoch=0,
        start_exclusive=None,
        sealed_boundary=final_boundary,
        turns=(
            PortableTurn(
                turn_id="turn-1",
                status="completed",
                messages=(
                    _message("user-1", 1, "turn-1", "user", "Begin the task."),
                    _message(
                        "assistant-1", 2, "turn-1", "assistant",
                        "First Assistant Step.", assistant_step_id="assistant-1",
                    ),
                    _message(
                        "assistant-2", 3, "turn-1", "assistant",
                        "Second Assistant Step.", assistant_step_id="assistant-2",
                    ),
                ),
                boundary=first_boundary,
            ),
            PortableTurn(
                turn_id="turn-2",
                status="cancelled",
                messages=(_message(
                    "user-2", 4, "turn-2", "user", "Do not deploy on Friday.",
                ),),
                boundary=final_boundary,
            ),
        ),
        full_raw_replay_available=True,
    )
    replay = ReplaySelection(
        mode="exact",
        history_epoch=0,
        sealed_boundary=projection.sealed_boundary,
        tail=projection,
    )
    agent = SimpleNamespace(
        context=_FakeContext(),
        tools=_FakeTools(),
        memory_window=100,
        max_tokens=1024,
    )

    messages, _usage = await prepare_turn_context(
        agent=agent,
        thread_id="native-replay",
        user_content="Continue from that history.",
        turn_model="anthropic/claude-test",
        fresh_context=False,
        project_id=None,
        channel="web",
        chat_id="native-replay",
        media=None,
        replay_selection=replay,
    )

    assert [message["role"] for message in messages] == [
        "system",
        "user",
        "assistant",
        "assistant",
        "user",
        "user",
    ]
    assert messages[2]["content"] == "First Assistant Step."
    assert messages[3]["content"] == "Second Assistant Step."
    assert messages[4]["content"] == "Do not deploy on Friday."


@pytest.mark.parametrize(
    ("routed_model", "replays_signed_block"),
    [
        ("anthropic/claude-fable-5", True),
        ("anthropic/claude-opus-5", False),
    ],
)
@pytest.mark.asyncio
async def test_native_external_anthropic_replay_uses_routed_effective_model(
    isolated_db,
    monkeypatch,
    routed_model,
    replays_signed_block,
):
    from core.context_replay import ReplaySelection
    from history.types import (
        HistoryBoundary,
        PortableHistoryProjection,
        PortableMessage,
        PortableTurn,
    )
    from core.turn_context import prepare_turn_context
    from infra.database import get_db
    from infra.time import utc_now_iso

    monkeypatch.setattr(
        "llm.capabilities.native_transport_identity",
        lambda _model: tuple(routed_model.split("/", 1)),
    )
    monkeypatch.setattr(
        "llm.capabilities.get_model_context_window",
        lambda _model: 200000,
    )
    now = utc_now_iso()
    db = get_db()
    db.execute(
        "INSERT INTO threads (id, user_id, title, created_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        ("native-external-replay", "steve", "External replay", now, now),
    )
    db.commit()
    db.close()

    boundary = HistoryBoundary(
        "native-external-replay", 0, 2, "assistant-signed",
    )
    projection = PortableHistoryProjection(
        format_version=1,
        thread_id="native-external-replay",
        history_epoch=0,
        start_exclusive=None,
        sealed_boundary=boundary,
        turns=(PortableTurn(
            turn_id="turn-signed",
            status="completed",
            messages=(
                PortableMessage(
                    message_id="user-signed",
                    seq=1,
                    turn_id="turn-signed",
                    role="user",
                    content="Begin.",
                    created_at=now,
                ),
                PortableMessage(
                    message_id="assistant-signed",
                    seq=2,
                    turn_id="turn-signed",
                    role="assistant",
                    content="Answer.",
                    created_at=now,
                    assistant_step_id="assistant-signed",
                    reasoning_text="Thought.",
                    reasoning_provenance={
                        "source_harness": "native",
                        "source_provider": "anthropic",
                        "source_effective_model": "claude-fable-5",
                        "representation_kind": "anthropic_thinking",
                        "native_blocks": [{
                            "type": "thinking",
                            "thinking": "Thought.",
                            "signature": "signed-thought",
                        }],
                    },
                ),
            ),
            boundary=boundary,
        ),),
        full_raw_replay_available=True,
    )
    replay = ReplaySelection(
        mode="exact",
        history_epoch=0,
        sealed_boundary=boundary,
        tail=projection,
    )
    agent = SimpleNamespace(
        context=_FakeContext(),
        tools=_FakeTools(),
        memory_window=100,
        max_tokens=1024,
    )

    messages, _usage = await prepare_turn_context(
        agent=agent,
        thread_id="native-external-replay",
        user_content="Continue.",
        turn_model="external@server/claude-fable-5",
        fresh_context=False,
        project_id=None,
        channel="web",
        chat_id="native-external-replay",
        media=None,
        replay_selection=replay,
    )

    assistant_content = messages[2]["content"]
    if replays_signed_block:
        assert assistant_content == [
            {
                "type": "thinking",
                "thinking": "Thought.",
                "signature": "signed-thought",
            },
            {"type": "text", "text": "Answer."},
        ]
    else:
        assert assistant_content == "Answer."


@pytest.mark.asyncio
async def test_external_anthropic_response_persists_and_replays_transport_identity(
    isolated_db,
    monkeypatch,
):
    """The persisted source identity comes from the resolved wire dialect.

    This drives an External catalog target through Native setup, Anthropic
    response parsing, common step persistence, and portable replay.  A
    hand-authored provenance row would not catch the catalog-provider versus
    transport-provider mismatch this guards.
    """
    from core import agent as agent_module
    from core import turn_provider_call, turns_steps
    from history.projection import render_portable_history
    from history.adapters import to_native_history
    from core.turn_core import BOOT_EPOCH, persist_turn_terminal
    from infra.database import get_db
    from infra.input_writes import begin_batch_write, settle_batch_write
    from infra.thread_driver import admit_input
    from infra.time import utc_now_iso
    from llm.providers.anthropic import _parse_response
    from llm.resolution import ExecutionTarget
    from ws import turn as ws_turn
    thread_id = "native-external-response-replay"
    turn_id = "native-external-response-turn"
    now = utc_now_iso()
    db = get_db()
    db.execute(
        "INSERT INTO threads "
        "(id, user_id, title, model, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (
            thread_id,
            "steve",
            "External response replay",
            "external@server/claude-fable-5",
            now,
            now,
        ),
    )
    db.commit()
    admission = admit_input(
        thread_id,
        role="user",
        content="Begin.",
        intent="apply_now",
        message_id="external-user",
        turn_id=turn_id,
        boot_epoch=BOOT_EPOCH,
        admitted_at=now,
        db=db,
    )
    assert admission.batch is not None
    db.close()

    agent = SimpleNamespace(
        context=_FakeContext(),
        tools=_FakeTools(),
        memory_window=100,
        temperature=0.3,
        max_tokens=1024,
    )
    async def _get_agent():
        return agent

    monkeypatch.setattr(agent_module, "get_agent", _get_agent)

    async def _validate(*_args, **_kwargs):
        return None

    monkeypatch.setattr(ws_turn, "validate_native_target", _validate)
    monkeypatch.setattr(
        ws_turn,
        "make_provider_params",
        lambda _model: {
            "model": "anthropic/claude-fable-5",
            "api_key": "sk-noauth",
            "api_base": "http://external.invalid",
            "uses_streaming": False,
        },
    )
    target = ExecutionTarget(
        catalog_id="external/claude-fable-5",
        harness="native",
        provider="external",
        backend="network",
        transport_model="external@server/claude-fable-5",
        expected_reported_model=None,
        context_window=200_000,
        available=True,
        availability_reason=None,
        display_name="External Fable",
        supports_reasoning=True,
    )

    (
        _agent,
        _active_model,
        provider_params,
        _cancel_event,
        identity,
        (_bound_send, send_cell),
        handler,
        _agent_content,
        _image_paths,
        _unbind_turn,
        _effort,
    ) = await ws_turn._setup_turn(
        thread_id,
        "Begin.",
        [],
        target=target,
        turn_id=turn_id,
        ingress="delegation",
    )

    async def _send(_message):
        return None

    send_cell[0] = _send

    begin_batch_write(
        admission.batch,
        attempt_id="external-write",
        started_at=now,
    )
    settle_batch_write("external-write", outcome="accepted")

    response = _parse_response(
        SimpleNamespace(
            model="claude-fable-5",
            content=[
                SimpleNamespace(
                    type="thinking",
                    thinking="Transport-native thought.",
                    signature="external-signed-thought",
                ),
                SimpleNamespace(type="text", text="Answer."),
            ],
            stop_reason="end_turn",
            usage=SimpleNamespace(
                input_tokens=1,
                output_tokens=1,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            ),
        ),
        model="anthropic/claude-fable-5",
    )
    async def _completion(**_kwargs):
        return response

    monkeypatch.setattr(turn_provider_call, "chat_completion", _completion)
    actual, _ = await turn_provider_call.call_llm(
        agent,
        [{"role": "user", "content": "Begin."}],
        [],
        target.transport_model,
        provider_params,
        thread_id,
        _cancel_event,
        handler.handle,
        1,
    )
    await turns_steps.emit_reasoning(actual, handler.handle)
    await handler.handle("text", {"content": actual.content})
    persist_turn_terminal(turn_id, "completed")

    db = get_db()
    try:
        assistant = db.execute(
            "SELECT metadata FROM messages "
            "WHERE thread_id = ? AND role = 'assistant'",
            (thread_id,),
        ).fetchone()
        provenance = json.loads(assistant["metadata"])["reasoning_provenance"]
        projection = render_portable_history(thread_id, db=db)
    finally:
        db.close()

    _unbind_turn(identity)
    assert identity.turn_id == turn_id
    assert provider_params["source_provider"] == "anthropic"
    assert provenance["source_provider"] == "anthropic"
    replay = to_native_history(
        projection,
        destination_provider="anthropic",
        destination_effective_model="claude-fable-5",
    )
    assistant_content = next(
        message["content"] for message in replay if message["role"] == "assistant"
    )
    assert assistant_content == [
        {
            "type": "thinking",
            "thinking": "Transport-native thought.",
            "signature": "external-signed-thought",
        },
        {"type": "text", "text": "Answer."},
    ]


@pytest.mark.asyncio
async def test_real_send_prefix_snapshot_records_project_cwd_and_source_root(
    isolated_db,
    monkeypatch,
    tmp_path,
):
    from core.native_context_prefix import build_real_send_prefix
    from infra.database import get_db
    from infra.time import utc_now_iso
    from tests.registry_helpers import seed_artifact

    monkeypatch.setattr(
        "llm.capabilities.get_model_context_window",
        lambda _model: 200000,
    )

    from infra import config
    from infra.workspace_paths import WorkspaceReference

    project_dir = config.WORKSPACE / "projects" / "scope-project"
    project_dir.mkdir(parents=True)
    (project_dir / "context").mkdir()
    (project_dir / "context" / "PROJECT.md").write_text("project", encoding="utf-8")
    (project_dir / "context" / "COORDINATION-MODEL.md").write_text(
        "coordination", encoding="utf-8"
    )
    now = utc_now_iso()
    db = get_db()
    db.execute(
        "INSERT INTO projects (id, name, path, created_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (
            "scope-proj",
            "Scope Project",
            WorkspaceReference.from_path(
                project_dir, config.WORKSPACE_SELECTION
            ).serialize(),
            now,
            now,
        ),
    )
    db.execute(
        "INSERT INTO threads (id, user_id, title, project_id, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        ("scope-current", "steve", "Scoped turn", "scope-proj", now, now),
    )
    seed_artifact(
        db,
        artifact_id="art-native",
        integration="corsia",
        type="document",
        external_id="docs/native.md",
        parent_thread_id="scope-current",
        title="Native Doc",
        description="Native context artifact",
        path=str(project_dir / "PROJECT.md"),
        metadata="{}",
        now=now,
    )
    db.commit()
    db.close()

    agent = SimpleNamespace(
        context=_FakeContext(),
        tools=_FakeTools(),
        memory_window=100,
        max_tokens=1024,
    )

    _prefix, info = await build_real_send_prefix(
        agent=agent,
        thread_id="scope-current",
        turn_model="anthropic/claude-test",
        fresh_context=True,
        project_id="scope-proj",
    )

    snapshot = info["effective_context_snapshot"]
    assert snapshot["cwd"] == str(project_dir)
    assert snapshot["source_root"] == str(project_dir)
    assert snapshot["scope_facts"]["cwd"] == str(project_dir)
    assert snapshot["scope_facts"]["source_root"] == str(project_dir)
    source_ids = {
        source["source_id"] for source in snapshot["source_inventory"]
    }
    assert "dynamic:artifacts-table" not in source_ids


@pytest.mark.parametrize("disposition", ["wake", "passive"])
@pytest.mark.asyncio
async def test_native_projects_canonical_context_event_exactly_once(
    isolated_db, monkeypatch, disposition,
):
    from core.context_replay import ReplaySelection
    from core.turn_context import prepare_turn_context
    from history.projection import render_portable_history
    from infra.database import get_db
    from infra.time import utc_now_iso

    event_token = f"CANONICAL_{disposition.upper()}_EVENT"
    monkeypatch.setattr(
        "llm.capabilities.get_model_context_window",
        lambda _model: 4096,
    )

    db = get_db()
    now = utc_now_iso()
    db.execute(
        "INSERT INTO threads (id, user_id, title, project_id, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        ("event-current", "steve", "Event Current", None, now, now),
    )
    _record_context_event_turn(
        db,
        thread_id="event-current",
        turn_id="event-turn",
        event_id="context-event",
        content=event_token,
        disposition=disposition,
    )
    db.commit()
    projection = render_portable_history("event-current", db=db)
    db.close()

    replay = ReplaySelection(
        mode="exact",
        history_epoch=projection.history_epoch,
        sealed_boundary=projection.sealed_boundary,
        tail=projection,
    )
    agent = SimpleNamespace(
        context=_FakeContext(),
        tools=_FakeTools(),
        memory_window=100,
        max_tokens=512,
    )

    messages, usage = await prepare_turn_context(
        agent=agent,
        thread_id="event-current",
        user_content="continue after the event",
        turn_model="anthropic/claude-test",
        fresh_context=False,
        project_id=None,
        channel="web",
        chat_id="event-current",
        media=None,
        replay_selection=replay,
    )

    assert event_token not in messages[0]["content"]
    assert json.dumps(messages).count(event_token) == 1
    prefix_payload = usage["source_artifacts"][0]["payload"]["messages"]
    turn_payload = usage["source_artifacts"][1]["payload"]["messages"]
    assert json.dumps(prefix_payload).count(event_token) == 1
    assert json.dumps(turn_payload).count(event_token) == 1


@pytest.mark.asyncio
async def test_native_checkpoint_tail_excludes_covered_event_and_projects_tail_event_once(
    isolated_db, monkeypatch,
):
    from core.context_replay import ReplaySelection
    from core.turn_context import prepare_turn_context
    from history.projection import render_portable_history
    from infra.database import get_db
    from infra.time import utc_now_iso

    monkeypatch.setattr("llm.capabilities.get_model_context_window", lambda _model: 4096)
    db = get_db()
    now = utc_now_iso()
    db.execute(
        "INSERT INTO threads (id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?)",
        ("event-tail", "steve", "Event Tail", now, now),
    )
    _record_context_event_turn(
        db, thread_id="event-tail", turn_id="before-turn", event_id="before-event",
        content="EVENT_COVERED_BY_CHECKPOINT", disposition="wake",
    )
    db.commit()
    boundary = render_portable_history("event-tail", db=db).sealed_boundary
    _record_context_event_turn(
        db, thread_id="event-tail", turn_id="after-turn", event_id="after-event",
        content="EVENT_AFTER_CHECKPOINT", disposition="passive",
    )
    db.commit()
    tail = render_portable_history("event-tail", db=db, start_exclusive=boundary)
    db.close()

    replay = ReplaySelection(
        mode="checkpoint_tail",
        history_epoch=tail.history_epoch,
        sealed_boundary=tail.sealed_boundary,
        tail=tail,
        checkpoint_generation_id="checkpoint-event-tail",
        checkpoint_boundary=boundary,
        checkpoint_json='{"narrative":"covered history"}',
    )
    agent = SimpleNamespace(context=_FakeContext(), tools=_FakeTools(), max_tokens=512)
    messages, _usage = await prepare_turn_context(
        agent, "event-tail", "continue", "anthropic/claude-test", False, None,
        "web", "event-tail", None, replay_selection=replay,
    )

    rendered = json.dumps(messages)
    assert "EVENT_COVERED_BY_CHECKPOINT" not in rendered
    assert rendered.count("EVENT_AFTER_CHECKPOINT") == 1


@pytest.mark.asyncio
async def test_real_send_prefix_loads_project_context_via_db_worker(monkeypatch):
    from core.native_context_prefix import build_real_send_prefix
    import core.native_context_prefix as native_context_prefix

    calls = []

    async def fake_run_in_db(fn, *args, **kwargs):
        calls.append(fn.__name__)
        if fn.__name__ == "_context_sources_for_prefix":
            return SimpleNamespace(
                sources=[],
                agent_facts={"agent_count": 0},
                installed=None, project_cwd="/workspace/projects/project-a",
            )
        return fn(*args, **kwargs)

    monkeypatch.setattr(
        "llm.capabilities.get_model_context_window",
        lambda _model: 200000,
    )
    monkeypatch.setattr(native_context_prefix, "run_in_db", fake_run_in_db)

    agent = SimpleNamespace(
        context=_FakeContext(),
        tools=_FakeTools(),
        memory_window=100,
        max_tokens=1024,
    )

    _prefix, info = await build_real_send_prefix(
        agent=agent,
        thread_id="project-worker-n1",
        turn_model="anthropic/claude-test",
        fresh_context=True,
        project_id="project-a",
    )

    assert "_context_sources_for_prefix" in calls
    assert info["effective_context_snapshot"]["scope_facts"]["cwd"] == (
        "/workspace/projects/project-a"
    )


@pytest.mark.asyncio
async def test_real_send_prefix_excludes_related_summary_source(
    isolated_db,
    monkeypatch,
):
    from core.native_context_prefix import build_real_send_prefix
    from infra.database import get_db
    from infra.time import utc_now_iso

    monkeypatch.setattr(
        "llm.capabilities.get_model_context_window",
        lambda _model: 200000,
    )

    db = get_db()
    now = utc_now_iso()
    db.execute(
        "INSERT INTO threads (id, user_id, title, project_id, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        ("current", "steve", "Current", "proj-a", now, now),
    )
    db.execute(
        "INSERT INTO threads (id, user_id, title, project_id, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        ("prior", "steve", "Prior Coordination", "proj-a", now, now),
    )
    db.execute(
        "INSERT INTO messages (id, thread_id, role, content, created_at) "
        "VALUES (?,?,?,?,?)",
        ("m-current", "current", "user", "Start the next turn.", now),
    )

    db.commit()
    db.close()

    agent = SimpleNamespace(
        context=_FakeContext(),
        tools=_FakeTools(),
        memory_window=100,
        max_tokens=1024,
    )

    prefix, info = await build_real_send_prefix(
        agent=agent,
        thread_id="current",
        turn_model="anthropic/claude-test",
        fresh_context=False,
        project_id="proj-a",
        replay_selection=_empty_replay("current"),
    )

    # Related cross-thread summaries are NOT auto-injected: no summary-origin
    # source is listed, and the prior thread's text never reaches the payload.
    snapshot = info["effective_context_snapshot"]
    summary_sources = [
        source
        for source in snapshot["source_inventory"]
        if source["origin_section"] == "summary"
    ]

    assert summary_sources == []
    assert "Related prior thread summaries" not in json.dumps(prefix)
    assert (
        "Prior thread established that CTX must be source-truth backed"
        not in json.dumps(prefix)
    )
    assert (
        "Prior thread established that CTX must be source-truth backed"
        not in (snapshot["frozen_prompt_material"].get("source_text") or "")
    )


@pytest.mark.asyncio
async def test_real_send_prefix_requires_explicit_replay_selection():
    from core.native_context_prefix import build_real_send_prefix

    agent = SimpleNamespace(
        context=_FakeContext(),
        tools=_FakeTools(),
        memory_window=100,
        max_tokens=1024,
    )

    with pytest.raises(
        ValueError,
        match="Native continuation requires an explicit replay selection",
    ):
        await build_real_send_prefix(
            agent=agent,
            thread_id="continuation",
            turn_model="anthropic/claude-test",
            fresh_context=False,
            project_id=None,
        )
