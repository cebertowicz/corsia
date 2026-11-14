"""Startup recovery tells the agent when it undoes a promote or moves the promoted file.

The agent that asked for the promote is long gone by the time recovery runs, so each way recovery
can undo or relocate a promote leaves a note in that agent's context journal and a change on the
promoted document's identity. Each case below fails on a build where recovery only logs.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from tests.test_durable_writes import (
    _seed_comment_state,
    _shared_target,
    _working_file,
    durable_scope as durable_scope,  # noqa: F401 - shared governed-workspace fixture
)

_COLUMNS = (
    "id,operation,phase,undoing,actor_thread_id,artifact_type,external_id,target_path,"
    "stage_path,backup_path,source_path,description,content_sha256,created_parent,"
    "created_at,updated_at"
)


def _insert_promote_intent(db, intent_id, *, phase, thread_id, target, source, text, undoing=0):
    now = "2026-11-13T00:00:00+00:00"
    db.execute(
        f"INSERT INTO durable_write_intents ({_COLUMNS}) VALUES ({','.join(['?'] * 16)})",
        (
            intent_id, "promote", phase, undoing, thread_id, "document",
            f"workspace://workspace-stable-id/projects/alpha/shared/planning/{target.name}",
            str(target), str(target.parent / f".corsia-write-{intent_id}.stage"), None,
            str(source), "Promoted launch plan", hashlib.sha256(text.encode()).hexdigest(), 0,
            now, now,
        ),
    )
    db.commit()


def _thread_notes(db, thread_id):
    return [
        (row["summary"], json.loads(row["details_json"]))
        for row in db.execute(
            "SELECT summary,details_json FROM context_change_journal "
            "WHERE object_kind='thread_context' AND object_id=? ORDER BY seq",
            (thread_id,),
        ).fetchall()
    ]


def _document_changes(db, artifact_id):
    return [
        (row["summary"], row["actor_thread_id"])
        for row in db.execute(
            "SELECT summary,actor_thread_id FROM context_change_journal WHERE object_kind='artifact' "
            "AND object_id=? AND change_kind='promote_undone' ORDER BY seq",
            (artifact_id,),
        ).fetchall()
    ]


def _history(source, target, thread_id, why, where, *, parked=False):
    """What the document's history says: third person, since its readers did not make the promote."""
    beside = "A newer file was already at the source path, so the file was kept beside it. " if parked else ""
    return (
        f"The promote of `{source}` to `{target}` by thread {thread_id} was undone when CORSIA "
        f"restarted. {why} {beside}The file is at `{where}`."
    )


def _digest_rows(db, thread_id, source_id):
    """The journal rows this thread's next digest carries for the promoted document."""
    from infra.passive_relevant import relevant_changes

    top = db.execute("SELECT MAX(seq) FROM context_change_journal").fetchone()[0]
    rows, _cut = relevant_changes(
        db, thread_id=thread_id, user_id="user", project_id=None, after=0, through=top,
    )
    return [r for r in rows if r["object_id"] in (source_id, thread_id)]


def test_an_undo_that_a_restart_interrupted_is_told_to_the_agent(durable_scope, monkeypatch):
    """The live promote failed and its undo crashed halfway: startup finishes it and says so."""
    from registry import durable_writes, service

    workspace, project_root, thread_id, db, source, source_id, _text = _working_file(
        durable_scope, "interrupted.md",
    )
    target = project_root / "shared" / "planning" / "interrupted.md"
    original_restore = durable_writes._restore_target

    def refuse_registration(*_args, **_kwargs):
        raise RuntimeError("registration failed")

    def restore_then_crash(path, backup):
        original_restore(path, backup)
        raise RuntimeError("simulated crash after restore")

    monkeypatch.setattr(service, "register_write_strict", refuse_registration)
    monkeypatch.setattr(durable_writes, "_restore_target", restore_then_crash)
    with pytest.raises(RuntimeError, match="simulated crash after restore"):
        durable_writes.promote_file(
            source, "planning", thread_id=thread_id, project_id="project-alpha",
            workspace=workspace, description="Promoted launch plan",
        )
    monkeypatch.setattr(durable_writes, "_restore_target", original_restore)
    assert _thread_notes(db, thread_id) == []

    durable_writes.recover_write_intents(workspace=workspace)

    assert source.exists() and not target.exists()
    [(summary, details)] = _thread_notes(db, thread_id)
    assert summary == (
        f"Your promote of `{source}` to `{target}` was undone when CORSIA restarted. "
        f"It was interrupted by a restart before it finished. Your file is at `{source}`."
    )
    assert details["file_now_at"] == str(source)
    assert details["reoffer_unwritten"] is True
    assert _document_changes(db, source_id) == [
        (_history(source, target, thread_id, "It was interrupted by a restart before it finished.", source), thread_id),
    ]
    # The promoting agent hears it once, from its own note, not again from the document's history.
    assert [r["summary"] for r in _digest_rows(db, thread_id, source_id)] == [summary]


