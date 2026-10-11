from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from uuid import uuid4

import pytest

from infra.platform_paths import WorkspaceSelection
from tests.registry_helpers import make_session


@pytest.fixture
def durable_scope(isolated_db, tmp_path, monkeypatch):
    from infra import config
    from infra.database import get_db
    from registry import file_resolution

    workspace = tmp_path / "workspace"
    project_root = workspace / "projects" / "alpha"
    shared = project_root / "shared"
    planning = shared / "planning"
    planning.mkdir(parents=True)
    (shared / "INDEX.md").write_text("# Shared\n", encoding="utf-8")
    (planning / "INDEX.md").write_text("# Planning\n", encoding="utf-8")
    selection = WorkspaceSelection(
        workspace_id="workspace-stable-id",
        root=workspace.resolve(),
        name="Test workspace",
    )
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    monkeypatch.setattr(config, "WORKSPACE_SELECTION", selection)
    monkeypatch.setattr(
        file_resolution,
        "get_allowed_dirs_rw",
        lambda **_kwargs: [workspace.resolve()],
    )
    db = get_db()
    db.execute(
        "INSERT INTO projects (id,name,path,status,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (
            "project-alpha",
            "Alpha",
            "workspace://workspace-stable-id/projects/alpha",
            "active",
            "t",
            "t",
        ),
    )
    thread_id = make_session(db, project_id="project-alpha", prefix="durable")
    db.execute("UPDATE threads SET agent_enabled=1 WHERE id=?", (thread_id,))
    db.commit()

    # These regression cases predate the required shared-document review trigger;
    # keep their focus on publication mechanics while dedicated ownership tests
    # exercise the creation contract without this compatibility default.
    from registry import durable_writes

    real_write_text = durable_writes.write_text
    real_promote_file = durable_writes.promote_file

    def write_with_trigger(*args, **kwargs):
        kwargs.setdefault("update_trigger", "when the test fixture changes")
        return real_write_text(*args, **kwargs)

    def promote_with_trigger(*args, **kwargs):
        kwargs.setdefault("update_trigger", "when the test fixture changes")
        return real_promote_file(*args, **kwargs)

    monkeypatch.setattr(durable_writes, "write_text", write_with_trigger)
    monkeypatch.setattr(durable_writes, "promote_file", promote_with_trigger)
    return workspace, project_root, thread_id, db


def test_shared_write_is_atomic_registered_and_returns_bounded_hint(durable_scope):
    from registry.durable_writes import write_text

    workspace, project_root, thread_id, db = durable_scope
    reference = project_root / "shared" / "reference"
    reference.mkdir()
    (reference / "INDEX.md").write_text("# Reference\n", encoding="utf-8")
    similar = reference / "roadmap-old.md"
    similar.write_text("old", encoding="utf-8")
    target = project_root / "shared" / "planning" / "roadmap.md"

    receipt = write_text(
        target,
        "new roadmap\n",
        thread_id=thread_id,
        project_id="project-alpha",
        workspace=workspace,
        description="Current delivery roadmap",
    )

    assert receipt is not None
    assert target.read_text(encoding="utf-8") == "new roadmap\n"
    assert receipt["artifact"]["external_id"] == (
        "workspace://workspace-stable-id/projects/alpha/shared/planning/roadmap.md"
    )
    index_path = project_root / "shared" / "planning" / "INDEX.md"
    assert receipt["index_registered"] == str(index_path)
    assert "- `roadmap.md` — Current delivery roadmap" in index_path.read_text(
        encoding="utf-8"
    )
    assert receipt["similar_files"] == [str(similar)]
    assert "duplicate" in receipt["deduplication_note"].lower()
    assert db.execute("SELECT COUNT(*) FROM durable_write_intents").fetchone()[0] == 0


def test_shared_index_entry_is_not_duplicated_on_rewrite(durable_scope):
    """An edit to an already-indexed file must not append a second bullet."""
    from registry.durable_writes import write_text

    workspace, project_root, thread_id, _db = durable_scope
    target = project_root / "shared" / "planning" / "roadmap.md"
    index_path = project_root / "shared" / "planning" / "INDEX.md"
    common = dict(
        thread_id=thread_id,
        project_id="project-alpha",
        workspace=workspace,
    )

    write_text(target, "first\n", description="Delivery roadmap", **common)
    write_text(target, "second\n", description="Delivery roadmap", **common)

    index_text = index_path.read_text(encoding="utf-8")
    assert index_text.count("`roadmap.md`") == 1


