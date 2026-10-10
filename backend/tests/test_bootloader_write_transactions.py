"""Context edits conserve concurrent state and keep file work outside writes."""

from contextlib import closing
import json
import sqlite3

from fastapi import HTTPException
import pytest

from api import context_bootloader as api
from infra import config, database
from infra.platform_paths import WorkspaceSelection
from registry import bootload, service
from registry.file_identity import FileIdentity


@pytest.fixture
def context_scope(isolated_db, tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    monkeypatch.setattr(config, "WORKSPACE_SELECTION", WorkspaceSelection(
        workspace_id="context-writes", root=workspace.resolve(), name="Context writes",
    ))
    monkeypatch.setattr(service, "get_allowed_dirs_rw", lambda **_kwargs: [workspace])
    for filename in ("first.md", "second.md"):
        (workspace / filename).write_text(filename, encoding="utf-8")
    with database.write_transaction(None, scope="seed_bootloader_write_test") as db:
        db.execute(
            "INSERT INTO threads (id,user_id,model,bootloader_files,created_at,updated_at) "
            "VALUES ('context-thread','context-user','native/test','[]','t','t')",
        )
        db.execute(
            "INSERT INTO settings (key,value,updated_at) VALUES ('workspace_context',?,'t')",
            (json.dumps({"bootloader_files": [], "unrelated": "keep"}),),
        )
    return isolated_db, workspace


def _snapshot(db_path, scope):
    with closing(sqlite3.connect(str(db_path))) as db:
        if scope in {"global", "workspace"}:
            row = db.execute(
                "SELECT value FROM settings WHERE key='workspace_context'",
            ).fetchone()
            if row is None:
                row = db.execute(
                    "SELECT value FROM settings WHERE key='workspace_context'",
                ).fetchone()
            setting = json.loads(row[0])
            files = setting["bootloader_files"]
        else:
            setting = json.loads(db.execute(
                "SELECT value FROM settings WHERE key='workspace_context'",
            ).fetchone()[0])
            files = json.loads(
                db.execute(
                    "SELECT bootloader_files FROM threads WHERE id='context-thread'",
                ).fetchone()[0]
            )
        return (
            files,
            setting["unrelated"],
            db.execute("SELECT COUNT(*) FROM context_change_journal").fetchone()[0],
            db.execute("SELECT COUNT(*) FROM context_bootloader_events").fetchone()[0],
        )


def _replace_from_another_connection(db_path, scope, files):
    with closing(sqlite3.connect(str(db_path), timeout=0)) as db:
        if scope in {"global", "workspace"}:
            db.execute(
                "UPDATE settings SET value=? WHERE key = 'workspace_context'",
                (json.dumps({"bootloader_files": files, "unrelated": "keep"}),),
            )
        else:
            db.execute(
                "UPDATE threads SET bootloader_files=? WHERE id='context-thread'",
                (json.dumps(files),),
            )
        db.commit()


async def _edit(scope, action, workspace):
    body: dict = {"scope": scope, "thread_id": "context-thread"}
    user = {"id": "context-user"}
    if action == "replace":
        body["files"] = [str(workspace / "first.md")]
        return await api.update_bootloader_files(body, user)
    if action == "attach":
        body["path"] = str(workspace / "first.md")
        return await api.attach_bootloader_file(body, user)
    body.update(path="created.md", content="saved text")
    return await api.create_and_attach_bootloader_file(body, user)


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["global", "workspace", "thread"])
@pytest.mark.parametrize("action", ["replace", "attach", "create"])
async def test_stale_validation_never_overwrites_a_newer_context_list(
    context_scope, monkeypatch, scope, action,
):
    """A missing write-time comparison would erase the competing saved edit."""
    db_path, workspace = context_scope
    newer = [str(workspace / "second.md")]
    normalize = api.normalize_bootloader_entries

    def validate_then_change(*args, **kwargs):
        files = normalize(*args, **kwargs)
        _replace_from_another_connection(db_path, scope, newer)
        return files

    monkeypatch.setattr(api, "normalize_bootloader_entries", validate_then_change)
    error = None
    try:
        await _edit(scope, action, workspace)
    except HTTPException as exc:
        error = exc
    assert _snapshot(db_path, scope) == (newer, "keep", 0, 0)
    assert error is not None
    assert error.status_code == 400
    assert "changed" in error.detail.lower()
    if action == "create":
        root = workspace if scope in {"global", "workspace"} else workspace / ".threads/context-thread/context"
        assert (root / "created.md").read_text() == "saved text"
        assert "saved" in error.detail.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["global", "workspace", "thread"])
