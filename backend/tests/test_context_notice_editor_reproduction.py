"""Reproduce the missing passive notice after a document-editor save.

Both ordinary and governed document writes already persist bytes and register an
artifact. A configured bootloader source must additionally journal one passive
notice for the consuming thread's next independent activity.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from auth_fixtures import OWNER_TOKEN

_TOKEN = OWNER_TOKEN
_AUTH = f"?token={_TOKEN}"


def _session(client, title: str, project_id: str | None = None) -> str:
    response = client.post(
        f"/api/threads{_AUTH}",
        json={"title": title, "project_id": project_id},
    )
    assert response.status_code == 200, response.text
    return response.json()["thread_id"]


@pytest.mark.integration
@pytest.mark.parametrize("managed", [False, True], ids=["ordinary", "governed"])
def test_editor_save_of_configured_bootloader_source_journals_passive_notice(
    integration_client,
    integration_workspace,
    monkeypatch,
    managed: bool,
):
    """The actual PUT must keep its receipt and add the existing journal event."""
    project_id = None
    scope_root = integration_workspace
    if managed:
        project = integration_client.post(
            f"/api/projects{_AUTH}",
            json={"name": f"editor-managed-{uuid.uuid4().hex[:8]}"},
        )
        assert project.status_code == 200, project.text
        project_body = project.json()["project"]
        project_id = project_body["id"]
        scope_root = Path(project_body["path"])

    reader = _session(integration_client, f"editor-reader-{uuid.uuid4().hex[:8]}", project_id)
    writer = _session(integration_client, f"editor-writer-{uuid.uuid4().hex[:8]}", project_id)
    parent = scope_root / ("shared/planning" if managed else "documents")
    if managed:
        # A category becomes writable only once its structure owner has supplied
        # its category index; this makes the governed endpoint path available.
        parent.mkdir(parents=True, exist_ok=True)
        (parent / "INDEX.md").write_text("# Planning\n", encoding="utf-8")
    target = parent / f"configured-{uuid.uuid4().hex[:8]}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# Before\n", encoding="utf-8")

    from infra.database import get_db

    db = get_db()
    try:
        # The loader accepts explicitly configured sources outside context/.
        # A selected model lets the reader admit the digest input later; the
        # save itself never depends on it.
        db.execute(
            "UPDATE threads SET bootloader_files=?, model='native/model' WHERE id=?",
            (json.dumps([str(target)]), reader),
        )
        db.commit()
    finally:
        db.close()

    saved = integration_client.put(
        f"/api/files/content{_AUTH}",
        json={
            "path": str(target),
            "text": "# After\n",
            "project_id": project_id,
            "thread_id": writer,
        },
    )

    # Preserve the successful editor-save facts before asserting the missing
    # context-notice behavior.
    assert saved.status_code == 200, saved.text
    receipt = saved.json()
    assert receipt["saved"] is True
    if managed:
        assert receipt["registry_managed"] is True
    assert target.read_text(encoding="utf-8") == "# After\n"
    artifact = receipt["artifact"]
    assert artifact is not None
    db = get_db()
    try:
        registration = db.execute(
            "SELECT parent_thread_id FROM artifact_registrations WHERE artifact_id=?",
            (artifact["id"],),
        ).fetchone()
        # A new project enforces shared ownership from creation, so a file
        # placed on disk outside the registry is first claimed by its conductor.
        if managed:
            from infra.conductor import get_conductor

            expected_parent = get_conductor(project_id, db=db)
        else:
            expected_parent = writer
    finally:
        db.close()
    assert registration is not None
    assert registration["parent_thread_id"] == expected_parent

    # Isolate the asserted digest from the routine time notice. The reader owns
    # this source; the writer is a different thread, so the change is relevant.
    from infra import passive_changes
    from infra.passive_changes import materialize_pending_digest

    monkeypatch.setattr(passive_changes, "clock_event", lambda *_args: None)
    expected_label = target.relative_to(integration_workspace).as_posix()
    db = get_db()
    try:
        journal = db.execute(
            "SELECT object_kind,object_id,change_kind,summary,details_json "
            "FROM context_change_journal WHERE object_kind='thread_context' "
            "AND object_id=? ORDER BY seq",
            (reader,),
        ).fetchone()
        assert journal is not None, "editor save must journal the configured source change"
        assert dict(journal) == {
            "object_kind": "thread_context",
            "object_id": reader,
            "change_kind": "updated",
            "summary": f"Context source updated: {expected_label}",
            "details_json": json.dumps({"path": expected_label}, sort_keys=True),
        }
        accepted = materialize_pending_digest(db, reader)
        assert accepted is not None
        message = db.execute(
            "SELECT content,metadata FROM messages WHERE id=?", (accepted.message_id,)
        ).fetchone()
    finally:
        db.close()

    assert message is not None
    assert expected_label in message["content"]
    events = json.loads(message["metadata"])["context_events"]
    assert len(events) == 1
    assert {
        key: events[0][key]
        for key in ("object_kind", "object_id", "change_kind", "summary", "details")
    } == {
        "object_kind": "thread_context",
        "object_id": reader,
        "change_kind": "updated",
        "summary": f"Context source updated: {expected_label}",
        "details": {"path": expected_label},
    }


@pytest.mark.integration
def test_editor_save_keeps_success_and_reports_failed_notice(
    integration_client,
    integration_workspace,
    monkeypatch,
):
    """A journal failure never undoes the save; the receipt names the failure."""
    reader = _session(integration_client, f"editor-reader-{uuid.uuid4().hex[:8]}")
    writer = _session(integration_client, f"editor-writer-{uuid.uuid4().hex[:8]}")
    target = integration_workspace / "documents" / f"configured-{uuid.uuid4().hex[:8]}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# Before\n", encoding="utf-8")

    from infra.database import get_db

    db = get_db()
    try:
        db.execute(
            "UPDATE threads SET bootloader_files=? WHERE id=?",
            (json.dumps([str(target)]), reader),
        )
        db.commit()
    finally:
        db.close()

    from core import context_source_paths

    def _journal_unavailable(*_args, **_kwargs):
        raise RuntimeError("journal unavailable")

    monkeypatch.setattr(
        context_source_paths, "append_context_source_change", _journal_unavailable
    )

    saved = integration_client.put(
        f"/api/files/content{_AUTH}",
        json={"path": str(target), "text": "# After\n", "thread_id": writer},
    )

    assert saved.status_code == 200, saved.text
    receipt = saved.json()
    assert receipt["saved"] is True
    assert receipt["artifact"] is not None
    assert receipt["context_notice_error"] == (
        "File saved. The context-change notice could not be recorded; other "
        "agents may still use earlier instructions."
    )
    assert target.read_text(encoding="utf-8") == "# After\n"
    db = get_db()
    try:
        journaled = db.execute(
            "SELECT count(*) FROM context_change_journal "
            "WHERE object_kind='thread_context' AND object_id=?",
            (reader,),
        ).fetchone()[0]
    finally:
        db.close()
    assert journaled == 0