def test_shared_index_append_is_serialized_across_processes(
    durable_scope,
    monkeypatch,
):
    """Concurrent first-time appends must not both pass the duplicate check.

    Exercises the real critical section by racing OS processes against the
    same index file, rather than trusting the single-threaded happy path.
    """
    import threading
    import time

    from registry import durable_writes
    from registry.durable_writes import _register_in_category_index
    from registry.shared_target import SharedTarget

    _workspace, project_root, _thread_id, _db = durable_scope
    category_root = project_root / "shared" / "planning"
    index_path = category_root / "INDEX.md"
    shared = SharedTarget(
        category_root / "raced.md", category_root, index_path, False
    )

    workers = 4
    start = threading.Barrier(workers)
    errors: list[str] = []

    # Widen the read->check->append window so every thread would observe a
    # missing bullet if the section were not mutually exclusive. Without the
    # lock this test fails with one bullet per thread; that is its whole point.
    real_lookup = durable_writes._index_lists_file

    def _slow_lookup(index_text: str, name: str) -> bool:
        time.sleep(0.05)
        return real_lookup(index_text, name)

    monkeypatch.setattr(durable_writes, "_index_lists_file", _slow_lookup)

    def _append() -> None:
        start.wait(timeout=30)
        failure = _register_in_category_index(shared, "racer")
        if failure is not None:
            errors.append(failure)

    threads = [threading.Thread(target=_append) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert not any(thread.is_alive() for thread in threads)
    assert index_path.read_text(encoding="utf-8").count("`raced.md`") == 1


@pytest.mark.parametrize(
    ("index_text", "name", "expected"),
    (
        ("- `roadmap.md` — the plan\n", "roadmap.md", True),
        # A longer name sharing a prefix is a different file.
        ("- `roadmap.md.bak` — a backup\n", "roadmap.md", False),
        ("- `roadmap.md` — the plan\n", "roadmap.md.bak", False),
        # Prose mentioning the file is not a registration.
        ("See `roadmap.md` for details.\n", "roadmap.md", False),
        ("", "roadmap.md", False),
    ),
)
def test_index_entry_detection_matches_whole_names_only(index_text, name, expected):
    from registry.durable_writes import _index_lists_file

    assert _index_lists_file(index_text, name) is expected


def test_shared_index_itself_is_not_self_registered(durable_scope):
    """Writing a category INDEX.md must not add a bullet pointing at itself."""
    from registry.durable_writes import write_text

    workspace, project_root, thread_id, _db = durable_scope
    index_path = project_root / "shared" / "planning" / "INDEX.md"

    write_text(
        index_path,
        "# Planning\n",
        thread_id=thread_id,
        project_id="project-alpha",
        workspace=workspace,
    )

    assert "`INDEX.md`" not in index_path.read_text(encoding="utf-8")


def test_shared_index_failure_is_surfaced_not_swallowed(durable_scope, monkeypatch):
    """A published file whose index could not be updated reports the drift."""
    from registry import durable_writes
    from registry.durable_writes import write_text

    workspace, project_root, thread_id, _db = durable_scope
    target = project_root / "shared" / "planning" / "roadmap.md"

    def _boom(_shared, _description, **_kwargs):
        return "INDEX.md: disk on fire"

    monkeypatch.setattr(durable_writes, "_register_in_category_index", _boom)

    receipt = write_text(
        target,
        "roadmap\n",
        thread_id=thread_id,
        project_id="project-alpha",
        workspace=workspace,
        description="Delivery roadmap",
    )

    # The write itself still succeeds — an index failure must not destroy content.
    assert target.read_text(encoding="utf-8") == "roadmap\n"
    assert receipt["index_error"] == "INDEX.md: disk on fire"
    assert "index_registered" not in receipt


def test_new_shared_write_does_not_replace_destination_created_during_publish(
    durable_scope,
    monkeypatch,
):
    from registry import durable_writes

    workspace, project_root, thread_id, db = durable_scope
    target = project_root / "shared" / "planning" / "concurrent-winner.md"
    original_stage = durable_writes.stage_bound_bytes

    def stage_then_publish_winner(path, data, **kwargs):
        staged = original_stage(path, data, **kwargs)
        target.write_text("concurrent winner\n", encoding="utf-8")
        return staged

    monkeypatch.setattr(durable_writes, "stage_bound_bytes", stage_then_publish_winner)

    with pytest.raises(FileExistsError):
        durable_writes.write_text(
            target,
            "losing write\n",
            thread_id=thread_id,
            project_id="project-alpha",
            workspace=workspace,
            description="Must not replace a concurrent winner",
        )

    assert target.read_text(encoding="utf-8") == "concurrent winner\n"
    assert list(target.parent.glob(".corsia-write-*")) == []
    assert db.execute("SELECT COUNT(*) FROM durable_write_intents").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM artifact_identities").fetchone()[0] == 0


def test_promotion_reads_the_path_entry_it_claimed_after_source_replacement(
    durable_scope,
    monkeypatch,
):
    from core.workspace_layout import resolve_thread_working_root
    from registry.durable_writes import promote_file
    from tools import filesystem_effects

    workspace, project_root, thread_id, _db = durable_scope
    source_root = resolve_thread_working_root(project_root, thread_id)
    source_root.mkdir(parents=True)
    source = source_root / "claim-race.md"
    source.write_text("old pathname entry\n", encoding="utf-8")
    replacement = source_root / ".replacement"
    replacement.write_text("replacement pathname entry\n", encoding="utf-8")
    target = project_root / "shared" / "planning" / source.name
    original_rename = filesystem_effects.os.rename
    replacement_installed = False

    def replace_source_before_claim(
        source_name,
        claim_name,
        *,
        src_dir_fd,
        dst_dir_fd,
    ):
        nonlocal replacement_installed
        os.replace(replacement, source)
        replacement_installed = True
        original_rename(
            source_name,
            claim_name,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(filesystem_effects.os, "rename", replace_source_before_claim)

    promote_file(
        source,
        "planning",
        thread_id=thread_id,
        project_id="project-alpha",
        workspace=workspace,
        description="Claim race proof",
    )

    assert replacement_installed is True
    assert target.read_text(encoding="utf-8") == "replacement pathname entry\n"
    assert not source.exists()
    assert list(source_root.glob(".corsia-write-*.source")) == []


def test_promotion_streams_claim_without_using_the_in_memory_write_path(
    durable_scope,
    monkeypatch,
):
    from core.workspace_layout import resolve_thread_working_root
    from registry import durable_writes

    workspace, project_root, thread_id, _db = durable_scope
    source_root = resolve_thread_working_root(project_root, thread_id)
    source_root.mkdir(parents=True)
    source = source_root / "streamed.bin"
    source.write_bytes((b"stream-me\x00" * 257) + b"tail")
    expected = source.read_bytes()
    target = project_root / "shared" / "planning" / source.name

    def reject_buffered_stage(*_args, **_kwargs):
        raise AssertionError("promotion used the in-memory bytes staging path")

    monkeypatch.setattr(durable_writes, "stage_bound_bytes", reject_buffered_stage)

    durable_writes.promote_file(
        source,
        "planning",
        thread_id=thread_id,
        project_id="project-alpha",
        workspace=workspace,
        description="Streaming promotion proof",
    )

    assert target.read_bytes() == expected
    assert not source.exists()


def test_startup_finishes_undo_after_crash_just_after_target_restore(
    durable_scope,
    monkeypatch,
):
    from registry import durable_writes, service

    workspace, project_root, thread_id, db = durable_scope
    target = project_root / "shared" / "planning" / "undo-crash.md"
    original_restore = durable_writes._restore_target

    def fail_registration(*_args, **_kwargs):
        raise RuntimeError("registration failed")

    def restore_then_crash(path, backup):
        original_restore(path, backup)
        raise RuntimeError("simulated crash after restore")

    monkeypatch.setattr(service, "register_write_strict", fail_registration)
    monkeypatch.setattr(durable_writes, "_restore_target", restore_then_crash)

    with pytest.raises(RuntimeError, match="simulated crash after restore"):
        durable_writes.write_text(
            target,
            "unpublished\n",
            thread_id=thread_id,
            project_id="project-alpha",
            workspace=workspace,
            description="Crash recovery proof",
        )

    assert not target.exists()
    assert db.execute("SELECT COUNT(*) FROM durable_write_intents").fetchone()[0] == 1

    monkeypatch.setattr(durable_writes, "_restore_target", original_restore)
    durable_writes.recover_write_intents(workspace=workspace)

    assert not target.exists()
    assert list(target.parent.glob(".corsia-write-*")) == []
    assert db.execute("SELECT COUNT(*) FROM durable_write_intents").fetchone()[0] == 0


def test_startup_finishes_existing_undo_when_old_and_new_bytes_match(
    durable_scope,
    monkeypatch,
):
    from registry import durable_writes, service

    workspace, project_root, thread_id, db = durable_scope
    target = project_root / "shared" / "planning" / "same-content-undo.md"
    target.write_text("same\n", encoding="utf-8")
    original_remove = durable_writes._remove_if_present

    def fail_registration(*_args, **_kwargs):
        raise RuntimeError("registration failed")

    def remove_then_crash(path):
        original_remove(path)
        if path is not None and path.name.endswith(".backup"):
            raise RuntimeError("simulated crash after undo cleanup")

    monkeypatch.setattr(service, "register_write_strict", fail_registration)
    monkeypatch.setattr(durable_writes, "_remove_if_present", remove_then_crash)

    with pytest.raises(RuntimeError, match="simulated crash after undo cleanup"):
        durable_writes.write_text(
            target,
            "same\n",
            thread_id=thread_id,
            project_id="project-alpha",
            workspace=workspace,
        )

    assert target.read_text(encoding="utf-8") == "same\n"
    intent = db.execute("SELECT undoing FROM durable_write_intents").fetchone()
    assert intent is not None and intent["undoing"] == 2

    monkeypatch.setattr(durable_writes, "_remove_if_present", original_remove)
    durable_writes.recover_write_intents(workspace=workspace)

    assert target.read_text(encoding="utf-8") == "same\n"
    assert list(target.parent.glob(".corsia-write-*")) == []
    assert db.execute("SELECT COUNT(*) FROM durable_write_intents").fetchone()[0] == 0


@pytest.mark.parametrize("entry_kind", ("symlink", "directory"))
def test_startup_recovers_nonregular_source_claim_after_cleanup_crash(
    durable_scope,
    monkeypatch,
    entry_kind,
):
    from core.workspace_layout import resolve_thread_working_root
    from registry import durable_writes
    from tools import filesystem_effects

    workspace, project_root, thread_id, db = durable_scope
    source_root = resolve_thread_working_root(project_root, thread_id)
    source_root.mkdir(parents=True)
    source = source_root / "raced-entry.md"
    source.write_text("original regular file\n", encoding="utf-8")
    payload = source_root / "symlink-payload.md"
    payload.write_text("raced symlink target\n", encoding="utf-8")
    replacement = source_root / ".replacement-entry"
    if entry_kind == "symlink":
        replacement.symlink_to(payload.name)
    else:
        replacement.mkdir()
        (replacement / "nested.md").write_text(
            "raced directory entry\n",
            encoding="utf-8",
        )
    target = project_root / "shared" / "planning" / source.name
    original_rename = filesystem_effects.os.rename
    original_recover = durable_writes._recover_unpublished_claim

    def replace_source_before_claim(
        source_name,
        claim_name,
        *,
        src_dir_fd,
        dst_dir_fd,
    ):
        source.unlink()
        original_rename(replacement, source)
        original_rename(
            source_name,
            claim_name,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    def crash_before_claim_cleanup(*_args, **_kwargs):
        raise RuntimeError("simulated crash before claim cleanup")

    monkeypatch.setattr(filesystem_effects.os, "rename", replace_source_before_claim)
    monkeypatch.setattr(
        durable_writes,
        "_recover_unpublished_claim",
        crash_before_claim_cleanup,
    )

    with pytest.raises(RuntimeError, match="simulated crash before claim cleanup"):
        durable_writes.promote_file(
            source,
            "planning",
            thread_id=thread_id,
            project_id="project-alpha",
            workspace=workspace,
            description="Nonregular claim recovery proof",
        )

    claim = next(source_root.glob(".corsia-write-*.source"))
    assert claim.is_symlink() if entry_kind == "symlink" else claim.is_dir()
    assert db.execute("SELECT COUNT(*) FROM durable_write_intents").fetchone()[0] == 1

    monkeypatch.setattr(durable_writes, "_recover_unpublished_claim", original_recover)
    durable_writes.recover_write_intents(workspace=workspace)

    if entry_kind == "symlink":
        assert source.is_symlink()
        assert source.read_text(encoding="utf-8") == "raced symlink target\n"
    else:
        assert source.is_dir()
        assert (source / "nested.md").read_text(encoding="utf-8") == (
            "raced directory entry\n"
        )
    assert not target.exists()
    assert list(source_root.glob(".corsia-write-*.source")) == []
    assert db.execute("SELECT COUNT(*) FROM durable_write_intents").fetchone()[0] == 0


@pytest.mark.parametrize(
    "relative",
    (
        "working/rogue.md",
        "documents/rogue.md",
        "context/rogue.md",
        "arbitrary/nested/rogue.md",
    ),
)
def test_ordinary_thread_rejects_unclassified_project_subtrees(
    durable_scope,
    relative,
):
    from registry.durable_writes import write_text
    from registry.shared_write_errors import DurableWritePolicyError

    workspace, project_root, thread_id, _db = durable_scope
    target = project_root / relative

    with pytest.raises(DurableWritePolicyError) as error:
        write_text(
            target,
            "must not escape governed paths",
            thread_id=thread_id,
            project_id="project-alpha",
            workspace=workspace,
            description="Rejected unclassified write",
        )

    message = str(error.value)
    assert ".threads/<id>/" in message
    assert "shared/<category>" in message
    assert "conductor" in message


@pytest.mark.parametrize(
    "relative",
    (
        "PLAN.md",
        "context/RESPONSIBILITY.md",
        "skills/my-skill/SKILL.md",
        "working/scratch.md",
        "working/nested/deep.md",
    ),
)
def test_thread_writes_anywhere_inside_its_own_thread_directory(
    durable_scope,
    relative,
):
    """A thread owns its whole ``.threads/<id>/`` directory, not just working/."""
    from registry.durable_writes import write_text

    workspace, project_root, thread_id, _db = durable_scope
    target = project_root / ".threads" / thread_id / relative

    # ``None`` means "ordinary permitted write" — the caller performs the write.
    assert (
        write_text(
            target,
            "thread-owned durable state\n",
            thread_id=thread_id,
            project_id="project-alpha",
            workspace=workspace,
            description="Thread-owned write",
        )
        is None
    )


def test_thread_cannot_write_into_another_threads_directory(durable_scope):
    from registry.durable_writes import write_text
    from registry.shared_write_errors import DurableWritePolicyError

    workspace, project_root, thread_id, _db = durable_scope
    target = project_root / ".threads" / "some-other-thread" / "PLAN.md"

    with pytest.raises(DurableWritePolicyError) as error:
        write_text(
            target,
            "must not cross thread ownership",
            thread_id=thread_id,
            project_id="project-alpha",
            workspace=workspace,
            description="Rejected cross-thread write",
        )

    assert "another thread's directory" in str(error.value)
    assert not target.exists()


def test_direct_user_can_write_inside_a_thread_directory(durable_scope):
    """The user edits any document, including one under an agent's ``.threads/<id>/``."""
    from registry.durable_writes import write_text

    workspace, project_root, _thread_id, _db = durable_scope
    target = project_root / ".threads" / "some-other-thread" / "working" / "draft.md"

    assert (
        write_text(
            target,
            "edited by the user\n",
            thread_id="ui",
            project_id="project-alpha",
            workspace=workspace,
            direct_user=True,
        )
        is None
    )


def test_scope_conductor_can_write_inside_another_threads_directory(durable_scope):
    from infra.conductor import set_conductor
    from registry.durable_writes import write_text

    workspace, project_root, thread_id, db = durable_scope
    assert set_conductor("project-alpha", thread_id, db=db)
    target = project_root / ".threads" / "some-other-thread" / "working" / "draft.md"

    assert (
        write_text(
            target,
            "edited by the conductor\n",
            thread_id=thread_id,
            project_id="project-alpha",
            workspace=workspace,
        )
        is None
    )


def test_thread_root_write_is_durable_while_working_write_is_tmp(durable_scope):
    """Thread-root state surfaces in the pane; working/ stays ephemeral."""
    from registry.file_resolution import is_tmp

    _workspace, project_root, thread_id, _db = durable_scope
    thread_root = project_root / ".threads" / thread_id

    assert is_tmp(thread_root / "working" / "scratch.md") is True
    assert is_tmp(thread_root / "PLAN.md") is False
    assert is_tmp(thread_root / "context" / "RESPONSIBILITY.md") is False


def test_existing_shared_edit_reuses_identity_without_new_description(durable_scope):
    from registry.durable_writes import write_text

    workspace, project_root, thread_id, db = durable_scope
    target = project_root / "shared" / "planning" / "decision.md"
    first = write_text(
        target,
        "first\n",
        thread_id=thread_id,
        project_id="project-alpha",
        workspace=workspace,
        description="Current decision",
    )
    target.chmod(0o640)
    previous_umask = os.umask(0o077)
    try:
        second = write_text(
            target,
            "second\n",
            thread_id=thread_id,
            project_id="project-alpha",
            workspace=workspace,
        )
    finally:
        os.umask(previous_umask)

    assert first is not None and second is not None
    assert first["artifact"]["id"] == second["artifact"]["id"]
    assert target.read_text(encoding="utf-8") == "second\n"
    assert target.stat().st_mode & 0o777 == 0o640
    assert db.execute(
        "SELECT COUNT(*) FROM artifact_identities WHERE path=?", (str(target),)
    ).fetchone()[0] == 1


def test_failed_existing_edit_restores_bytes_and_mode(durable_scope, monkeypatch):
    from registry import service
    from registry.durable_writes import write_text

    workspace, project_root, thread_id, _db = durable_scope
    target = project_root / "shared" / "planning" / "mode-rollback.md"
    target.write_text("before\n", encoding="utf-8")
    target.chmod(0o640)

    def fail(*_args, **_kwargs):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(service, "register_write_strict", fail)
    with pytest.raises(RuntimeError, match="db exploded"):
        write_text(
            target,
            "after\n",
            thread_id=thread_id,
            project_id="project-alpha",
            workspace=workspace,
        )

    assert target.read_text(encoding="utf-8") == "before\n"
    assert target.stat().st_mode & 0o777 == 0o640


def test_similarity_hint_failure_cannot_turn_committed_write_into_failure(
    durable_scope,
    monkeypatch,
):
    from registry.durable_writes import write_text

    workspace, project_root, thread_id, _db = durable_scope
    reference = project_root / "shared" / "reference"
    reference.mkdir()
    (reference / "INDEX.md").write_text("# Reference\n", encoding="utf-8")
    target = project_root / "shared" / "planning" / "committed.md"
    original_iterdir = Path.iterdir

    def flaky_iterdir(path):
        if path == reference:
            raise PermissionError("concurrent category failure")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", flaky_iterdir)
    receipt = write_text(
        target,
        "committed\n",
        thread_id=thread_id,
        project_id="project-alpha",
        workspace=workspace,
        description="Must still report success",
    )

    assert receipt is not None and receipt["ok"] is True
    assert target.read_text(encoding="utf-8") == "committed\n"


def test_new_shared_write_requires_existing_category_and_description(durable_scope):
    from registry.durable_writes import write_text
    from registry.shared_write_errors import DurableWritePolicyError

    workspace, project_root, thread_id, _db = durable_scope
    missing = project_root / "shared" / "research" / "notes.md"
    with pytest.raises(DurableWritePolicyError) as category_error:
        write_text(
            missing,
            "notes",
            thread_id=thread_id,
            project_id="project-alpha",
            workspace=workspace,
            description="Research notes",
        )
    message = str(category_error.value)
    assert "planning" in message
    assert "conductor" in message.lower() or "user" in message.lower()

    target = project_root / "shared" / "planning" / "notes.md"
    with pytest.raises(DurableWritePolicyError, match="description"):
        write_text(
            target,
            "notes",
            thread_id=thread_id,
            project_id="project-alpha",
            workspace=workspace,
        )
    with pytest.raises(DurableWritePolicyError, match="description"):
        write_text(
            target,
            "notes",
            thread_id=thread_id,
            project_id="project-alpha",
            workspace=workspace,
            description="   ",
        )


def test_root_and_direct_shared_writes_are_rejected_for_ordinary_thread(
    durable_scope,
):
    from registry.durable_writes import write_text
    from registry.shared_write_errors import DurableWritePolicyError

    workspace, project_root, thread_id, _db = durable_scope
    for target in (
        project_root / "rogue.md",
        project_root / "shared" / "rogue.md",
        project_root / "shared" / "INDEX.md",
    ):
        with pytest.raises(DurableWritePolicyError):
            write_text(
                target,
                "nope",
                thread_id=thread_id,
                project_id="project-alpha",
                workspace=workspace,
                description="Should be rejected",
            )


def test_conductor_can_create_category_index_as_explicit_structure(durable_scope):
    from infra.conductor import set_conductor
    from registry.durable_writes import write_text

    workspace, project_root, thread_id, db = durable_scope
    assert set_conductor("project-alpha", thread_id, db=db)
    target = project_root / "shared" / "research" / "INDEX.md"

    receipt = write_text(
        target,
        "# Research\n",
        thread_id=thread_id,
        project_id="project-alpha",
        workspace=workspace,
    )

    assert receipt is not None
    assert target.read_text(encoding="utf-8") == "# Research\n"


def test_direct_user_can_complete_an_existing_unindexed_category(durable_scope):
    from registry.durable_writes import write_text

    workspace, project_root, _thread_id, _db = durable_scope
    category = project_root / "shared" / "research"
    category.mkdir()
    target = category / "INDEX.md"

    receipt = write_text(
        target,
        "# Research\n",
        thread_id="ui",
        project_id="project-alpha",
        workspace=workspace,
        direct_user=True,
    )

    assert receipt is not None
    assert target.read_text(encoding="utf-8") == "# Research\n"


def test_category_writer_can_edit_existing_category_index(durable_scope):
    from registry.durable_writes import write_text

    workspace, project_root, thread_id, _db = durable_scope
    target = project_root / "shared" / "planning" / "INDEX.md"

    receipt = write_text(
        target,
        "# Planning\n\n- roadmap.md\n",
        thread_id=thread_id,
        project_id="project-alpha",
        workspace=workspace,
    )

    assert receipt is not None
    assert target.read_text(encoding="utf-8") == "# Planning\n\n- roadmap.md\n"


def test_ordinary_session_still_cannot_create_a_new_category(durable_scope):
    from registry.durable_writes import write_text
    from registry.shared_write_errors import DurableWritePolicyError

    workspace, project_root, thread_id, _db = durable_scope
    target = project_root / "shared" / "research" / "INDEX.md"

    with pytest.raises(DurableWritePolicyError) as excinfo:
        write_text(
            target,
            "# Research\n",
            thread_id=thread_id,
            project_id="project-alpha",
            workspace=workspace,
        )

    assert "research" in str(excinfo.value)
    assert not target.exists()


def test_reviewed_host_action_can_bypass_default_workspace_governance(
    durable_scope,
):
    from registry.durable_writes import write_text

    workspace, project_root, thread_id, _db = durable_scope
    for target in (
        project_root / "reviewed-root-write.md",
        project_root / "shared" / "unapproved" / "nested" / "reviewed.md",
    ):
        receipt = write_text(
            target,
            "reviewed",
            thread_id=thread_id,
            project_id="project-alpha",
            workspace=workspace,
            reviewed=True,
        )

        assert receipt is None
        assert not target.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["write_file", "edit_file", "multi_edit"])
async def test_active_lease_reviews_workspace_local_write_before_governance(
    durable_scope,
    monkeypatch,
    tool_name,
):
    from infra import state
    from tools import host_access_gate, sandbox_config
    from tools.filesystem_edit import EditFileTool, MultiEditTool
    from tools.filesystem_rw import WriteFileTool
    from tools.registry import ToolRegistry

    workspace, project_root, thread_id, _db = durable_scope
    target = project_root / "reviewed-root-write.md"
    if tool_name != "write_file":
        target.write_text("before\n", encoding="utf-8")
    lease = {
        "id": "lease-1",
        "revision": 4,
        "application_hash": "a" * 64,
        "state": "active",
    }
    active_lease = [lease]
    reviews = []
    consumptions = []

    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RW", [workspace])
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RO", [])
    monkeypatch.setattr(
        sandbox_config,
        "_list_authoritative_repo_policies",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        host_access_gate,
        "is_path_allowed",
        lambda _path, *, for_write=False: True,
    )
    monkeypatch.setattr(
        host_access_gate,
        "check_repo_write_path",
        lambda _path: None,
    )

    async def resolve(_thread_id):
        return active_lease[0]

    async def review(_lease_id, **kwargs):
        reviews.append(kwargs)
        return {
            "lease": lease,
            "review": {
                "lease_id": lease["id"],
                "lease_revision": lease["revision"],
                "thread_id": kwargs["thread_id"],
                "tool_name": kwargs["tool_name"],
                "action_hash": kwargs["action_hash"],
                "verdict": "allow",
                "permit_id": "permit-1",
            },
        }

    async def consume(permit_id, **kwargs):
        consumptions.append((permit_id, kwargs))

    monkeypatch.setattr(host_access_gate, "_resolve_active_lease", resolve)
    monkeypatch.setattr(host_access_gate, "_review_host_action", review)
    monkeypatch.setattr(
        host_access_gate,
        "_consume_host_action_permit",
        consume,
    )

    registry = ToolRegistry()
    registry.register(WriteFileTool(workspace=workspace))
    registry.register(EditFileTool(workspace=workspace))
    registry.register(MultiEditTool(workspace=workspace))
    if tool_name == "write_file":
        params = {
            "path": str(target),
            "content": "reviewed\n",
            "description": "Reviewed root write",
        }
    elif tool_name == "edit_file":
        params = {
            "path": str(target),
            "old_string": "before",
            "new_string": "reviewed",
        }
    else:
        params = {
            "path": str(target),
            "edits": [{"old_string": "before", "new_string": "reviewed"}],
        }
    token = state._current_thread_id.set(thread_id)
    try:
        ordinary_target = (
            project_root / "shared" / "planning" / "ordinary-during-lease.md"
        )
        ordinary = await registry.execute(
            "write_file",
            {
                "path": str(ordinary_target),
                "content": "ordinary\n",
                "description": "Ordinary governed write",
                "update_trigger": "when the test fixture changes",
            },
        )
        assert json.loads(ordinary)["registry_managed"] is True
        assert reviews == []
        assert consumptions == []

        active_lease[0] = None
        denied = await registry.execute(tool_name, params)
        assert denied.startswith("Error:")
        assert "The scope root is scope structure owned by" in denied
        assert reviews == []
        assert consumptions == []

        active_lease[0] = lease
        result = await registry.execute(tool_name, params)
    finally:
        state._current_thread_id.reset(token)

    expected_result = {
        "write_file": "Successfully wrote",
        "edit_file": "Successfully edited",
        "multi_edit": "Successfully applied",
    }[tool_name]
    assert result.startswith(expected_result)
    assert target.read_text(encoding="utf-8") == "reviewed\n"
    assert len(reviews) == 1
    assert reviews[0]["tool_name"] == tool_name
    assert len(consumptions) == 1
    assert consumptions[0][0] == "permit-1"


def test_registration_failure_restores_filesystem_and_clears_intent(
    durable_scope,
    monkeypatch,
):
    from registry import service
    from registry.durable_writes import write_text

    workspace, project_root, thread_id, db = durable_scope
    target = project_root / "shared" / "planning" / "rollback.md"

    def fail(*_args, **_kwargs):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(service, "register_write_strict", fail)
    with pytest.raises(RuntimeError, match="db exploded"):
        write_text(
            target,
            "must roll back",
            thread_id=thread_id,
            project_id="project-alpha",
            workspace=workspace,
            description="Rollback proof",
        )

    assert not target.exists()
    assert db.execute("SELECT COUNT(*) FROM durable_write_intents").fetchone()[0] == 0
    assert list(target.parent.glob(".corsia-write-*")) == []


def test_recovery_rolls_back_staged_and_completes_applied_intents(durable_scope):
    from registry.durable_writes import recover_write_intents

    workspace, project_root, thread_id, db = durable_scope
    category = project_root / "shared" / "planning"
    staged_target = category / "staged.md"
    stage = category / ".corsia-write-intent-staged.stage"
    stage.write_text("staged", encoding="utf-8")
    now = "2026-07-22T00:00:00+00:00"
    db.execute(
        "INSERT INTO durable_write_intents "
        "(id,operation,phase,actor_thread_id,artifact_type,external_id,"
        "target_path,stage_path,backup_path,source_path,description,"
        "content_sha256,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "intent-staged",
            "write",
            "staged",
            thread_id,
            "document",
            "workspace://workspace-stable-id/projects/alpha/shared/planning/staged.md",
            str(staged_target),
            str(stage),
            None,
            None,
            "Staged",
            hashlib.sha256(b"staged").hexdigest(),
            now,
            now,
        ),
    )
    applied = category / "applied.md"
    applied.write_text("applied", encoding="utf-8")
    db.execute(
        "INSERT INTO durable_write_intents "
        "(id,operation,phase,actor_thread_id,artifact_type,external_id,"
        "target_path,stage_path,backup_path,source_path,description,"
        "content_sha256,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "intent-applied",
            "write",
            "file_applied",
            thread_id,
            "document",
            "workspace://workspace-stable-id/projects/alpha/shared/planning/applied.md",
            str(applied),
            str(category / ".corsia-write-intent-applied.stage"),
            None,
            None,
            "Applied",
            hashlib.sha256(b"applied").hexdigest(),
            now,
            now,
        ),
    )
    db.commit()

    recover_write_intents(workspace=workspace)

    assert not stage.exists()
    assert db.execute("SELECT COUNT(*) FROM durable_write_intents").fetchone()[0] == 0
    identity = db.execute(
        "SELECT id FROM artifact_identities WHERE external_id LIKE '%/applied.md'"
    ).fetchone()
    assert identity is not None


