from __future__ import annotations

import json

import pytest

from delegation_docs_fixtures import (
    _OWNER,
    _mk_thread,
    _registrable_roots,
    doc,
)

pytestmark = pytest.mark.integration


def test_artifact_target_schemas_use_thread_id():
    from tools.artifacts import ArtifactReconcileTool, ArtifactUpdateTool

    for tool in (ArtifactUpdateTool(), ArtifactReconcileTool()):
        properties = tool.parameters["properties"]
        assert "thread_id" in properties
        assert "session_id" not in properties


def test_artifact_update_tool_advertises_pinning_for_long_lived_documents():
    from tools.artifacts import ArtifactUpdateTool

    tool = ArtifactUpdateTool()

    assert tool.parameters["properties"]["pinned"]["type"] == ["boolean", "null"]
    assert "plan task" in tool.description
    assert "durable" in tool.description
    assert "pinned" in tool.description


@pytest.mark.asyncio
async def test_doc_comment_create_tool_derives_agent_author(
    integration_client,
    doc,
    _registrable_roots,
):
    from tools.documents import DocCommentCreateTool

    sid = _mk_thread(integration_client, _OWNER, title="Tool Reviewer")

    result = await DocCommentCreateTool().execute(
        doc_path=str(doc),
        quote="A unique tail sentence",
        body="Looks good from the tool surface.",
        _thread_id=sid,
    )

    payload = json.loads(result)
    comment = payload["comment"]
    assert comment["author_kind"] == "agent"
    assert comment["author"] == "Tool Reviewer"
    assert comment["author_thread_id"] == sid
    assert comment["anchor_text"] == "A unique tail sentence"
    assert comment["anchor_prefix"] is not None
    assert comment["anchor_suffix"] is not None

    from registry import models

    artifact = models.get_artifact(comment["artifact_id"])
    assert artifact["external_id"].startswith("workspace://")
    assert artifact["path"] == str(doc.resolve())


@pytest.mark.asyncio
async def test_doc_comment_tool_list_reanchors_and_orphans_after_external_edit(
    integration_client,
    doc,
    _registrable_roots,
):
    from tools.documents import DocCommentCreateTool, DocCommentsListTool

    sid = _mk_thread(integration_client, _OWNER, title="Tool Reviewer")
    created = json.loads(await DocCommentCreateTool().execute(
        doc_path=str(doc), quote="A unique tail sentence", body="Keep this.", _thread_id=sid,
    ))["comment"]
    original = doc.read_text()
    doc.write_text("Inserted above.\n" + original)

    listed = json.loads(await DocCommentsListTool().execute(
        artifact_id=created["artifact_id"], _thread_id=sid,
    ))
    row = listed["comments"][0]
    assert listed["anchors_checked"] is True
    assert row["from_off"] == created["from_off"] + len("Inserted above.\n")
    assert row["orphaned"] is False

    doc.write_text("The quote is gone.\n")
    orphaned = json.loads(await DocCommentsListTool().execute(
        artifact_id=created["artifact_id"], _thread_id=sid,
    ))["comments"][0]
    assert orphaned["orphaned"] is True
    assert (orphaned["from_off"], orphaned["to_off"]) == (
        row["from_off"], row["to_off"],
    )


@pytest.mark.asyncio
async def test_doc_comment_reply_rejects_conflicting_document_target(
    integration_client,
    doc,
    _registrable_roots,
):
    from tools.documents import DocCommentCreateTool

    sid = _mk_thread(integration_client, _OWNER, title="Tool Reviewer")
    created = json.loads(await DocCommentCreateTool().execute(
        doc_path=str(doc),
        quote="A unique tail sentence",
        body="top-level",
        _thread_id=sid,
    ))

    result = await DocCommentCreateTool().execute(
        parent_id=created["comment"]["id"],
        doc_path=str(doc),
        body="reply",
        _thread_id=sid,
    )

    assert result == "Error: replies derive their document target from parent_id"


