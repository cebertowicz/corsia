from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import time

import pytest

from infra.platform_paths import WorkspaceSelection
from tests.registry_helpers import make_session


@pytest.fixture
def ownership_scope(isolated_db, tmp_path, monkeypatch):
    from infra import config
    from infra.database import get_db
    from registry import service

    workspace = tmp_path / "workspace"
    project_root = workspace / "projects" / "alpha"
    category = project_root / "shared" / "documents"
    category.mkdir(parents=True)
    (project_root / "shared" / "INDEX.md").write_text("# Shared\n", encoding="utf-8")
    (category / "INDEX.md").write_text("# Documents\n", encoding="utf-8")
    selection = WorkspaceSelection(
        workspace_id="workspace-stable-id",
        root=workspace.resolve(),
        name="Test workspace",
    )
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    monkeypatch.setattr(config, "WORKSPACE_SELECTION", selection)
    monkeypatch.setattr(
        service,
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
    owner = make_session(db, project_id="project-alpha", title="Documents", prefix="owner")
    other = make_session(db, project_id="project-alpha", title="Other Lane", prefix="other")
    conductor = make_session(db, project_id="project-alpha", title="Conductor", prefix="conductor")
    plain = make_session(db, project_id="project-alpha", title="Plain Chat", prefix="plain")
    worker = make_session(db, project_id="project-alpha", title="Worker", prefix="worker")
    nested = make_session(db, project_id="project-alpha", title="Nested", prefix="nested")
    db.execute(
        "UPDATE threads SET agent_enabled=1 WHERE id IN (?,?,?)",
        (owner, other, conductor),
    )
    db.execute("UPDATE threads SET spawned_by=? WHERE id=?", (owner, worker))
    db.execute("UPDATE threads SET spawned_by=? WHERE id=?", (worker, nested))
    db.execute(
        "UPDATE projects SET conductor_thread_id=? WHERE id='project-alpha'",
        (conductor,),
    )
    db.commit()
    return {
        "workspace": workspace,
        "root": project_root,
        "category": category,
        "db": db,
        "owner": owner,
        "other": other,
        "conductor": conductor,
        "plain": plain,
        "worker": worker,
        "nested": nested,
    }


def _write(scope, name: str, content: str = "body\n", *, actor: str | None = None, **kwargs):
    from registry.durable_writes import write_text

    return write_text(
        scope["category"] / name,
        content,
        thread_id=actor or scope["owner"],
        project_id="project-alpha",
        workspace=scope["workspace"],
        description=f"Description for {name}",
        update_trigger="when source facts change",
        **kwargs,
    )


def _enforce(scope):
    from infra.settings import set_setting

    set_setting(
        "shared_ownership:project-alpha",
        {"shared_ownership_enforced_at": "2026-09-29T00:00:00Z"},
        db=scope["db"],
    )


def _event_types(db, artifact_id: str) -> list[str]:
    return [
        str(row[0])
        for row in db.execute(
            "SELECT event_type FROM artifact_registration_events "
            "WHERE artifact_id=? ORDER BY created_at,id",
            (artifact_id,),
        ).fetchall()
    ]


def _shell_write(path: Path, text: str, started_ns: int) -> None:
    """Write as a shell command that started at ``started_ns`` would.

    Linux stamps mtimes from a coarse kernel clock that can trail
    ``time.time_ns()`` by a tick, so a write made just after ``started_ns`` can
    read as older than it; a command's own write never predates its start.
    """
    path.write_text(text, encoding="utf-8")
    stat = path.stat()
    if stat.st_mtime_ns < started_ns:
        os.utime(path, ns=(stat.st_atime_ns, started_ns))


def test_acting_owner_resolves_agents_workers_user_and_plain_chat(ownership_scope):
    from infra.passive_changes import materialize_pending_digest
    from registry.shared_principals import UI_PRINCIPAL, acting_owner

    scope = ownership_scope
    db = scope["db"]
    assert acting_owner(scope["owner"], False, db=db) == scope["owner"]
    assert acting_owner(scope["worker"], False, db=db) == scope["owner"]
    assert acting_owner(scope["nested"], False, db=db) == scope["owner"]
    assert acting_owner(scope["plain"], False, db=db) is None
    assert acting_owner(scope["plain"], True, db=db) == UI_PRINCIPAL
    assert materialize_pending_digest(db, UI_PRINCIPAL) is None


def test_creation_requires_owner_and_trigger_and_infers_dated_trigger(ownership_scope):
    from registry.durable_writes import write_text
    from registry.shared_write_errors import DurableWritePolicyError
    from registry.identity_metadata import parse_metadata

    scope = ownership_scope
    common = {
        "project_id": "project-alpha",
        "workspace": scope["workspace"],
        "description": "A durable record",
    }
    with pytest.raises(DurableWritePolicyError, match="update_trigger"):
        write_text(
            scope["category"] / "missing-trigger.md",
            "body\n",
            thread_id=scope["owner"],
            **common,
        )
    with pytest.raises(DurableWritePolicyError, match="requires `owner`"):
        write_text(
            scope["category"] / "missing-owner.md",
            "body\n",
            thread_id=scope["plain"],
            update_trigger="when facts change",
            **common,
        )

    explicit = write_text(
        scope["category"] / "explicit.md",
        "body\n",
        thread_id=scope["plain"],
        owner="Documents",
        update_trigger="when facts change",
        **common,
    )
    dated_dir = scope["category"] / "2026-09-29-run"
    dated_dir.mkdir()
    dated = write_text(
        dated_dir / "result.md",
        "body\n",
        thread_id=scope["owner"],
        **common,
    )
    dated_name = write_text(
        scope["category"] / "2026-09-29-record.md",
        "body\n",
        thread_id=scope["owner"],
        **common,
    )
    editor = write_text(
        scope["category"] / "editor.md",
        "body\n",
        thread_id=scope["owner"],
        direct_user=True,
        **common,
    )
    dated_editor = write_text(
        scope["category"] / "2026-09-29-editor.md",
        "body\n",
        thread_id=scope["owner"],
        direct_user=True,
        update_trigger="ignored for editor writes",
        **common,
    )

    assert explicit["artifact"]["parent_thread_id"] == scope["owner"]
    assert parse_metadata(explicit["artifact"]["metadata"])["update_trigger"] == "when facts change"
    assert parse_metadata(dated["artifact"]["metadata"])["update_trigger"] == "none: dated record"
    assert parse_metadata(dated_name["artifact"]["metadata"])["update_trigger"] == "none: dated record"
    assert "update_trigger" not in parse_metadata(editor["artifact"]["metadata"])
    assert parse_metadata(dated_editor["artifact"]["metadata"])["update_trigger"] == "none: dated record"


def test_non_owner_is_refused_and_conductor_and_user_edits_are_recorded(ownership_scope):
    from registry.durable_writes import write_text
    from registry.shared_write_errors import DurableWritePolicyError

    scope = ownership_scope
    created = _write(scope, "gate.md", "first\n")
    artifact_id = created["artifact"]["id"]

    with pytest.raises(DurableWritePolicyError) as caught:
        _write(scope, "gate.md", "third\n", actor=scope["other"])
    assert "content_edited_by_non_owner" not in _event_types(scope["db"], artifact_id)
    refusal = str(caught.value)
    assert "call `doc_comment_create`" in refusal
    assert all(field in refusal for field in ("`doc_path`", "`quote`", "`replacement`"))
    assert f"`{scope['category'] / 'gate.md'}`" in refusal
    assert "owned by Documents" in refusal
    assert "Only its owner, the conductor, or the user edits it directly." in refusal
    assert "the owner applies it. To raise a point without a text change, comment instead." in refusal

    conductor_edit = _write(
        scope,
        "gate.md",
        "by conductor\n",
        actor=scope["conductor"],
    )
    assert conductor_edit["artifact"]["parent_thread_id"] == scope["owner"]
    assert "content_edited_by_non_owner" in _event_types(scope["db"], artifact_id)
    user_edit = write_text(
        scope["category"] / "gate.md",
        "by user\n",
        thread_id=scope["plain"],
        project_id="project-alpha",
        workspace=scope["workspace"],
        direct_user=True,
    )
    assert user_edit["artifact"]["parent_thread_id"] == scope["owner"]


@pytest.mark.asyncio
async def test_file_tools_and_promotion_reach_shared_owner_gate(ownership_scope, monkeypatch):
    from tools import filesystem_edit, filesystem_rw
    from tools.filesystem_edit import EditFileTool, MultiEditTool
    from tools.filesystem_gate import AuthorizedPath
    from tools.filesystem_rw import WriteFileTool
    from tools.promote import PromoteArtifactTool

    scope = ownership_scope

    async def authorize(*, path, **_kwargs):
        return AuthorizedPath(Path(path).resolve(strict=False), reviewed=False)

    monkeypatch.setattr(filesystem_rw, "_resolve_path_for_action", authorize)
    monkeypatch.setattr(filesystem_edit, "_resolve_path_for_action", authorize)
    write_schema = WriteFileTool(workspace=scope["workspace"]).parameters["properties"]
    assert "Required when creating a new shared file" in write_schema["description"]["description"]
    assert "Required when creating a new shared file" in write_schema["update_trigger"]["description"]
    promote_schema = PromoteArtifactTool(workspace=scope["root"]).parameters["properties"]
    assert "Required for a new shared artifact" in promote_schema["update_trigger"]["description"]
    target = scope["category"] / "tool-gate.md"
    _write(scope, target.name, "alpha beta gamma\n")
    context = {
        "_thread_id": scope["conductor"],
        "_thread_project_id": "project-alpha",
    }
    written = json.loads(await WriteFileTool(workspace=scope["workspace"]).execute(
        path=str(target), content="ALPHA beta gamma\n", **context,
    ))
    edited = json.loads(await EditFileTool(workspace=scope["workspace"]).execute(
        path=str(target), old_string="beta", new_string="BETA", **context,
    ))
    multi = json.loads(await MultiEditTool(workspace=scope["workspace"]).execute(
        path=str(target),
        edits=[
            {"old_string": "ALPHA", "new_string": "Alpha"},
            {"old_string": "gamma", "new_string": "GAMMA"},
        ],
        **context,
    ))
    assert all("ownership_warning" not in receipt for receipt in (written, edited, multi))
    assert target.read_text(encoding="utf-8") == "Alpha BETA GAMMA\n"

    source = scope["root"] / ".threads" / scope["owner"] / "working" / "promoted.md"
    source.parent.mkdir(parents=True)
    source.write_text("promoted\n", encoding="utf-8")
    promoted = json.loads(await PromoteArtifactTool(workspace=scope["root"]).execute(
        path=str(source),
        category="documents",
        description="Promoted through the agent tool",
        update_trigger="when promotion inputs change",
        _thread_id=scope["owner"],
    ))
    assert promoted["ok"] is True
    assert promoted["artifact"]["parent_thread_id"] == scope["owner"]
    assert not source.exists()


def test_enforcement_claims_unowned_file_for_conductor(ownership_scope):
    from registry import service
    from registry.shared_write_errors import DurableWritePolicyError

    scope = ownership_scope
    target = scope["category"] / "legacy.md"
    target.write_text("legacy\n", encoding="utf-8")
    _enforce(scope)

    with pytest.raises(DurableWritePolicyError):
        _write(scope, target.name, "attempt\n", actor=scope["other"])
    artifact = service.find_file_identity(str(target), db=scope["db"])
    assert artifact is not None
    assert scope["db"].execute(
        "SELECT parent_thread_id FROM artifact_registrations "
        "WHERE artifact_id=? AND relationship_state LIKE 'owner-%'",
        (artifact["id"],),
    ).fetchone()["parent_thread_id"] == scope["conductor"]


def test_warn_only_category_append_registers_and_records_index_identity(ownership_scope):
    from registry import service
    from registry.identity_metadata import parse_metadata

    scope = ownership_scope
    index = scope["category"] / "INDEX.md"
    assert service.find_file_identity(str(index), db=scope["db"]) is None

    _write(scope, "indexed.md", "body\n", actor=scope["other"])

    artifact = service.find_file_identity(str(index), db=scope["db"])
    assert artifact is not None
    assert parse_metadata(artifact["metadata"])["last_written_sha256"] == hashlib.sha256(
        index.read_bytes()
    ).hexdigest()
    assert scope["db"].execute(
        "SELECT parent_thread_id FROM artifact_registrations "
        "WHERE artifact_id=? AND relationship_state LIKE 'owner-%'",
        (artifact["id"],),
    ).fetchone()["parent_thread_id"] == scope["conductor"]
    assert "content_edited_by_non_owner" in _event_types(scope["db"], artifact["id"])
    assert scope["db"].execute(
        "SELECT COUNT(*) FROM context_change_journal WHERE object_kind='artifact' "
        "AND object_id=? AND change_kind='content_edited_by_non_owner'",
        (artifact["id"],),
    ).fetchone()[0] == 1


def test_mechanical_index_append_allowed_but_content_rewrite_refused(ownership_scope):
    from registry import service
    from registry.durable_writes import write_text
    from registry.shared_write_errors import DurableWritePolicyError

    scope = ownership_scope
    index = scope["category"] / "INDEX.md"
    artifact = service.register_write_strict(
        scope["conductor"], str(index), db=scope["db"]
    )
    _enforce(scope)
    common = {
        "thread_id": scope["other"],
        "project_id": "project-alpha",
        "workspace": scope["workspace"],
    }
    with pytest.raises(DurableWritePolicyError):
        write_text(index, "# Documents\n- `new.md` — Not published yet\n", **common)
    (scope["category"] / "new.md").write_text("body\n", encoding="utf-8")
    with pytest.raises(DurableWritePolicyError):
        write_text(index, "# Documents\nOwners must defer to me.\n", **common)
    appended = write_text(index, "# Documents\n- `new.md` — New\n", **common)
    assert appended["artifact"]["id"] == artifact["id"]
    assert "content_edited_by_non_owner" in _event_types(scope["db"], artifact["id"])
    with pytest.raises(DurableWritePolicyError):
        write_text(index, "# Documents\n- `new.md` — Rewritten\n", **common)


def test_classifier_table_and_diff_cap(tmp_path):
    from registry.shared_edit_classification import capped_unified_diff, classify_edit

    index = tmp_path / "INDEX.md"
    doc = tmp_path / "doc.md"
    (tmp_path / "a.md").write_text("a\n", encoding="utf-8")
    cases = (
        (index, b"# Index\n", b"# Index\n- `[a](a.md)`\n", "mechanical"),
        (index, b"# Index\n", b"# Index\n\n| [a](a.md) | A |\n", "mechanical"),
        (index, b"# Index\n", b"# Index\n- `missing.md` - M\n", "content"),
        (index, b"# Index\n", b"# Index\nAny prose at all\n", "content"),
        (index, b"# Index\n", b"# Index\n## New section\n", "content"),
        (index, b"- `[a](a.md)` old\n", b"- `[a](a.md)` new\n", "content"),
        (doc, b"**Review:** old\n\nBody\n", b"**Review:** new\n\nBody\n", "mechanical"),
        (doc, b"See [A](old.md)\n", b"See [A](new.md)\n", "mechanical"),
        (index, b"- `old.md` - A\n", b"- `new.md` - A\n", "mechanical"),
        (doc, b"Run `old`\n", b"Run `new`\n", "content"),
        (doc, b"old\n", b"new\n", "content"),
    )
    for path, before, after, expected in cases:
        assert classify_edit(path, before, after) == expected
    diff = capped_unified_diff(doc, b"a\n", ("x" * 40_000).encode())
    assert len(diff.encode()) <= 16 * 1024 + 128
    assert "truncated" in diff


class _ShellProcess:
    def __init__(self) -> None:
        self.pid = 999_996
        self.returncode = 0

    async def communicate(self):
        return b"shell wrote\n", b""

    async def wait(self):
        return self.returncode

    def kill(self):
        self.returncode = -9


@pytest.mark.asyncio
async def test_bash_callsite_annotates_and_delivers_owner_note(
    ownership_scope,
    monkeypatch,
):
    from core.turn_core import TurnIdentity
    from host_access import processes
    from tools import shell as shell_module
    from tools.shell import BashTool

    scope = ownership_scope
    created = _write(scope, "bash-callsite.md", "first\n")
    target = scope["category"] / "bash-callsite.md"

    async def create_subprocess(*_args, **_kwargs):
        _shell_write(target, "changed by shell\n", time.time_ns())
        return _ShellProcess()

    monkeypatch.setattr(shell_module.asyncio, "create_subprocess_exec", create_subprocess)
    monkeypatch.setattr(processes, "_process_creation_fingerprint", lambda _pid: "fingerprint")
    monkeypatch.setattr(shell_module, "list_registered_repo_policies", lambda: [])
    from tests.shell_platform_helpers import use_mac_shell
    use_mac_shell(monkeypatch)
    monkeypatch.setattr(shell_module, "is_safehouse_available", lambda: True)
    monkeypatch.setattr(shell_module, "safehouse_version_error", lambda: None)
    monkeypatch.setattr(shell_module, "is_path_allowed", lambda *_a, **_k: True)
    monkeypatch.setattr(shell_module, "execution_envelope_profile", lambda *_a, **_k: "")
    monkeypatch.setattr(
        shell_module,
        "safehouse_argv",
        lambda command, *_a, **_k: ["safehouse", "--", "bash", "-c", command],
    )
    monkeypatch.setattr(shell_module, "check_worktree_targets", lambda *_a: None)
    monkeypatch.setattr(shell_module, "check_command_safety", lambda *_a: None)

    result = await BashTool(working_dir=str(scope["root"])).execute(
        "printf changed >> shared/documents/bash-callsite.md",
        _thread_id=scope["other"],
        _thread_project_id="project-alpha",
        _host_access_turn_identity=TurnIdentity("shared-shell-turn", 1),
    )

    assert "outside the file tools" in result
    assert "has been told" in result
    assert "ownership_bypass_write" in _event_types(scope["db"], created["artifact"]["id"])
    assert scope["db"].execute(
        "SELECT COUNT(*) FROM context_change_journal WHERE object_kind='artifact' "
        "AND object_id=? AND change_kind='ownership_bypass_write'",
        (created["artifact"]["id"],),
    ).fetchone()[0] == 1


def test_shell_bypass_detects_foreign_but_not_owner_or_sanctioned_write(ownership_scope):
    from registry.identity_metadata import parse_metadata
    from tools.shared_write_detection import detect_shell_shared_writes

    scope = ownership_scope
    first = _write(scope, "shell.md", "first\n")
    target = scope["category"] / "shell.md"
    started = time.time_ns()
    _shell_write(target, "shell edit\n", started)
    notices = detect_shell_shared_writes(
        scope["other"], "project-alpha", "printf edit > shared/documents/shell.md", started,
        scope["root"],
    )
    assert len(notices) == 1
    assert "outside the file tools" in notices[0]
    assert "ownership_bypass_write" in _event_types(scope["db"], first["artifact"]["id"])

    started = time.time_ns()
    target.write_text("owner shell edit\n", encoding="utf-8")
    assert detect_shell_shared_writes(
        scope["owner"], "project-alpha", "printf edit > shared/documents/shell.md", started,
        scope["root"],
    ) == []

    started = time.time_ns()
    sanctioned = _write(scope, "shell.md", "sanctioned\n")
    assert detect_shell_shared_writes(
        scope["other"], "project-alpha", "touch shared/documents/*.md", started, scope["root"],
    ) == []
    assert parse_metadata(sanctioned["artifact"]["metadata"])["last_written_sha256"] == hashlib.sha256(
        b"sanctioned\n"
    ).hexdigest()
    assert scope["db"].execute(
        "SELECT COUNT(*) FROM artifact_registration_events "
        "WHERE artifact_id=? AND event_type='ownership_bypass_write' "
        "AND reason='outside CORSIA'",
        (first["artifact"]["id"],),
    ).fetchone()[0] == 1


@pytest.mark.asyncio
async def test_shell_detection_takes_the_writer_only_to_record_a_bypass(
    ownership_scope, monkeypatch,
):
    from tools import shared_write_detection as detection

    scope = ownership_scope
    _write(scope, "quiet.md", "first\n")
    target = scope["category"] / "quiet.md"
    real_transaction = detection.write_transaction
    taken: list[str] = []

    def counting_transaction(db, *, scope):
        taken.append(scope)
        return real_transaction(db, scope=scope)

    monkeypatch.setattr(detection, "write_transaction", counting_transaction)
    root = scope["root"]
    started = time.time_ns()
    assert detection.detect_shell_shared_writes(
        scope["other"], "project-alpha", "ls", started, root,
    ) == []
    target.write_text("owner edit\n", encoding="utf-8")
    assert detection.detect_shell_shared_writes(
        scope["owner"], "project-alpha", "printf x >> shared/documents/quiet.md", started, root,
    ) == []
    assert taken == []

    _shell_write(target, "foreign edit\n", started)
    assert len(detection.detect_shell_shared_writes(
        scope["other"], "project-alpha", "cd shared/documents && printf x >> quiet.md", started, root,
    )) == 1
    assert taken == ["detect_shell_shared_writes"]

    async def run(fn, *args):
        return fn(*args)

    assert await detection.collect_shell_shared_write_notices(
        "no-such-thread", "project-alpha", "ls", started, root, runner=run,
    ) == []


def test_concurrent_shared_write_is_not_blamed_on_a_command_that_never_names_it(
    ownership_scope,
):
    """#3213: an mtime inside the command's window is not evidence of authorship."""
    from tools.shared_write_detection import detect_shell_shared_writes

    scope = ownership_scope
    first = _write(scope, "concurrent.md", "first\n")
    target = scope["category"] / "concurrent.md"
    started = time.time_ns()
    _shell_write(target, "another writer\n", started)
    for command in (
        "git show origin/dev:shared/documents/concurrent.md",
        "sed -n 1,5p README.md && git fetch origin",
        "cat shared/documents/other.md > /tmp/copy.md",
    ):
        assert detect_shell_shared_writes(
            scope["other"], "project-alpha", command, started, scope["root"],
        ) == []
    assert "ownership_bypass_write" not in _event_types(scope["db"], first["artifact"]["id"])

    notices = detect_shell_shared_writes(
        scope["other"], "project-alpha", "printf x > " + str(target), started, "/",
    )
    assert len(notices) == 1
    assert "ownership_bypass_write" in _event_types(scope["db"], first["artifact"]["id"])


@pytest.mark.asyncio
async def test_moved_file_edit_tool_relocates_before_the_owner_gate_and_preserves_registration(
    ownership_scope,
    monkeypatch,
):
    scope = ownership_scope
    created = _write(scope, "old.md", "same bytes\n")
    artifact_id = created["artifact"]["id"]
    db = scope["db"]
    db.execute(
        "UPDATE artifact_registrations SET surfacing='quiet' "
        "WHERE artifact_id=? AND parent_thread_id=?",
        (artifact_id, scope["owner"]),
    )
    db.execute(
        "INSERT INTO artifact_registrations "
        "(id,artifact_id,parent_thread_id,relationship_state,tmp,surfacing,created_at,updated_at) "
        "VALUES (?,?,?,?,0,'visible','t','t')",
        ("subscriber-move", artifact_id, scope["other"], "subscriber-recent"),
    )
    db.execute(
        "INSERT INTO doc_comments "
        "(id,artifact_id,author,body,created_at,updated_at) VALUES (?,?,?,?,?,?)",
        ("move-comment", artifact_id, "Reviewer", "Keep this", "t", "t"),
    )
    db.commit()
    old_path = scope["category"] / "old.md"
    new_path = scope["category"] / "2026-09-29-renamed.md"
    old_path.rename(new_path)

    from tools import filesystem_edit
    from tools.filesystem_edit import EditFileTool
    from tools.filesystem_gate import AuthorizedPath

    async def authorize(*, path, **_kwargs):
        return AuthorizedPath(Path(path).resolve(strict=False), reviewed=False)

    monkeypatch.setattr(filesystem_edit, "_resolve_path_for_action", authorize)
    receipt = json.loads(await EditFileTool(workspace=scope["workspace"]).execute(
        path=str(new_path),
        old_string="same bytes",
        new_string="owner edit",
        _thread_id=scope["owner"],
        _thread_project_id="project-alpha",
    ))
    row = db.execute(
        "SELECT path FROM artifact_identities WHERE id=?", (artifact_id,)
    ).fetchone()
    registrations = db.execute(
        "SELECT parent_thread_id,surfacing,relationship_state "
        "FROM artifact_registrations WHERE artifact_id=? ORDER BY parent_thread_id",
        (artifact_id,),
    ).fetchall()
    assert receipt["artifact"]["id"] == artifact_id
    assert receipt["artifact"]["parent_thread_id"] == scope["owner"]
    assert "ownership_warning" not in receipt
    assert row["path"] == str(new_path)
    assert {(r[0], r[1], r[2]) for r in registrations} == {
        (scope["owner"], "quiet", "owner-active"),
        (scope["other"], "visible", "subscriber-recent"),
    }
    assert "relocated" in _event_types(db, artifact_id)
    assert db.execute(
        "SELECT body FROM doc_comments WHERE id='move-comment'"
    ).fetchone()["body"] == "Keep this"


def test_enforced_moved_file_never_transfers_to_conductor(ownership_scope):
    from registry.shared_write_errors import DurableWritePolicyError

    scope = ownership_scope
    created = _write(scope, "enforced-old.md", "same bytes\n")
    artifact_id = created["artifact"]["id"]
    old_path = scope["category"] / "enforced-old.md"
    new_path = scope["category"] / "enforced-new.md"
    old_path.rename(new_path)
    _enforce(scope)

    with pytest.raises(DurableWritePolicyError):
        _write(scope, new_path.name, "blocked edit\n", actor=scope["other"])
    artifact = scope["db"].execute(
        "SELECT path FROM artifact_identities WHERE id=?", (artifact_id,)
    ).fetchone()
    owner = scope["db"].execute(
        "SELECT parent_thread_id FROM artifact_registrations "
        "WHERE artifact_id=? AND relationship_state LIKE 'owner-%'",
        (artifact_id,),
    ).fetchone()
    assert artifact["path"] == str(new_path)
    assert owner["parent_thread_id"] == scope["owner"]
    assert new_path.read_text(encoding="utf-8") == "same bytes\n"


def test_ambiguous_and_edited_moves_fall_back_and_registration_seeds_hash(
    ownership_scope,
    caplog,
):
    from registry import service
    from registry.identity_metadata import parse_metadata
    from registry.moved_files import recover_moved_file_identity

    scope = ownership_scope
    one = _write(scope, "same-one.md", "duplicate\n")["artifact"]
    two = _write(scope, "same-two.md", "duplicate\n")["artifact"]
    (scope["category"] / "same-one.md").unlink()
    (scope["category"] / "same-two.md").unlink()
    ambiguous = scope["category"] / "ambiguous.md"
    ambiguous.write_text("duplicate\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert recover_moved_file_identity(
            ambiguous, actor_thread_id=scope["other"], db=scope["db"]
        ) is None
    assert one["id"] in caplog.text and two["id"] in caplog.text

    fallback = service.register_write_strict(
        scope["other"], str(ambiguous), db=scope["db"]
    )
    metadata = parse_metadata(fallback["metadata"])
    assert metadata["last_written_sha256"] == hashlib.sha256(b"duplicate\n").hexdigest()

    moved = _write(scope, "edited-before-detection.md", "old\n")["artifact"]
    old_path = scope["category"] / "edited-before-detection.md"
    changed_path = scope["category"] / "changed-before-detection.md"
    old_path.rename(changed_path)
    changed_path.write_text("changed\n", encoding="utf-8")
    assert recover_moved_file_identity(
        changed_path, actor_thread_id=scope["other"], db=scope["db"]
    ) is None
    assert scope["db"].execute(
        "SELECT path FROM artifact_identities WHERE id=?", (moved["id"],)
    ).fetchone()["path"] == str(old_path)


def test_conductor_relocation_keeps_owner_and_foreign_tray_stays_forbidden(ownership_scope):
    from registry.relocate import RelocationError, relocate_artifact

    scope = ownership_scope
    artifact = _write(scope, "relocate.md", "body\n")["artifact"]
    artifact_id = artifact["id"]
    db = scope["db"]
    db.execute(
        "UPDATE artifact_registrations SET surfacing='quiet' "
        "WHERE artifact_id=? AND parent_thread_id=?",
        (artifact_id, scope["owner"]),
    )
    db.commit()
    target = scope["category"] / "relocated.md"
    moved = relocate_artifact(
        artifact_id,
        str(target),
        actor_thread_id=scope["conductor"],
        db=db,
    )
    owner = db.execute(
        "SELECT parent_thread_id,surfacing FROM artifact_registrations "
        "WHERE artifact_id=? AND relationship_state LIKE 'owner-%'",
        (artifact_id,),
    ).fetchone()
    assert moved["path"] == str(target)
    assert (owner["parent_thread_id"], owner["surfacing"]) == (scope["owner"], "quiet")
    assert "ownership_rehomed" not in _event_types(db, artifact_id)

    non_shared_path = scope["root"] / "lane-notes.md"
    non_shared_path.write_text("notes\n", encoding="utf-8")
    from registry import service

    non_shared = service.register_write_strict(
        scope["owner"], str(non_shared_path), db=db,
    )
    with pytest.raises(RelocationError, match="owning thread or the scope conductor"):
        relocate_artifact(
            non_shared["id"],
            str(scope["root"] / "moved-lane-notes.md"),
            actor_thread_id=scope["conductor"],
            db=db,
        )

    with pytest.raises(RelocationError, match="owning thread or the scope conductor"):
        relocate_artifact(
            artifact_id,
            str(scope["category"] / "non-owner.md"),
            actor_thread_id=scope["other"],
            db=db,
        )

    foreign = scope["root"] / ".threads" / scope["other"]
    foreign.mkdir(parents=True)
    with pytest.raises(RelocationError, match="another thread's private directory"):
        relocate_artifact(
            artifact_id,
            str(foreign / "relocated.md"),
            actor_thread_id=scope["conductor"],
            db=db,
        )