def test_recovery_removes_unpublished_category_created_by_intent(durable_scope):
    from registry.durable_writes import recover_write_intents

    workspace, project_root, thread_id, db = durable_scope
    category = project_root / "shared" / "research"
    category.mkdir()
    target = category / "INDEX.md"
    now = "2026-07-22T00:00:00+00:00"
    db.execute(
        "INSERT INTO durable_write_intents "
        "(id,operation,phase,actor_thread_id,artifact_type,external_id,"
        "target_path,stage_path,backup_path,source_path,description,"
        "content_sha256,created_parent,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "intent-category",
            "write",
            "staged",
            thread_id,
            "document",
            "workspace://workspace-stable-id/projects/alpha/shared/research/INDEX.md",
            str(target),
            str(category / ".corsia-write-intent-category.stage"),
            None,
            None,
            None,
            hashlib.sha256(b"# Research\n").hexdigest(),
            1,
            now,
            now,
        ),
    )
    db.commit()

    recover_write_intents(workspace=workspace)

    assert not category.exists()
    assert db.execute("SELECT COUNT(*) FROM durable_write_intents").fetchone()[0] == 0


def test_recovery_clears_intent_when_created_category_never_existed(durable_scope):
    from registry.durable_writes import recover_write_intents

    workspace, project_root, thread_id, db = durable_scope
    category = project_root / "shared" / "never-created"
    target = category / "INDEX.md"
    intent_id = "intent-before-mkdir"
    now = "2026-07-22T00:00:00+00:00"
    db.execute(
        "INSERT INTO durable_write_intents "
        "(id,operation,phase,actor_thread_id,artifact_type,external_id,"
        "target_path,stage_path,backup_path,source_path,description,"
        "content_sha256,created_parent,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            intent_id,
            "write",
            "staged",
            thread_id,
            "document",
            "workspace://workspace-stable-id/projects/alpha/shared/never-created/INDEX.md",
            str(target),
            str(category / f".corsia-write-{intent_id}.stage"),
            None,
            None,
            None,
            hashlib.sha256(b"# Never created\n").hexdigest(),
            1,
            now,
            now,
        ),
    )
    db.commit()

    recover_write_intents(workspace=workspace)

    assert not category.exists()
    assert db.execute("SELECT COUNT(*) FROM durable_write_intents").fetchone()[0] == 0