@pytest.mark.asyncio
async def test_artifact_register_and_resolve_tools_use_current_thread(
    integration_client,
    doc,
    _registrable_roots,
):
    from tools.artifacts import ArtifactRegisterTool, ArtifactResolveTool

    sid = _mk_thread(integration_client, _OWNER, title="Artifact owner")

    registered = json.loads(await ArtifactRegisterTool().execute(
        path=str(doc),
        description="Tool-owned doc",
        _thread_id=sid,
    ))
    artifact = registered["artifact"]
    assert artifact["parent_thread_id"] == sid
    assert artifact["description"] == "Tool-owned doc"
    assert artifact["external_id"].startswith("workspace://")
    assert artifact["path"] == str(doc.resolve())

    resolved = json.loads(await ArtifactResolveTool().execute(
        path=str(doc),
        _thread_id=sid,
    ))
    assert resolved["found"] is True
    assert resolved["artifact"]["id"] == artifact["id"]


@pytest.mark.asyncio
async def test_artifact_register_promotes_explicitly_durable_identity(
    integration_client,
    doc,
    _registrable_roots,
):
    """An explicit durable re-registration protects a prior ephemeral artifact."""
    from tools.artifacts import ArtifactRegisterTool

    sid = _mk_thread(integration_client, _OWNER, title="Artifact owner")

    first = json.loads(await ArtifactRegisterTool().execute(
        path=str(doc), retention="ephemeral", _thread_id=sid,
    ))["artifact"]
    second = json.loads(await ArtifactRegisterTool().execute(
        path=str(doc), retention="durable", _thread_id=sid,
    ))["artifact"]
    downgraded = json.loads(await ArtifactRegisterTool().execute(
        path=str(doc), retention="ephemeral", _thread_id=sid,
    ))["artifact"]

    assert first["retention"] == "ephemeral"
    assert second["id"] == first["id"]
    assert second["retention"] == "durable"
    assert downgraded["retention"] == "durable"


@pytest.mark.asyncio
async def test_artifact_register_rejects_durable_retention_for_released_identity(
    integration_client,
    doc,
    _registrable_roots,
):
    """A released artifact refuses an identity-level retention change."""
    from tools.artifact_ownership import ArtifactReleaseTool
    from tools.artifacts import ArtifactRegisterTool

    sid = _mk_thread(integration_client, _OWNER, title="Artifact owner")
    first = json.loads(await ArtifactRegisterTool().execute(
        path=str(doc), retention="ephemeral", _thread_id=sid,
    ))["artifact"]

    released = await ArtifactReleaseTool().execute(
        integration=first["integration"],
        type=first["type"],
        external_id=first["external_id"],
        reason="release before renewed registration",
        _thread_id=sid,
    )
    assert not released.startswith("Error")

    renewed = await ArtifactRegisterTool().execute(
        path=str(doc), retention="durable", _thread_id=sid,
    )

    from registry import models

    assert renewed == (
        "Error: retention cannot be changed after ownership is released; "
        "rehome the artifact to an owner first"
    )
    assert models.get_artifact(first["id"])["retention"] == "ephemeral"


@pytest.mark.asyncio
async def test_artifact_register_rejects_durable_retention_from_subscriber(
    integration_client,
    doc,
    _registrable_roots,
):
    """A subscriber cannot change the active owner's identity-level policy."""
    from tools.artifacts import ArtifactRegisterTool

    owner = _mk_thread(integration_client, _OWNER, title="Artifact owner")
    subscriber = _mk_thread(integration_client, _OWNER, title="Artifact subscriber")
    first = json.loads(await ArtifactRegisterTool().execute(
        path=str(doc), retention="ephemeral", _thread_id=owner,
    ))["artifact"]

    renewed = await ArtifactRegisterTool().execute(
        path=str(doc), retention="durable", _thread_id=subscriber,
    )

    from registry import models

    assert renewed == "Error: retention can only be changed by the active owner"
    assert models.get_artifact(first["id"])["retention"] == "ephemeral"


