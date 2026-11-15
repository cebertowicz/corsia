"""A person's save to the path a promote left is refused as "moved" and names where the document is.

Before this refusal the save failed with "No such file", which does not say the document moved, so the
person closed the tab and lost the text it still held. The refusal writes nothing and redirects nothing.
"""

from __future__ import annotations

import asyncio

import pytest

from api.files import document_save_http_exception
from doceditor.document_save import DocumentSave, DocumentSaveError, save_document_text
from registry import models
from tests.test_durable_writes import _promote, _working_file
from tests.test_durable_writes import durable_scope as durable_scope  # noqa: F401 - shared fixture

MOVED = (
    "Couldn't save: this document is at projects/alpha/shared/planning/{name}. "
    "Your changes are still in this tab. Copy them into the document there."
)


def _user_id(db, thread_id):
    return db.execute("SELECT user_id FROM threads WHERE id=?", (thread_id,)).fetchone()[0]


def _save(db, thread_id, path, *, create=False, by_agent=False):
    return asyncio.run(save_document_text(
        DocumentSave(str(path), "my edits\n", project_id="project-alpha", thread_id=thread_id, create=create),
        _user_id(db, thread_id), by_agent=by_agent,
    ))


def _refusal(db, thread_id, path, **kwargs) -> DocumentSaveError:
    with pytest.raises(DocumentSaveError) as refused:
        _save(db, thread_id, path, **kwargs)
    return refused.value


def test_a_save_to_the_path_a_plain_promote_left_is_refused_as_moved(durable_scope):
    """The promote could not carry the identity, so the old one is a tombstone with a successor."""
    workspace, _root, thread_id, db, source, _source_id, _text = _working_file(durable_scope, "plain.md", stale=True)
    _promote(workspace, thread_id, db, source)
    assert not source.exists()

    refused = _refusal(db, thread_id, source)

    assert refused.kind == "moved"
    assert refused.message == MOVED.format(name="plain.md")
    assert not source.exists()


def test_a_save_to_the_path_a_carried_promote_left_is_refused_as_moved(durable_scope):
    """The identity moved to the new path and a tombstone was inserted at the old one."""
    workspace, _root, thread_id, db, source, source_id, _text = _working_file(durable_scope, "carried.md")
    assert _promote(workspace, thread_id, db, source) == source_id

    refused = _refusal(db, thread_id, source)

    assert refused.kind == "moved"
    assert refused.message == MOVED.format(name="carried.md")
    assert not source.exists()


def test_the_refusal_is_a_409_carrying_the_message(durable_scope):
    workspace, _root, thread_id, db, source, _source_id, _text = _working_file(durable_scope, "http.md")
    _promote(workspace, thread_id, db, source)

    http = document_save_http_exception(_refusal(db, thread_id, source))

    assert http.status_code == 409
    assert http.detail == MOVED.format(name="http.md")


def test_a_path_outside_the_workspace_says_only_that_the_document_is_in_another_folder(durable_scope, monkeypatch):
    """No workspace-relative path exists to show, so none is guessed and no absolute path is shown."""
    from registry import promoted_path

    workspace, _root, thread_id, db, source, _source_id, _text = _working_file(durable_scope, "away.md")
    _promote(workspace, thread_id, db, source)
    monkeypatch.setattr(promoted_path, "promoted_to", lambda *_a, **_k: "/elsewhere/away.md")

    refused = _refusal(db, thread_id, source)

    assert refused.kind == "moved"
    assert refused.message == (
        "Couldn't save: this document is in another folder. Your changes are still in this tab."
    )


def test_an_agents_save_keeps_the_plain_error(durable_scope):
    """The text speaks to a person with a tab open; an agent's save keeps today's error."""
    workspace, _root, thread_id, db, source, _source_id, _text = _working_file(durable_scope, "agent.md")
    _promote(workspace, thread_id, db, source)

    assert _refusal(db, thread_id, source, by_agent=True).kind == "no_such_file"


def test_a_deleted_path_keeps_no_such_file(durable_scope):
    _workspace, _root, thread_id, db, source, source_id, _text = _working_file(durable_scope, "gone.md")
    source.unlink()
    models.retire(source_id, tombstone=True, cause="file_gone", db=db)
    db.commit()

    assert _refusal(db, thread_id, source).kind == "no_such_file"


def test_a_path_never_registered_keeps_no_such_file(durable_scope):
    from core.workspace_layout import resolve_thread_working_root

    _workspace, project_root, thread_id, db = durable_scope
    fresh = resolve_thread_working_root(project_root, thread_id) / "never.md"

    assert _refusal(db, thread_id, fresh).kind == "no_such_file"


def test_a_promoted_path_whose_document_is_gone_keeps_no_such_file(durable_scope):
    """With no live successor there is nowhere to point."""
    workspace, _root, thread_id, db, source, _source_id, _text = _working_file(durable_scope, "orphan.md", stale=True)
    successor_id = _promote(workspace, thread_id, db, source)
    models.retire(successor_id, tombstone=True, cause="file_gone", db=db)
    db.commit()

    assert _refusal(db, thread_id, source).kind == "no_such_file"


def test_a_path_that_holds_a_file_again_saves(durable_scope):
    workspace, _root, thread_id, db, source, _source_id, _text = _working_file(durable_scope, "back.md", stale=True)
    _promote(workspace, thread_id, db, source)
    source.write_text("restored\n", encoding="utf-8")

    _save(db, thread_id, source)

    assert source.read_text(encoding="utf-8") == "my edits\n"


def test_a_new_document_at_the_path_a_promote_left_is_unchanged(durable_scope):
    """create=true never reaches the strict resolve, so New document at that path creates the file as before."""
    workspace, _root, thread_id, db, source, _source_id, _text = _working_file(durable_scope, "again.md", stale=True)
    _promote(workspace, thread_id, db, source)

    _save(db, thread_id, source, create=True)

    assert source.read_text(encoding="utf-8") == "my edits\n"


def test_a_probe_outside_the_scope_learns_nothing_about_a_promote(durable_scope, monkeypatch):
    """Even when the lookup would answer for a path, one outside this project's roots is never told."""
    from registry import promoted_path

    workspace, _root, thread_id, db, source, _source_id, _text = _working_file(durable_scope, "probe.md")
    _promote(workspace, thread_id, db, source)
    other = workspace / "projects" / "beta" / source.name
    monkeypatch.setattr(promoted_path, "promoted_to", lambda *_a, **_k: "projects/alpha/shared/planning/probe.md")

    assert _refusal(db, thread_id, source).kind == "moved"
    assert _refusal(db, thread_id, other).kind == "no_such_file"


def test_the_lookup_is_a_few_indexed_reads_on_the_error_path_only(durable_scope):
    """The scale budget: at most 6 reads (5 today), each a key lookup, and only after a save found no file."""
    from registry.promoted_path import moved_save_message

    workspace, _root, thread_id, db, source, _source_id, _text = _working_file(durable_scope, "budget.md")
    _promote(workspace, thread_id, db, source)
    statements: list[str] = []
    db.set_trace_callback(statements.append)
    assert moved_save_message(source, db=db) is not None
    db.set_trace_callback(None)
    reads = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]

    assert 0 < len(reads) <= 6
    for sql in reads:
        plan = " ".join(row[-1] for row in db.execute("EXPLAIN QUERY PLAN " + sql))
        assert "SCAN" not in plan, plan