def test_recovery_rejects_tampered_cleanup_paths_without_deleting_them(
    durable_scope,
):
    from registry.durable_writes import recover_write_intents

    workspace, project_root, thread_id, db = durable_scope
    target = project_root / "shared" / "planning" / "tampered.md"
    outside = workspace.parent / "must-survive.txt"
    outside.write_text("keep", encoding="utf-8")
    now = "2026-07-22T00:00:00+00:00"
    db.execute(
        "INSERT INTO durable_write_intents "
        "(id,operation,phase,actor_thread_id,artifact_type,external_id,"
        "target_path,stage_path,backup_path,source_path,description,"
        "content_sha256,created_parent,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "intent-tampered",
            "write",
            "staged",
            thread_id,
            "document",
            "workspace://workspace-stable-id/projects/alpha/shared/planning/tampered.md",
            str(target),
            str(outside),
            None,
            None,
            "Tamper proof",
            hashlib.sha256(b"new").hexdigest(),
            0,
            now,
            now,
        ),
    )
    db.commit()

    with pytest.raises(RuntimeError, match="stage path"):
        recover_write_intents(workspace=workspace)

    assert outside.read_text(encoding="utf-8") == "keep"
    assert db.execute("SELECT COUNT(*) FROM durable_write_intents").fetchone()[0] == 1


