"""A write or edit that names the path a promote left points at the promoted document."""

from __future__ import annotations

import asyncio

import pytest

from registry import durable_writes, models
from registry.shared_write_errors import PromotedPathRefused
from tests.test_durable_writes import _promote, _working_file
from tests.test_durable_writes import durable_scope as durable_scope

MOVED = "This file was promoted to projects/alpha/shared/planning/{name}; {verb} it there."


def _promoted(durable_scope, name):
    """A working file promoted to shared/planning as a new identity: the old path is gone and its tombstone has a successor.

    The file is edited outside CORSIA first (``stale``), the case where a promote cannot carry its identity and leaves
    a tombstone behind. A promote that carries the identity leaves no row at the old path.
    """
    workspace, _root, thread_id, db, source, source_id, _text = _working_file(durable_scope, name, stale=True)
    successor_id = _promote(workspace, thread_id, db, source)
    return workspace, thread_id, db, source, source_id, successor_id


def _write(workspace, thread_id, path):
    return durable_writes.write_text(
        path, "replayed\n", thread_id=thread_id, project_id="project-alpha", workspace=workspace,
    )


def test_a_write_to_the_path_a_promote_left_is_refused_and_creates_nothing(durable_scope):
    workspace, thread_id, _db, source, _source_id, _successor = _promoted(durable_scope, "left.md")
    assert not source.exists()

    with pytest.raises(PromotedPathRefused) as refused:
        _write(workspace, thread_id, source)

    assert str(refused.value) == MOVED.format(name="left.md", verb="write")
    assert not source.exists()


def test_the_pointer_names_the_document_the_comments_moved_to(durable_scope):
    """The registry's successor link, not the path text, decides where the pointer goes."""
    workspace, thread_id, db, source, source_id, successor_id = _promoted(durable_scope, "linked.md")
    assert models.successor_id(source_id, db=db) == successor_id

    with pytest.raises(PromotedPathRefused, match="projects/alpha/shared/planning/linked.md"):
        _write(workspace, thread_id, source)


def test_a_path_whose_file_was_deleted_still_takes_a_write(durable_scope):
    """A plain delete leaves a tombstone with no successor, and its path is writable as before."""
    workspace, _root, thread_id, db, source, source_id, _text = _working_file(durable_scope, "gone.md")
    source.unlink()
    models.retire(source_id, tombstone=True, cause="file_gone", db=db)
    db.commit()

    assert _write(workspace, thread_id, source) is None


def test_a_path_never_registered_takes_a_write(durable_scope):
    from core.workspace_layout import resolve_thread_working_root

    workspace, project_root, thread_id, _db = durable_scope
    fresh = resolve_thread_working_root(project_root, thread_id) / "new.md"

    assert _write(workspace, thread_id, fresh) is None


def test_a_path_a_promote_left_is_writable_once_the_promoted_document_is_gone(durable_scope):
    """With no live successor there is nowhere to point, so the write is not refused."""
    workspace, thread_id, db, source, _source_id, successor_id = _promoted(durable_scope, "orphan.md")
    models.retire(successor_id, tombstone=True, cause="file_gone", db=db)
    db.commit()

    assert _write(workspace, thread_id, source) is None


def test_a_path_that_holds_a_file_again_is_not_refused(durable_scope):
    """The refusal is for a missing file only; a file at the path is an ordinary write."""
    workspace, thread_id, _db, source, _source_id, _successor = _promoted(durable_scope, "back.md")
    source.write_text("restored\n", encoding="utf-8")

    _write(workspace, thread_id, source)  # raises PromotedPathRefused if the refusal fired on a present file


@pytest.fixture(autouse=True)
def _tools_reach_the_workspace(durable_scope, monkeypatch):
    from tools import sandbox_config

    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RW", [durable_scope[0].resolve()])


def _edit(workspace, thread_id, path, tool_cls, **arguments):
    tool = tool_cls(workspace=workspace)
    return asyncio.run(tool.execute(path=str(path), _thread_id=thread_id, _thread_project_id="project-alpha", **arguments))


def test_edit_file_on_the_path_a_promote_left_points_at_the_document(durable_scope):
    from tools.filesystem_edit import EditFileTool

    workspace, thread_id, _db, source, *_ = _promoted(durable_scope, "edit.md")

    result = _edit(workspace, thread_id, source, EditFileTool, old_string="Launch", new_string="Ship")

    assert result == "Error: " + MOVED.format(name="edit.md", verb="edit")
    assert not source.exists()


def test_multi_edit_on_the_path_a_promote_left_points_at_the_document(durable_scope):
    from tools.filesystem_edit import MultiEditTool

    workspace, thread_id, _db, source, *_ = _promoted(durable_scope, "multi.md")

    result = _edit(
        workspace, thread_id, source, MultiEditTool,
        edits=[{"old_string": "Launch", "new_string": "Ship"}],
    )

    assert result == "Error: " + MOVED.format(name="multi.md", verb="edit")


def test_edit_file_on_a_deleted_path_keeps_the_plain_not_found_error(durable_scope):
    from tools.filesystem_edit import EditFileTool

    workspace, _root, thread_id, db, source, source_id, _text = _working_file(durable_scope, "plain.md")
    source.unlink()
    models.retire(source_id, tombstone=True, cause="file_gone", db=db)
    db.commit()

    result = _edit(workspace, thread_id, source, EditFileTool, old_string="Launch", new_string="Ship")

    assert result == f"Error: File not found: {source}"


def test_a_carried_promote_points_saved_calls_at_the_new_path(durable_scope):
    """A promote that keeps the identity changes only its path; the saved call a wake repeats must name the new one."""
    import json

    workspace, _root, thread_id, db, source, source_id, _text = _working_file(durable_scope, "carried.md")
    user_id = str(db.execute("SELECT user_id FROM threads WHERE id=?", (thread_id,)).fetchone()[0])
    ops = [{"call": {"path": str(source), "old_string": "Launch", "new_string": "Ship"}}]
    db.execute(
        """INSERT INTO doc_comment_writer_line(id,root_id,artifact_id,agent_thread_id,user_id,ops,saved_at)
           VALUES ('line-carried','root-carried',?,?,?,?,'2026-11-13T00:00:00+00:00')""",
        (source_id, thread_id, user_id, json.dumps(ops)),
    )
    db.commit()

    assert _promote(workspace, thread_id, db, source) == source_id

    saved = json.loads(db.execute("SELECT ops FROM doc_comment_writer_line WHERE id='line-carried'").fetchone()[0])
    assert saved[0]["call"]["path"].endswith("projects/alpha/shared/planning/carried.md")
    assert saved[0]["call"]["old_string"] == "Launch"


def test_a_path_under_another_workspace_is_not_a_promoted_path(durable_scope, tmp_path):
    """A write whose workspace is not the one this process selected has no registry row here, so it is never refused."""
    from registry.promoted_path import promoted_to

    assert promoted_to(tmp_path / "elsewhere" / "note.md") is None