@pytest.mark.parametrize("action", ["replace", "attach", "create"])
async def test_context_edit_commits_its_list_journal_and_audit(
    context_scope, scope, action,
):
    """Missing one coupled write or a commit would leave incomplete durable state."""
    db_path, workspace = context_scope
    result = await _edit(scope, action, workspace)
    assert result["ok"] is True
    filename = "created.md" if action == "create" else "first.md"
    root = (
        workspace / ".threads/context-thread/context"
        if scope == "thread" and action == "create" else workspace
    )
    assert _snapshot(db_path, scope) == ([str(root / filename)], "keep", 1, 1)


def test_bootload_prepares_real_paths_before_acquiring_its_writer(context_scope, monkeypatch):
    """Resolving inside the writer blocks another connection before registration."""
    db_path, workspace = context_scope
    prepare = service.prepare_file_identity

    def prepare_with_free_writer(path, **kwargs):
        if not isinstance(path, FileIdentity):
            with closing(sqlite3.connect(str(db_path), timeout=0)) as other:
                other.execute("BEGIN IMMEDIATE")
                other.rollback()
        return prepare(path, **kwargs)

    monkeypatch.setattr(service, "prepare_file_identity", prepare_with_free_writer)
    assert bootload.sync_bootload_documents("context-project", workspace, ["first.md"]) is None
    with closing(sqlite3.connect(str(db_path))) as check:
        check.row_factory = sqlite3.Row
        row = check.execute(
            "SELECT path FROM artifact_identities WHERE path = ?",
            (str(workspace / "first.md"),),
        ).fetchone()
    assert row is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["global", "workspace", "thread"])
@pytest.mark.parametrize("action", ["replace", "attach", "create"])
async def test_audit_failure_rolls_back_the_list_and_journal(context_scope, scope, action):
    """An early commit would leave the requested list or a success journal behind."""
    db_path, workspace = context_scope
    with closing(sqlite3.connect(str(db_path))) as db:
        db.execute(
            "CREATE TRIGGER reject_context_audit BEFORE INSERT ON context_bootloader_events "
            "BEGIN SELECT RAISE(ABORT, 'audit refused'); END",
        )
    with pytest.raises(sqlite3.IntegrityError, match="audit refused"):
        await _edit(scope, action, workspace)
    assert _snapshot(db_path, scope) == ([], "keep", 0, 0)
    if action == "create":
        root = workspace if scope in {"global", "workspace"} else workspace / ".threads/context-thread/context"
        assert (root / "created.md").read_text() == "saved text"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["replace", "attach", "create"])
async def test_thread_ownership_is_current_when_the_edit_is_written(
    context_scope, monkeypatch, action,
):
    """Reusing only the pre-validation owner would write to a newly foreign thread."""
    db_path, workspace = context_scope
    normalize = api.normalize_bootloader_entries

    def validate_then_reassign(*args, **kwargs):
        files = normalize(*args, **kwargs)
        with closing(sqlite3.connect(str(db_path))) as db:
            db.execute("UPDATE threads SET user_id='other-user' WHERE id='context-thread'")
            db.commit()
        return files

    monkeypatch.setattr(api, "normalize_bootloader_entries", validate_then_reassign)
    with pytest.raises(HTTPException) as error:
        await _edit("thread", action, workspace)
    assert error.value.status_code == 404
    assert _snapshot(db_path, "thread") == ([], "keep", 0, 0)