def test_recovery_rejects_path_like_intent_id_before_cleanup(durable_scope):
    from registry.durable_writes import recover_write_intents

    workspace, project_root, thread_id, db = durable_scope
    target = project_root / "shared" / "planning" / "tampered-id.md"
    intent_id = "bad/../../../must-survive"
    escaped_stage = (target.parent / f".corsia-write-{intent_id}.stage").resolve(
        strict=False
    )
    escaped_stage.write_text("keep", encoding="utf-8")
    now = "2026-07-22T00:00:00+00:00"
    db.execute(
        "INSERT INTO durable_write_intents "
        "(id,operation,phase,actor_thread_id,artifact_type,external_id,"
        "target_path,stage_path,backup_path,source_path,description,"
        "content_sha256,created_parent,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            intent_id,
            "write",
            "staged",
            thread_id,
            "document",
            "workspace://workspace-stable-id/projects/alpha/shared/planning/tampered-id.md",
            str(target),
            str(escaped_stage),
            None,
            None,
            "Tamper proof",
            hashlib.sha256(b"new").hexdigest(),
            0,
            now,
            now,
        ),
    )
    db.commit()

    with pytest.raises(RuntimeError, match="identifier"):
        recover_write_intents(workspace=workspace)

    assert escaped_stage.read_text(encoding="utf-8") == "keep"