@pytest.mark.asyncio
async def test_artifact_register_rejects_nonexistent_path(
    integration_client,
    doc,
    _registrable_roots,
):
    from tools.artifacts import ArtifactRegisterTool

    sid = _mk_thread(integration_client, _OWNER, title="Artifact owner")
    missing = doc.parent / "does-not-exist.md"
    assert not missing.exists()

    result = await ArtifactRegisterTool().execute(
        path=str(missing),
        _thread_id=sid,
    )

    assert result.startswith("Error: path does not exist")


@pytest.mark.asyncio
async def test_artifact_rehome_tool_enforces_current_owner_authority(
    integration_client,
    doc,
    _registrable_roots,
):
    from tools.artifact_ownership import ArtifactRehomeTool
    from tools.artifacts import ArtifactRegisterTool, ArtifactResolveTool

    owner = _mk_thread(integration_client, _OWNER, title="Artifact owner")
    actor = _mk_thread(integration_client, _OWNER, title="Non-owner actor")
    target = _mk_thread(integration_client, _OWNER, title="Target owner")

    registered = json.loads(await ArtifactRegisterTool().execute(
        path=str(doc),
        _thread_id=owner,
    ))
    artifact = registered["artifact"]

    denied = await ArtifactRehomeTool().execute(
        integration=artifact["integration"],
        type=artifact["type"],
        external_id=artifact["external_id"],
        new_owner_thread_id=target,
        reason="non-owner should not be able to take ownership",
        _thread_id=actor,
    )

    assert denied.startswith("Error: Re-home authority denied")
    resolved = json.loads(await ArtifactResolveTool().execute(
        path=str(doc),
        _thread_id=owner,
    ))
    assert resolved["artifact"]["parent_thread_id"] == owner


@pytest.mark.asyncio
async def test_artifact_release_tool_enforces_current_owner_authority(
    integration_client,
    doc,
    _registrable_roots,
):
    from tools.artifact_ownership import ArtifactReleaseTool
    from tools.artifacts import ArtifactRegisterTool, ArtifactResolveTool

    owner = _mk_thread(integration_client, _OWNER, title="Artifact owner")
    actor = _mk_thread(integration_client, _OWNER, title="Non-owner actor")

    registered = json.loads(await ArtifactRegisterTool().execute(
        path=str(doc),
        _thread_id=owner,
    ))
    artifact = registered["artifact"]

    denied = await ArtifactReleaseTool().execute(
        integration=artifact["integration"],
        type=artifact["type"],
        external_id=artifact["external_id"],
        reason="non-owner should not be able to release ownership",
        _thread_id=actor,
    )

    assert denied.startswith("Error: Release authority denied")
    resolved = json.loads(await ArtifactResolveTool().execute(
        path=str(doc),
        _thread_id=owner,
    ))
    assert resolved["artifact"]["parent_thread_id"] == owner


@pytest.mark.asyncio
async def test_artifact_release_tool_emits_canonical_removal_event(
    integration_client,
    doc,
    _registrable_roots,
):
    from tools.artifact_ownership import ArtifactReleaseTool
    from tools.artifacts import ArtifactRegisterTool

    owner = _mk_thread(integration_client, _OWNER, title="Artifact owner")

    registered = json.loads(await ArtifactRegisterTool().execute(
        path=str(doc),
        _thread_id=owner,
    ))
    artifact = registered["artifact"]

    ok = await ArtifactReleaseTool().execute(
        integration=artifact["integration"],
        type=artifact["type"],
        external_id=artifact["external_id"],
        reason="handing off to triage",
        _thread_id=owner,
    )
    assert not ok.startswith("Error")

    from infra.database import get_db

    db = get_db()
    try:
        msg = db.execute(
            "SELECT role, metadata FROM messages "
            "WHERE thread_id=? ORDER BY created_at DESC LIMIT 1",
            (owner,),
        ).fetchone()
    finally:
        db.close()
    assert msg["role"] == "system"
    meta = json.loads(msg["metadata"])
    assert meta["type"] == "artifact_removed"
    assert meta["subtype"] == "ownership_released"
    assert meta["event_type"] == "artifact.ownership_released"
    assert meta["artifact_id"] == artifact["id"]
    assert meta["actor_kind"] == "agent"


