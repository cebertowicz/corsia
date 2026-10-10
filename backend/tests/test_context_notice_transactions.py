"""Context notice batches own only their writes, after filesystem classification."""

from contextlib import closing
import json
import sqlite3

import pytest

from core import context_source_paths
from infra import database
from tools.file_write_completion import record_context_source_change

NOTICE_FAILURE = (
    "File saved. The context-change notice could not be recorded; other agents "
    "may still use earlier instructions."
)


@pytest.fixture
def notice_scope(isolated_db, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "shared-notes.md"
    source.write_text("saved instructions", encoding="utf-8")
    with database.write_transaction(None, scope="notice_transaction_seed") as db:
        db.execute(
            "INSERT INTO projects (id,name,created_at,updated_at) "
            "VALUES ('notice-project','Original','t','t')",
        )
        db.executemany(
            "INSERT INTO threads (id,user_id,model,bootloader_files,created_at,updated_at) "
            "VALUES (?,'steve','native/test',?,'t','t')",
            [(name, json.dumps([str(source)])) for name in ("notice-a", "notice-b")],
        )
    return isolated_db, workspace, source


def _snapshot(path):
    with closing(sqlite3.connect(str(path))) as db:
        return (
            db.execute("SELECT name FROM projects WHERE id='notice-project'").fetchone()[0],
            db.execute(
                "SELECT object_kind,object_id,actor_thread_id,details_json "
                "FROM context_change_journal ORDER BY object_id",
            ).fetchall(),
        )


def _writer_free(path):
    with closing(sqlite3.connect(str(path), timeout=0)) as db:
        db.execute("BEGIN IMMEDIATE")
        db.rollback()


def _fail_second_notice():
    with database.write_transaction(None, scope="notice_transaction_fault") as db:
        db.execute(
            "CREATE TRIGGER reject_second_notice AFTER INSERT ON context_change_journal "
            "WHEN (SELECT COUNT(*) FROM context_change_journal)=2 "
            "BEGIN SELECT RAISE(FAIL, 'second notice rejected'); END",
        )


def _append(db, workspace, source):
    return context_source_paths.append_context_source_change(
        db, path=str(source), workspace=workspace, actor_thread_id="notice-writer",
    )


def test_completion_classifies_without_a_writer_then_commits_every_recipient(
    notice_scope, monkeypatch,
):
    path, workspace, source = notice_scope
    real_classify = context_source_paths.classify_context_source
    classified = []

    def classify(*args, **kwargs):
        _writer_free(path)
        changes = real_classify(*args, **kwargs)
        _writer_free(path)
        classified.extend(change.object_id for change in changes)
        return changes

    monkeypatch.setattr(context_source_paths, "classify_context_source", classify)
    assert record_context_source_change(
        source, thread_id="notice-writer", workspace=workspace,
    ) is None
    assert sorted(classified) == ["notice-a", "notice-b"]
    assert _snapshot(path) == ("Original", [
        ("thread_context", name, "notice-writer", '{"path": "shared-notes.md"}')
        for name in ("notice-a", "notice-b")
    ])
    assert source.read_text() == "saved instructions"
    _writer_free(path)


@pytest.mark.parametrize("is_source", [True, False])
def test_completion_never_settles_an_unrelated_pooled_transaction(notice_scope, is_source):
    path, workspace, source = notice_scope
    target = source if is_source else workspace / "ordinary.txt"
    if not is_source:
        target.write_text("ordinary file", encoding="utf-8")
    db = database.get_db()
    try:
        db.execute("UPDATE projects SET name='Caller' WHERE id='notice-project'")
        result = record_context_source_change(
            target, thread_id="notice-writer", workspace=workspace,
        )
        assert db.in_transaction
        assert db.execute("SELECT name FROM projects WHERE id='notice-project'").fetchone()[0] == "Caller"
        assert _snapshot(path) == ("Original", [])
        assert result == (NOTICE_FAILURE if is_source else None)
        db.rollback()
    finally:
        db.close()
    assert source.read_text() == "saved instructions"
    _writer_free(path)


def test_failed_named_batch_cannot_leak_into_the_callers_commit(notice_scope):
    path, workspace, source = notice_scope
    _fail_second_notice()
    db = database.get_db()
    try:
        db.execute("UPDATE projects SET name='Caller' WHERE id='notice-project'")
        with pytest.raises(sqlite3.IntegrityError, match="second notice rejected"):
            _append(db, workspace, source)
        assert db.in_transaction
        db.commit()
    finally:
        db.close()
    assert _snapshot(path) == ("Caller", [])
    _writer_free(path)


def test_named_caller_retains_rollback_of_its_successful_notice_batch(notice_scope):
    path, workspace, source = notice_scope
    db = database.get_db()
    try:
        db.execute("UPDATE projects SET name='Caller' WHERE id='notice-project'")
        assert _append(db, workspace, source) is not None
        assert db.in_transaction
        assert _snapshot(path) == ("Original", [])
        db.rollback()
    finally:
        db.close()
    assert _snapshot(path) == ("Original", [])


@pytest.mark.parametrize("fault", ["commit", "second_notice"])
def test_completion_failure_preserves_saved_bytes_and_leaves_no_partial_notice(
    notice_scope, monkeypatch, fault,
):
    path, workspace, source = notice_scope
    if fault == "commit":
        real_get_db = database.get_db

        class FailCommit(database._NoOpClose):
            def commit(self):
                raise RuntimeError("notice commit rejected")

        monkeypatch.setattr(database, "get_db", lambda: FailCommit(real_get_db()))
    else:
        _fail_second_notice()
    assert record_context_source_change(
        source, thread_id="notice-writer", workspace=workspace,
    ) == NOTICE_FAILURE
    assert source.read_text() == "saved instructions"
    assert _snapshot(path) == ("Original", [])
    _writer_free(path)


def test_missing_actor_does_not_journal_or_settle_a_foreign_owner(notice_scope):
    path, workspace, source = notice_scope
    db = database.get_db()
    try:
        db.execute("UPDATE projects SET name='Caller' WHERE id='notice-project'")
        assert record_context_source_change(source, thread_id=None, workspace=workspace) is None
        assert db.in_transaction
        assert _snapshot(path) == ("Original", [])
        db.rollback()
    finally:
        db.close()