def test_recovery_validates_promoted_identity_before_removing_source(
    durable_scope,
):
    from core.workspace_layout import resolve_thread_working_root
    from registry.durable_writes import recover_write_intents

    workspace, project_root, thread_id, db = durable_scope
    source_root = resolve_thread_working_root(project_root, thread_id)
    source_root.mkdir(parents=True)
    source = source_root / "promote.md"
    source.write_text("keep source", encoding="utf-8")
    target = project_root / "shared" / "planning" / "promote.md"
    target.write_text("keep source", encoding="utf-8")
    now = "2026-07-22T00:00:00+00:00"
    db.execute(
        "INSERT INTO durable_write_intents "
        "(id,operation,phase,actor_thread_id,artifact_type,external_id,"
        "target_path,stage_path,backup_path,source_path,description,"
        "content_sha256,created_parent,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "intent-wrong-identity",
            "promote",
            "file_applied",
            thread_id,
            "document",
            "workspace://workspace-stable-id/wrong.md",
            str(target),
            str(target.parent / ".corsia-write-intent-wrong-identity.stage"),
            None,
            str(source),
            "Promotion",
            hashlib.sha256(b"keep source").hexdigest(),
            0,
            now,
            now,
        ),
    )
    db.commit()

    with pytest.raises(RuntimeError, match="identity"):
        recover_write_intents(workspace=workspace)

    assert source.read_text(encoding="utf-8") == "keep source"
    assert target.read_text(encoding="utf-8") == "keep source"