@pytest.mark.asyncio
async def test_artifact_rehome_tool_emits_canonical_removal_event_on_old_owner(
    integration_client,
    doc,
    _registrable_roots,
):
    from tools.artifact_ownership import ArtifactRehomeTool
    from tools.artifacts import ArtifactRegisterTool

    owner = _mk_thread(integration_client, _OWNER, title="Artifact owner")
    target = _mk_thread(integration_client, _OWNER, title="Target owner")

    registered = json.loads(await ArtifactRegisterTool().execute(
        path=str(doc),
        _thread_id=owner,
    ))
    artifact = registered["artifact"]

    ok = await ArtifactRehomeTool().execute(
        integration=artifact["integration"],
        type=artifact["type"],
        external_id=artifact["external_id"],
        new_owner_thread_id=target,
        reason="moving to a better-fit owner",
        _thread_id=owner,
    )
    assert not ok.startswith("Error")

    from infra.database import get_db

    db = get_db()
    try:
        old_owner_msg = db.execute(
            "SELECT role, metadata FROM messages "
            "WHERE thread_id=? ORDER BY created_at DESC LIMIT 1",
            (owner,),
        ).fetchone()
        target_msg = db.execute(
            "SELECT COUNT(*) c FROM messages WHERE thread_id=? "
            "AND json_extract(metadata,'$.type')='artifact_removed'",
            (target,),
        ).fetchone()
    finally:
        db.close()
    assert old_owner_msg["role"] == "system"
    meta = json.loads(old_owner_msg["metadata"])
    assert meta["type"] == "artifact_removed"
    assert meta["subtype"] == "ownership_rehomed"
    assert meta["new_owner_thread_id"] == target
    assert meta["actor_kind"] == "agent"
    # The gaining thread is not a removal from its own perspective.
    assert target_msg["c"] == 0


@pytest.mark.asyncio
async def test_artifact_update_tool_pins_owner_registration(
    integration_client,
    doc,
    _registrable_roots,
):
    from tools.artifacts import ArtifactRegisterTool, ArtifactUpdateTool

    owner = _mk_thread(integration_client, _OWNER, title="Artifact owner")
    artifact = json.loads(await ArtifactRegisterTool().execute(
        path=str(doc),
        _thread_id=owner,
    ))["artifact"]

    pinned = json.loads(await ArtifactUpdateTool().execute(
        artifact_id=artifact["id"],
        pinned=True,
        _thread_id=owner,
    ))["artifact"]
    unpinned = json.loads(await ArtifactUpdateTool().execute(
        artifact_id=artifact["id"],
        pinned=False,
        _thread_id=owner,
    ))["artifact"]

    assert pinned["relationship_state"] == "owner-pinned"
    assert unpinned["relationship_state"] == "owner-active"


@pytest.mark.asyncio
async def test_artifact_update_hidden_tool_emits_canonical_removal_event(
    integration_client,
    doc,
    _registrable_roots,
):
    from tools.artifacts import ArtifactRegisterTool, ArtifactUpdateTool

    owner = _mk_thread(integration_client, _OWNER, title="Artifact owner")

    registered = json.loads(await ArtifactRegisterTool().execute(
        path=str(doc),
        _thread_id=owner,
    ))
    artifact = registered["artifact"]

    ok = await ArtifactUpdateTool().execute(
        artifact_id=artifact["id"],
        hidden=True,
        _thread_id=owner,
    )
    assert not ok.startswith("Error")

    from infra.database import get_db

    db = get_db()
    try:
        msg = db.execute(
            "SELECT role, metadata FROM messages "
            "WHERE thread_id=? ORDER BY created_at DESC LIMIT 1",
            (owner,),
        ).fetchone()
    finally:
        db.close()
    assert msg["role"] == "system"
    meta = json.loads(msg["metadata"])
    assert meta["type"] == "artifact_removed"
    assert meta["subtype"] == "hidden_from_pane"
    assert meta["hidden"] is True
    assert meta["actor_kind"] == "agent"