def test_a_rollback_for_comments_on_both_documents_is_told_to_the_agent(durable_scope):
    """An applied promote onto a document with comments can never register: recovery rolls it back and says why."""
    workspace, project_root, thread_id, db, source, source_id, text = _working_file(
        durable_scope, "crashed.md", stale=True,
    )
    _seed_comment_state(db, source_id, thread_id)
    _shared_target(durable_scope, "crashed.md")
    target = project_root / "shared" / "planning" / "crashed.md"
    intent_id = "intent-crashed-promote"
    # The state a crash leaves between publishing the bytes and registering them.
    target.write_text(text, encoding="utf-8")
    source.rename(source.parent / f".corsia-write-{intent_id}.source")
    _insert_promote_intent(
        db, intent_id, phase="file_applied", thread_id=thread_id, target=target, source=source,
        text=text,
    )

    from registry.durable_writes import recover_write_intents

    recover_write_intents(workspace=workspace)

    assert source.read_text(encoding="utf-8") == text and not target.exists()
    [(summary, _details)] = _thread_notes(db, thread_id)
    assert summary.startswith(f"Your promote of `{source}` to `{target}` was undone when CORSIA restarted. ")
    assert "both have comments" in summary and "Nothing was moved" in summary
    assert summary.endswith(f"Your file is at `{source}`.")
    [(history, actor)] = _document_changes(db, source_id)
    assert actor == thread_id and not history.startswith("Your")
    assert history.startswith(f"The promote of `{source}` to `{target}` by thread {thread_id} ")
    assert "both have comments" in history and history.endswith(f"The file is at `{source}`.")


def test_a_claim_parked_beside_a_newer_file_is_told_to_the_agent(durable_scope):
    """The promote never published and a newer file sits at its source: the claimed version is kept beside it."""
    from registry import service
    from registry.durable_writes import recover_write_intents

    workspace, project_root, thread_id, db, source, source_id, _text = _working_file(
        durable_scope, "staged-collision.md",
    )
    claim_text = "claimed source"
    intent_id = "intent-staged-collision"
    (source.parent / f".corsia-write-{intent_id}.source").write_text(claim_text, encoding="utf-8")
    source.write_text("newer source", encoding="utf-8")
    service.register_write_strict(thread_id, str(source), db=db)
    target = project_root / "shared" / "planning" / source.name
    _insert_promote_intent(
        db, intent_id, phase="staged", thread_id=thread_id, target=target, source=source,
        text=claim_text,
    )

    recover_write_intents(workspace=workspace)

    parked = source.with_name(f"{source.name}.promotion-recovery-{intent_id}")
    assert parked.read_text(encoding="utf-8") == claim_text
    assert source.read_text(encoding="utf-8") == "newer source"
    [(summary, details)] = _thread_notes(db, thread_id)
    assert summary == (
        f"Your promote of `{source}` to `{target}` was undone when CORSIA restarted. "
        "It was interrupted by a restart before it finished. A newer file was already at the "
        f"source path, so your file was kept beside it. Your file is at `{parked}`."
    )
    assert details["file_now_at"] == str(parked)
    assert _document_changes(db, source_id) == [
        (
            _history(
                source, target, thread_id, "It was interrupted by a restart before it finished.",
                parked, parked=True,
            ),
            thread_id,
        ),
    ]


def test_a_staged_promote_with_no_newer_file_says_nothing(durable_scope):
    """Nothing moved and the file is where it was: there is nothing to report."""
    from registry.durable_writes import recover_write_intents

    workspace, project_root, thread_id, db, source, _source_id, text = _working_file(
        durable_scope, "plain-staged.md",
    )
    intent_id = "intent-plain-staged"
    source.rename(source.parent / f".corsia-write-{intent_id}.source")
    target = project_root / "shared" / "planning" / source.name
    _insert_promote_intent(
        db, intent_id, phase="staged", thread_id=thread_id, target=target, source=source, text=text,
    )

    recover_write_intents(workspace=workspace)

    assert source.read_text(encoding="utf-8") == text
    assert _thread_notes(db, thread_id) == []


def test_a_notice_that_cannot_be_written_does_not_stop_recovery(durable_scope, monkeypatch, caplog):
    """A broken journal must not stop startup on every boot: the file still goes back and the failure is logged."""
    import logging

    from registry import promote_recovery_notice

    workspace, project_root, thread_id, db, source, source_id, text = _working_file(
        durable_scope, "unnoticed.md",
    )
    intent_id = "intent-unnoticed"
    source.rename(source.parent / f".corsia-write-{intent_id}.source")
    target = project_root / "shared" / "planning" / source.name
    _insert_promote_intent(
        db, intent_id, phase="staged", thread_id=thread_id, target=target, source=source, text=text,
        undoing=2,
    )

    def broken(*_args, **_kwargs):
        raise RuntimeError("journal unavailable")

    monkeypatch.setattr(promote_recovery_notice, "append_thread_context_note", broken)
    from registry.durable_writes import recover_write_intents

    with caplog.at_level(logging.WARNING):
        recover_write_intents(workspace=workspace)

    assert source.read_text(encoding="utf-8") == text
    assert db.execute("SELECT COUNT(*) FROM durable_write_intents").fetchone()[0] == 0
    records = [r for r in caplog.records if "Could not tell the agent" in r.getMessage()]
    assert len(records) == 1 and records[0].levelno >= logging.WARNING
    message = records[0].getMessage()
    assert f"promotion {intent_id} was undone" in message
    assert f"artifact {source_id}" in message
    assert f"file at {source}" in message