def test_recovery_cleans_claim_without_deleting_newer_source(durable_scope):
    from core.workspace_layout import resolve_thread_working_root
    from registry import models, service
    from registry.durable_writes import recover_write_intents

    workspace, project_root, thread_id, db = durable_scope
    source_root = resolve_thread_working_root(project_root, thread_id)
    source_root.mkdir(parents=True)
    source = source_root / "concurrent.md"
    source.write_text("newer source", encoding="utf-8")
    source_identity = service.register_write_strict(thread_id, str(source), db=db)
    intent_id = "intent-claimed-source"
    claim = source.parent / f".corsia-write-{intent_id}.source"
    claim.write_text("claimed source", encoding="utf-8")
    target = project_root / "shared" / "planning" / "concurrent.md"
    target.write_text("claimed source", encoding="utf-8")
    now = "2026-07-22T00:00:00+00:00"
    db.execute(
        "INSERT INTO durable_write_intents "
        "(id,operation,phase,actor_thread_id,artifact_type,external_id,"
        "target_path,stage_path,backup_path,source_path,description,"
        "content_sha256,created_parent,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            intent_id,
            "promote",
            "file_applied",
            thread_id,
            "document",
            "workspace://workspace-stable-id/projects/alpha/shared/planning/concurrent.md",
            str(target),
            str(target.parent / f".corsia-write-{intent_id}.stage"),
            None,
            str(source),
            "Concurrent promotion",
            hashlib.sha256(b"claimed source").hexdigest(),
            0,
            now,
            now,
        ),
    )
    db.commit()

    recover_write_intents(workspace=workspace)

    assert source.read_text(encoding="utf-8") == "newer source"
    assert target.read_text(encoding="utf-8") == "claimed source"
    assert not claim.exists()
    assert models.get_artifact(source_identity["id"], db=db)["state"] == "active"
    assert db.execute("SELECT COUNT(*) FROM durable_write_intents").fetchone()[0] == 0