@pytest.mark.asyncio
async def test_artifact_update_redundant_hide_tool_does_not_duplicate_event(
    integration_client,
    doc,
    _registrable_roots,
):
    """A tool-driven no-op hide must not emit a second removal event."""
    from tools.artifacts import ArtifactRegisterTool, ArtifactUpdateTool

    owner = _mk_thread(integration_client, _OWNER, title="Artifact owner")

    registered = json.loads(await ArtifactRegisterTool().execute(
        path=str(doc),
        _thread_id=owner,
    ))
    artifact = registered["artifact"]

    first = await ArtifactUpdateTool().execute(
        artifact_id=artifact["id"], hidden=True, _thread_id=owner,
    )
    assert not first.startswith("Error")
    second = await ArtifactUpdateTool().execute(
        artifact_id=artifact["id"], hidden=True, _thread_id=owner,
    )
    assert not second.startswith("Error")

    from infra.database import get_db

    db = get_db()
    try:
        count = db.execute(
            "SELECT COUNT(*) c FROM messages WHERE thread_id=? "
            "AND json_extract(metadata,'$.type')='artifact_removed'",
            (owner,),
        ).fetchone()["c"]
    finally:
        db.close()
    assert count == 1


@pytest.mark.asyncio
async def test_artifact_update_hidden_tool_returns_controlled_error_for_null_update(
    integration_client,
    doc,
    _registrable_roots,
    monkeypatch,
):
    from registry import models
    from tools.artifacts import ArtifactRegisterTool, ArtifactUpdateTool

    owner = _mk_thread(integration_client, _OWNER, title="Artifact owner")
    artifact = json.loads(await ArtifactRegisterTool().execute(
        path=str(doc),
        _thread_id=owner,
    ))["artifact"]

    monkeypatch.setattr(models, "update_surfacing", lambda *args, **kwargs: None)

    result = await ArtifactUpdateTool().execute(
        artifact_id=artifact["id"],
        hidden=True,
        _thread_id=owner,
    )
    assert result == "Error: Artifact not found"


@pytest.mark.asyncio
async def test_artifact_release_tool_propagates_turn_as_causal_batch_id(
    integration_client,
    doc,
    _registrable_roots,
):
    """Two releases under the same turn share one causal_event_id so the
    frontend can collapse them into one grouped removal line."""
    from core.turn_core import TurnIdentity
    from tools.artifact_ownership import ArtifactReleaseTool
    from tools.artifacts import ArtifactRegisterTool

    owner = _mk_thread(integration_client, _OWNER, title="Artifact owner")
    turn_identity = TurnIdentity("turn-batch-1", 1)

    first_doc = doc
    second_doc = doc.parent / f"second-{doc.name}"
    second_doc.write_text(doc.read_text())

    first = json.loads(await ArtifactRegisterTool().execute(
        path=str(first_doc), _thread_id=owner,
    ))["artifact"]
    second = json.loads(await ArtifactRegisterTool().execute(
        path=str(second_doc), _thread_id=owner,
    ))["artifact"]

    for artifact in (first, second):
        ok = await ArtifactReleaseTool().execute(
            integration=artifact["integration"],
            type=artifact["type"],
            external_id=artifact["external_id"],
            reason="batch release under one turn",
            _thread_id=owner,
            _host_access_turn_identity=turn_identity,
        )
        assert not ok.startswith("Error")

    from infra.database import get_db

    db = get_db()
    try:
        rows = db.execute(
            "SELECT metadata FROM messages WHERE thread_id=? "
            "AND json_extract(metadata,'$.type')='artifact_removed' "
            "ORDER BY created_at ASC",
            (owner,),
        ).fetchall()
    finally:
        db.close()
    assert len(rows) == 2
    causal_ids = {json.loads(r["metadata"])["causal_event_id"] for r in rows}
    assert causal_ids == {"turn-batch-1"}