def test_recovery_preserves_unpublished_claim_when_newer_source_exists(
    durable_scope,
):
    from core.workspace_layout import resolve_thread_working_root
    from registry.durable_writes import recover_write_intents

    workspace, project_root, thread_id, db = durable_scope
    source_root = resolve_thread_working_root(project_root, thread_id)
    source_root.mkdir(parents=True)
    source = source_root / "staged-collision.md"
    source.write_text("newer source", encoding="utf-8")
    intent_id = "intent-staged-collision"
    claim = source.parent / f".corsia-write-{intent_id}.source"
    claim.write_text("claimed source", encoding="utf-8")
    recovery = source.with_name(f"{source.name}.promotion-recovery-{intent_id}")
    target = project_root / "shared" / "planning" / source.name
    now = "2026-07-22T00:00:00+00:00"
    db.execute(
        "INSERT INTO durable_write_intents "
        "(id,operation,phase,actor_thread_id,artifact_type,external_id,"
        "target_path,stage_path,backup_path,source_path,description,"
        "content_sha256,created_parent,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            intent_id,
            "promote",
            "staged",
            thread_id,
            "document",
            "workspace://workspace-stable-id/projects/alpha/shared/planning/staged-collision.md",
            str(target),
            str(target.parent / f".corsia-write-{intent_id}.stage"),
            None,
            str(source),
            "Staged collision",
            hashlib.sha256(b"claimed source").hexdigest(),
            0,
            now,
            now,
        ),
    )
    db.commit()

    recover_write_intents(workspace=workspace)

    assert source.read_text(encoding="utf-8") == "newer source"
    assert recovery.read_text(encoding="utf-8") == "claimed source"
    assert not claim.exists()
    assert not target.exists()
    assert db.execute("SELECT COUNT(*) FROM durable_write_intents").fetchone()[0] == 0


def test_recovery_rejects_forged_created_parent_before_cleanup(durable_scope):
    from registry.durable_writes import recover_write_intents

    workspace, project_root, thread_id, db = durable_scope
    category = project_root / "shared" / "planning"
    target = category / "ordinary.md"
    now = "2026-07-22T00:00:00+00:00"
    db.execute(
        "INSERT INTO durable_write_intents "
        "(id,operation,phase,actor_thread_id,artifact_type,external_id,"
        "target_path,stage_path,backup_path,source_path,description,"
        "content_sha256,created_parent,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "intent-forged-parent",
            "write",
            "staged",
            thread_id,
            "document",
            "workspace://workspace-stable-id/projects/alpha/shared/planning/ordinary.md",
            str(target),
            str(category / ".corsia-write-intent-forged-parent.stage"),
            None,
            None,
            "Ordinary",
            hashlib.sha256(b"ordinary").hexdigest(),
            1,
            now,
            now,
        ),
    )
    db.commit()

    with pytest.raises(RuntimeError, match="created parent"):
        recover_write_intents(workspace=workspace)

    assert category.is_dir()
    assert (category / "INDEX.md").is_file()


@pytest.mark.asyncio
async def test_corsia_write_and_edit_tools_use_durable_shared_seam(
    durable_scope,
    monkeypatch,
):
    from core.turn_core import TurnIdentity
    from plans import service as plan_service
    from registry import service
    from tools import filesystem_edit, filesystem_rw
    from tools.filesystem_gate import AuthorizedPath
    from tools.filesystem_edit import EditFileTool, MultiEditTool
    from tools.filesystem_rw import WriteFileTool

    workspace, project_root, thread_id, db = durable_scope
    plan = plan_service.ensure_plan("thread", thread_id, db=db)
    node = plan_service.add_node(
        plan.id,
        kind="task",
        title="Publish through file tools",
        acceptance_criteria="Shared artifact is registered",
        db=db,
    )
    node = plan_service.transition_node(node.id, "active", db=db)
    turn = TurnIdentity("durable-tool-turn", 1)
    db.execute(
        """INSERT INTO active_turns
           (id,thread_id,execution_epoch,model,status,started_at,boot_epoch,
            plan_id,plan_node_id,plan_attempt_id,plan_commitment_id,plan_usage_role)
           VALUES (?,?,0,'native/test','running','t0','boot',?,?,?,?,?)""",
        (
            turn.turn_id,
            thread_id,
            plan.id,
            node.id,
            "attempt-durable-tools",
            node.commitment_id,
            "execution",
        ),
    )
    db.commit()
    target = project_root / "shared" / "planning" / "tool-path.md"
    loop_thread = threading.get_ident()
    announced: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        service,
        "notify_changed",
        lambda artifact: announced.append(
            (artifact["id"], threading.get_ident() == loop_thread)
        ),
    )

    async def authorize_write(**_kwargs):
        return AuthorizedPath(target, reviewed=False)

    monkeypatch.setattr(filesystem_rw, "_resolve_path_for_action", authorize_write)
    written = await WriteFileTool(workspace=workspace).execute(
        path=str(target),
        content="alpha beta",
        description="Tool path proof",
        update_trigger="when the test fixture changes",
        _thread_id=thread_id,
        _thread_project_id="project-alpha",
        _host_access_turn_identity=turn,
    )
    assert json.loads(written)["registry_managed"] is True

    async def authorize_edit(**_kwargs):
        return AuthorizedPath(target, reviewed=False)

    monkeypatch.setattr(filesystem_edit, "_resolve_path_for_action", authorize_edit)
    edited = await EditFileTool(workspace=workspace).execute(
        path=str(target),
        old_string="alpha",
        new_string="gamma",
        _thread_id=thread_id,
        _thread_project_id="project-alpha",
        _host_access_turn_identity=turn,
    )
    assert json.loads(edited)["registry_managed"] is True
    multiple = await MultiEditTool(workspace=workspace).execute(
        path=str(target),
        edits=[{"old_string": "beta", "new_string": "delta"}],
        _thread_id=thread_id,
        _thread_project_id="project-alpha",
        _host_access_turn_identity=turn,
    )
    assert json.loads(multiple)["registry_managed"] is True
    assert target.read_text(encoding="utf-8") == "gamma delta"
    assert db.execute(
        "SELECT COUNT(*) FROM artifact_identities WHERE path=?", (str(target),)
    ).fetchone()[0] == 1
    registration = db.execute(
        """SELECT origin_plan_node_id FROM artifact_registrations
           WHERE parent_thread_id=?""",
        (thread_id,),
    ).fetchone()
    assert registration["origin_plan_node_id"] == node.id
    producers = [
        json.loads(row["metadata"])["producer"]
        for row in db.execute(
            """SELECT metadata FROM artifact_registration_events
               WHERE affected_thread_id=? AND event_type='registered'
               ORDER BY rowid""",
            (thread_id,),
        ).fetchall()
        if row["metadata"] and "producer" in json.loads(row["metadata"])
    ]
    assert producers == ["write_file", "edit_file", "multi_edit"]
    artifact_id = json.loads(multiple)["artifact"]["id"]
    # The write runs off the loop, but the socket nudge must come from the loop.
    assert announced == [(artifact_id, True)] * 3
