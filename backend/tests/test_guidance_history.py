"""Commit-on-save guidance history (issue #2272, design §8 "0a").

R1: every guidance save is one commit of the saved bytes, authored by the
saver, with Reason/Evidence/Scope trailers. R2: a history fault never fails a
save and is always in its result. R3: outside edits are committed as
``Reason: unrecorded`` at the next guidance save or server start. R12: System
content is never guidance.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import threading
from pathlib import Path

import pytest

from infra.platform_paths import WorkspaceSelection
from tests.registry_helpers import make_session

SKILL_BODY = """---
name: demo-skill
description: A skill under test.
---

# Demo skill

Body text.
"""
_IDENTITY = ["-c", "user.email=t@t", "-c", "user.name=T"]


@pytest.fixture
def history_scope(isolated_db, tmp_path, monkeypatch):
    """A git workspace, an isolated database, and an owning thread."""
    from infra import config
    from tools import filesystem_edit, filesystem_rw, host_access_gate
    from tools.filesystem_gate import AuthorizedPath

    async def allow_file_action(**_kwargs):
        return host_access_gate.HostActionAuthorization()

    async def allow_file_path(*, params, **_kwargs):
        return AuthorizedPath(Path(params["path"]), reviewed=False)

    monkeypatch.setattr(host_access_gate, "authorize_host_action", allow_file_action)
    monkeypatch.setattr(filesystem_rw, "_resolve_path_for_action", allow_file_path)
    monkeypatch.setattr(filesystem_edit, "_resolve_path_for_action", allow_file_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(["init", "--quiet"], cwd=workspace)
    _git([*_IDENTITY, "commit", "--allow-empty", "-qm", "root"], cwd=workspace)
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    monkeypatch.setattr(
        config, "WORKSPACE_SELECTION",
        WorkspaceSelection("guidance-history-tests", workspace.resolve(), "Tests"),
    )
    from infra.database import get_db

    db = get_db()
    thread_id = make_session(db, title="History Tester", prefix="ghist")
    db.execute(
        "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
        ("workspace_context", '{"bootloader_files":["context/demo.md","context/shell.md","context/private.md"]}', "now"),
    )
    db.commit()
    db.close()
    return workspace, thread_id


def _git(args: list[str], *, cwd: Path) -> str:
    done = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return done.stdout


def _body(repo: Path, rev: str = "HEAD") -> str:
    return _git(["log", "-1", "--format=%B", rev], cwd=repo)


def _count(repo: Path) -> int:
    return int(_git(["rev-list", "--count", "HEAD"], cwd=repo))


def _skill(workspace: Path, name: str = "demo") -> Path:
    return workspace / "skills" / name / "SKILL.md"


def _history(workspace: Path, name: str = "demo") -> Path:
    return workspace / "context" / f"{name}.md"


def _enable_history_context() -> None:
    from infra.database import get_db

    db = get_db()
    try:
        db.execute(
            "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
            ("workspace_context", '{"bootloader_files":["context/demo.md"]}', "now"),
        )
        db.commit()
    finally:
        db.close()


def _save(workspace: Path, path: Path, text: str, **kwargs):
    from infra.guidance_git import save_guidance
    from infra.guidance_targets import guidance_target

    target = guidance_target(path, workspace)
    assert target is not None
    return save_guidance(
        {path: text.encode()}, workspace=workspace, target=target,
        author="Tester <t@agents.corsia.local>", **kwargs,
    )


# --- Classification (R12) -------------------------------------------------


@pytest.mark.parametrize(("relative", "scope"), [
    ("skills/demo/SKILL.md", "skill, workspace scope"),
    ("skills/demo/evals/cases.json", "skill, workspace scope"),
    ("skills/demo/corsia.yaml", "skill, workspace scope"),
    ("projects/alpha/skills/demo/SKILL.md", "skill, project scope"),
    (".threads/t1/skills/demo/SKILL.md", "skill, agent scope"),
    ("projects/alpha/.threads/t2/skills/demo/evals/a.json", "skill, agent scope"),
])
def test_skill_shapes_classify_by_scope(tmp_path, relative, scope):
    from core.guidance_paths import classify_guidance_path

    assert classify_guidance_path(tmp_path / relative, workspace=tmp_path, db=None) == scope


def test_system_and_ordinary_paths_are_not_guidance(isolated_db, tmp_path):
    from core.guidance_paths import classify_guidance_path
    from infra.database import get_db

    cases = [
        ".corsia/skills/demo/SKILL.md", ".corsia/context/10-INTENT-AUTHORITY.md",
        "skills/demo/NOTES.md", "skills/demo/assets/draft.md", "skills/demo/scripts/corsia.yaml",
        "shared/planning/plan.md",
        ".threads/t1/working/scratch.md", "README.md", "../outside/skills/demo/SKILL.md",
    ]
    db = get_db()
    try:
        for relative in cases:
            assert classify_guidance_path(tmp_path / relative, workspace=tmp_path, db=db) is None, relative
    finally:
        db.close()


def test_context_files_the_loaders_load_are_guidance(history_scope):
    from core.guidance_paths import classify_guidance_path
    from infra.database import get_db

    workspace, thread_id = history_scope
    intent = workspace / "context" / "10-INTENT.md"
    guide = workspace / "notes" / "guide.md"
    for path in (intent, guide):
        path.parent.mkdir()
        path.write_text("# guide\n")
    db = get_db()
    try:
        db.execute("UPDATE threads SET bootloader_files=? WHERE id=?", (f'["{guide}"]', thread_id))
        db.commit()
        assert classify_guidance_path(intent, workspace=workspace, db=db) == "context, workspace scope"
        assert classify_guidance_path(guide, workspace=workspace, db=db) == "context, agent scope"
        db.execute("UPDATE threads SET archived_at=CURRENT_TIMESTAMP WHERE id=?", (thread_id,))
        db.commit()
        from core.context_source_paths import configured_source_paths

        assert guide not in configured_source_paths(workspace, db)
    finally:
        db.close()


def test_a_symlink_to_a_file_outside_the_workspace_is_never_guidance_or_committed(history_scope, tmp_path):
    from core.guidance_paths import classify_guidance_path
    from infra.guidance_git import startup_sweep

    workspace, _ = history_scope
    secret = tmp_path / "outside" / "SKILL.md"
    secret.parent.mkdir()
    secret.write_text("PRIVATE KEY BYTES\n")
    _skill(workspace, "leak").parent.mkdir(parents=True)
    _skill(workspace, "leak").symlink_to(secret)
    (workspace / "skills" / "linked").symlink_to(secret.parent, target_is_directory=True)

    for path in (_skill(workspace, "leak"), _skill(workspace, "linked")):
        assert classify_guidance_path(path, workspace=workspace, db=None) is None, path
    assert _save(workspace, _history(workspace), "saved\n").outcome == "written"
    startup_sweep(workspace)
    assert _git(["ls-files", "context"], cwd=workspace).split() == ["context/demo.md"]
    assert "PRIVATE KEY BYTES" not in _git(["log", "--all", "-p"], cwd=workspace)


# --- Commit message -------------------------------------------------------


def test_one_detector_keeps_shas_uuids_and_urls_and_drops_credentials():
    from host_access.credential_sanitizer import contains_credential
    from infra.guidance_messages import commit_message as _message

    sha, uuid = "0123456789abcdef" * 2 + "01234567", "123e4567-e89b-12d3-a456-426614174000"
    url = "https://github.com/cebertowicz/corsia/pull/2364"
    assert not any(contains_credential(value) for value in (sha, uuid, url))
    token = "ghp_" + "A1b2" * 9
    assert contains_credential(token)

    message = _message("s", "skill, agent scope", "Why\nsecond line", f"{sha}, {uuid} {url} {token} prose")
    assert f"Evidence: {sha} {uuid} {url}\n" in message
    assert token not in message and "Evidence trimmed" in message
    assert message.endswith("Scope: skill, agent scope") and "Reason: Why\n" in message
    dropped = _message("s", "x", f"use {token}", None)
    assert "Reason: unrecorded" in dropped and "Reason dropped" in dropped and token not in dropped


# --- Tool saves (R1) ------------------------------------------------------


@pytest.mark.asyncio
async def test_write_file_commits_the_saved_bytes_as_the_saver(history_scope):
    from tools.filesystem_rw import WriteFileTool

    workspace, thread_id = history_scope
    skill = workspace / ".threads" / thread_id / "skills" / "demo" / "SKILL.md"
    result = await WriteFileTool(workspace=workspace).execute(
        str(skill), SKILL_BODY, reason="Drop the manual procedure", evidence="issue-2272",
        _thread_id=thread_id,
    )

    assert "Saved and committed" in result and "(skill, agent scope)" in result
    author = _git(["log", "-1", "--format=%an <%ae>|%cn <%ce>"], cwd=workspace)
    assert author.strip() == f"History Tester <{thread_id}@agents.corsia.local>|CORSIA <corsia@localhost>"
    body = _body(workspace)
    assert "Reason: Drop the manual procedure\nEvidence: issue-2272\nScope: skill, agent scope" in body
    assert _git(["show", f"HEAD:.threads/{thread_id}/skills/demo/SKILL.md"], cwd=workspace) == SKILL_BODY
    assert _count(workspace) == 2

    again = await WriteFileTool(workspace=workspace).execute(str(skill), SKILL_BODY + "x\n", _thread_id=thread_id)
    assert "Pass reason on your next guidance save" in again
    assert "Reason: unrecorded" in _body(workspace)


def test_save_guidance_delete_requires_the_expected_prior_hash(history_scope):
    from infra.guidance_git import digest, save_guidance
    from infra.guidance_targets import GuidanceTarget

    workspace, _thread_id = history_scope
    path = workspace / "context" / "demo.md"
    path.parent.mkdir()
    path.write_text("before\n")
    result = save_guidance(
        {}, workspace=workspace, target=GuidanceTarget("context, workspace scope"),
        author="Tester <t@agents.corsia.local>", reason="Remove obsolete guidance",
        deletes={path: digest("before\n")}, atomic=True,
    )

    assert result.outcome == "written"
    assert not path.exists()
    assert path.relative_to(workspace).as_posix() not in _git(["ls-tree", "-r", "--name-only", "HEAD"], cwd=workspace)


def test_save_guidance_delete_refuses_a_changed_destination(history_scope):
    from infra.guidance_git import digest, save_guidance
    from infra.guidance_targets import GuidanceTarget

    workspace, _thread_id = history_scope
    path = workspace / "context" / "demo.md"
    path.parent.mkdir()
    path.write_text("newer\n")
    result = save_guidance(
        {}, workspace=workspace, target=GuidanceTarget("context, workspace scope"),
        author="Tester <t@agents.corsia.local>", reason="Remove obsolete guidance",
        deletes={path: digest("before\n")}, atomic=True,
    )

    assert result.outcome == "stale"
    assert path.read_text() == "newer\n"


def test_atomic_delete_without_history_writes_nothing(history_scope, monkeypatch):
    from infra import guidance_git
    from infra.guidance_git import digest, save_guidance
    from infra.guidance_targets import GuidanceTarget

    workspace, _thread_id = history_scope
    path = workspace / "context" / "demo.md"
    path.parent.mkdir()
    path.write_text("keep\n")
    monkeypatch.setattr(guidance_git, "workspace_repository", lambda *_args, **_kwargs: (None, "off"))

    result = save_guidance(
        {}, workspace=workspace, target=GuidanceTarget("context, workspace scope"),
        author="Tester <t@agents.corsia.local>", deletes={path: digest("keep\n")}, atomic=True,
    )

    assert result.outcome == "history-off"
    assert path.read_text() == "keep\n"


def test_atomic_delete_with_a_target_fault_says_nothing_changed(history_scope):
    from infra.guidance_git import digest, save_guidance
    from infra.guidance_targets import GuidanceTarget

    workspace, _thread_id = history_scope
    path = workspace / "context" / "demo.md"
    path.parent.mkdir()
    path.write_text("keep\n")
    result = save_guidance(
        {}, workspace=workspace, target=GuidanceTarget("unknown", "classifier fault"),
        author="Tester <t@agents.corsia.local>", deletes={path: digest("keep\n")}, atomic=True,
    )

    assert (result.outcome, result.line) == ("failed", "Not saved: classifier fault.")
    assert path.read_text() == "keep\n"


def test_binary_safe_history_refuses_an_oversized_file_without_writing(history_scope):
    from infra.guidance_git import save_guidance
    from infra.guidance_targets import GuidanceTarget

    workspace, _thread_id = history_scope
    path = workspace / "context" / "large.bin"
    result = save_guidance(
        {path: b"\0" * (1024 * 1024 + 1)}, workspace=workspace,
        target=GuidanceTarget("context, workspace scope"), author="Tester <t@agents.corsia.local>",
        binary_safe_history=True,
    )

    assert (result.outcome, result.reason) == ("refused", "binary-too-large")
    assert not path.exists()


def test_binary_safe_history_refuses_an_oversized_file_before_a_non_atomic_target_fault(history_scope):
    from infra.guidance_git import save_guidance
    from infra.guidance_targets import GuidanceTarget

    workspace, _thread_id = history_scope
    path = workspace / "context" / "large.bin"
    result = save_guidance(
        {path: b"\0" * (1024 * 1024 + 1)}, workspace=workspace,
        target=GuidanceTarget("unknown", "classifier fault"), author="Tester <t@agents.corsia.local>",
        binary_safe_history=True,
    )

    assert (result.outcome, result.reason) == ("refused", "binary-too-large")
    assert not path.exists()


@pytest.mark.asyncio
async def test_edit_and_multi_edit_commit_on_a_recorded_base(history_scope):
    from tools.filesystem_edit import EditFileTool, MultiEditTool

    workspace, thread_id = history_scope
    skill = workspace / ".threads" / thread_id / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(SKILL_BODY)  # an outside edit: swept first, then edited

    edited = await EditFileTool(workspace=workspace).execute(
        str(skill), "Body text.", "Body text, edited.", reason="Clarify", _thread_id=thread_id,
    )
    multi = await MultiEditTool(workspace=workspace).execute(
        str(skill), [{"old_string": "edited.", "new_string": "edited twice."}], _thread_id=thread_id,
    )

    assert "Swept and committed" in edited and "Saved and committed" in edited
    assert "Saved and committed" in multi
    subjects = _git(["log", "--format=%s"], cwd=workspace).splitlines()
    assert [subject.split(":")[0] for subject in subjects] == ["guidance save", "guidance save", "guidance sweep", "root"]
    assert _git(["show", "HEAD~2:" + skill.relative_to(workspace).as_posix()], cwd=workspace) == SKILL_BODY
    assert _git(["status", "--porcelain"], cwd=workspace) == ""


@pytest.mark.asyncio
async def test_non_guidance_write_makes_no_commit(history_scope):
    from tools.filesystem_rw import WriteFileTool

    workspace, thread_id = history_scope
    result = await WriteFileTool(workspace=workspace).execute(
        str(workspace / "shared" / "notes.md"), "notes\n", _thread_id=thread_id,
    )
    assert "committed" not in result
    assert _count(workspace) == 1


def test_stale_edit_writes_nothing_and_says_so(history_scope):
    from infra.guidance_git import digest

    workspace, _ = history_scope
    skill = _history(workspace)
    _save(workspace, skill, "one\n")
    skill.write_text("moved\n")
    result = _save(workspace, skill, "two\n", expected={skill: digest("one\n")})
    assert result.outcome == "stale" and result.line.startswith("Error:")
    assert skill.read_text() == "moved\n"


@pytest.mark.parametrize("fallback", ["busy-lock", "history-off", "classifier-fault"])
def test_a_stale_edit_is_refused_on_every_fallback_path(history_scope, monkeypatch, fallback):
    from infra import guidance_git

    workspace, _ = history_scope
    skill = _history(workspace)
    _save(workspace, skill, "one\n")
    skill.write_text("moved\n")
    holder = os.open(workspace / ".git" / "corsia-guidance.lock", os.O_RDWR | os.O_CREAT)
    if fallback == "busy-lock":
        monkeypatch.setattr(guidance_git, "_LOCK_WAIT_SECONDS", 0.05)
        fcntl.flock(holder, fcntl.LOCK_EX)
    elif fallback == "history-off":
        monkeypatch.setattr(guidance_git, "workspace_repository", lambda *_args, **_kwargs: (None, "off"))
    else:
        monkeypatch.setattr("core.guidance_paths.classify_guidance_path", lambda *_args, **_kwargs: 1 / 0)
    try:
        result = _save(workspace, skill, "two\n", expected={skill: guidance_git.digest("one\n")})
    finally:
        os.close(holder)
    assert result.outcome == "stale" and "nothing was written" in result.line
    assert skill.read_text() == "moved\n"


def test_identical_save_adds_no_commit(history_scope):
    workspace, _ = history_scope
    _save(workspace, _history(workspace), "same\n")
    assert _save(workspace, _history(workspace), "same\n").outcome == "unchanged"
    assert _count(workspace) == 2


# --- The race family ------------------------------------------------------


def test_outside_write_after_the_write_stays_dirty_and_out_of_the_commit(history_scope):
    from infra.guidance_git import save_guidance
    from infra.guidance_targets import guidance_target

    workspace, _ = history_scope
    skill = _history(workspace)

    def write_then_outside_edit(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.write_text("outside\n")  # lands before the commit

    result = save_guidance(
        {skill: b"saver\n"}, workspace=workspace, target=guidance_target(skill, workspace),
        author="Tester <t@a>", write=write_then_outside_edit,
    )
    assert result.outcome == "written"
    assert _git(["show", "HEAD:context/demo.md"], cwd=workspace) == "saver\n"
    assert skill.read_text() == "outside\n"
    assert _git(["status", "--porcelain", "--", "context"], cwd=workspace).strip() == "M context/demo.md"


def test_two_concurrent_saves_to_one_file_make_two_correct_commits(history_scope):
    workspace, _ = history_scope
    skill = _history(workspace)
    start = threading.Barrier(2)
    results = []

    def save(text: str) -> None:
        start.wait()
        results.append(_save(workspace, skill, text))

    workers = [threading.Thread(target=save, args=(text,)) for text in ("a\n", "b\n")]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=60)

    assert sorted(result.outcome for result in results) == ["written", "written"]
    blobs = {_git(["show", f"{result.sha}:context/demo.md"], cwd=workspace) for result in results}
    assert blobs == {"a\n", "b\n"}
    assert _count(workspace) == 3
    assert _git(["show", "HEAD:context/demo.md"], cwd=workspace) == skill.read_text()


# --- Faults never fail a save (R2) -----------------------------------------


def test_a_busy_lock_still_saves_and_shows_the_failure(history_scope, monkeypatch):
    from infra import guidance_git

    workspace, _ = history_scope
    monkeypatch.setattr(guidance_git, "_LOCK_WAIT_SECONDS", 0.05)
    holder = os.open(workspace / ".git" / "corsia-guidance.lock", os.O_RDWR | os.O_CREAT)
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        result = _save(workspace, _history(workspace), "held\n")
    finally:
        os.close(holder)
    assert (result.outcome, result.reason) == ("deferred", "deferred-guidance-lock")
    assert "the commit failed" in result.line and "not in history" in result.line
    assert _history(workspace).read_text() == "held\n"
    assert _count(workspace) == 1


def test_a_commit_fault_retries_once_then_saves_with_the_failure(history_scope, monkeypatch):
    from infra import guidance_git

    workspace, _ = history_scope
    real, calls = guidance_git._git, []

    def failing(repo, *args, **kwargs):
        if args[0] == "commit-tree":
            calls.append(args)
            raise guidance_git.GitFault("git commit-tree failed: disk full")
        return real(repo, *args, **kwargs)

    monkeypatch.setattr(guidance_git, "_git", failing)
    result = _save(workspace, _history(workspace), "kept\n")
    assert result.outcome == "failed" and len(calls) == 2
    assert "disk full" in result.line and "Report this" in result.line
    assert _history(workspace).read_text() == "kept\n"
    assert _count(workspace) == 1


def test_commit_fault_restores_a_deleted_file(history_scope, monkeypatch):
    from infra import guidance_git
    from infra.guidance_git import digest, save_guidance
    from infra.guidance_targets import GuidanceTarget

    workspace, _thread_id = history_scope
    path = workspace / "context" / "demo.md"
    path.parent.mkdir()
    path.write_text("restore me\n")
    monkeypatch.setattr(
        guidance_git, "_commit", lambda *_args, **_kwargs: (_ for _ in ()).throw(guidance_git.GitFault("disk full")),
    )

    result = save_guidance(
        {}, workspace=workspace, target=GuidanceTarget("context, workspace scope"),
        author="Tester <t@agents.corsia.local>", deletes={path: digest("restore me\n")}, atomic=True,
    )

    assert result.outcome == "failed"
    assert path.read_text() == "restore me\n"


def test_a_status_fault_is_visible_never_clean(history_scope, monkeypatch):
    from infra import guidance_git

    workspace, _ = history_scope
    real = guidance_git._git

    def failing(repo, *args, **kwargs):
        if args[0] == "status":
            raise guidance_git.GitFault("git status failed: corrupt index")
        return real(repo, *args, **kwargs)

    monkeypatch.setattr(guidance_git, "_git", failing)
    result = _save(workspace, _history(workspace), "text\n")
    assert result.outcome == "failed" and "corrupt index" in result.line
    assert _history(workspace).read_text() == "text\n"


def test_an_index_sync_fault_keeps_the_commit_and_reports_a_partial_save(history_scope, monkeypatch):
    from infra import guidance_git, product_events

    workspace, _ = history_scope
    real, events = guidance_git._git, []

    def failing(repo, *args, env=None, **kwargs):
        if args[0] == "update-index" and env is None:
            raise guidance_git.GitFault("git update-index failed: index.lock exists")
        return real(repo, *args, env=env, **kwargs)

    monkeypatch.setattr(guidance_git, "_git", failing)
    monkeypatch.setattr(product_events, "emit_product_event", lambda *args, **kwargs: events.append(kwargs["properties"]))
    result = _save(workspace, _history(workspace), "text\n")
    rel = _history(workspace).relative_to(workspace).as_posix()

    assert (result.outcome, result.reason, result.unsynced) == ("written", "index-unsynced", (rel,))
    assert _count(workspace) == 2
    assert "Saved and committed" not in result.line
    assert f"Partly saved: commit {result.sha[:7]} landed" in result.line and rel in result.line
    assert "index.lock exists" in result.line
    assert events[-1]["index_unsynced"] == [rel]


def test_file_version_reads_a_status_fault_as_a_fault_never_clean(history_scope, monkeypatch):
    from infra import guidance_git

    workspace, _ = history_scope
    skill = _history(workspace)
    _save(workspace, skill, "text\n")
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=workspace, capture_output=True, text=True, check=True).stdout.strip()
    assert guidance_git.file_version(workspace, skill) == sha
    skill.write_text("edited\n")
    assert guidance_git.file_version(workspace, skill) == f"{sha}·dirty"

    real = guidance_git._git

    def failing(repo, *args, **kwargs):
        if args[0] == "status":
            raise guidance_git.GitFault("git status failed: corrupt index")
        return real(repo, *args, **kwargs)

    monkeypatch.setattr(guidance_git, "_git", failing)
    with pytest.raises(guidance_git.GitFault, match="corrupt index"):
        guidance_git.file_version(workspace, skill)


def test_a_classifier_fault_saves_and_says_history_may_be_missing(history_scope, monkeypatch):
    from infra.guidance_git import save_guidance
    from infra.guidance_targets import guidance_target

    workspace, _ = history_scope

    def broken(*_args, **_kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr("core.guidance_paths.classify_guidance_path", broken)
    target = guidance_target(_history(workspace), workspace)
    result = save_guidance({_history(workspace): b"x\n"}, workspace=workspace, target=target, author="a <a@a>")
    assert result.outcome == "failed" and "History may be missing" in result.line
    assert _history(workspace).read_text() == "x\n"


# --- Outside edits (R3) and the repository rule ---------------------------


def test_shell_edits_are_swept_unrecorded_at_the_next_save_and_at_startup(history_scope):
    from infra.guidance_git import startup_sweep

    workspace, _ = history_scope
    shell = _history(workspace, "shell")
    shell.parent.mkdir(parents=True)
    shell.write_text("from a shell\n")
    (workspace / "notes.md").write_text("not guidance\n")

    result = _save(workspace, _history(workspace), "saved\n")

    assert "Swept and committed context/shell.md" in result.line
    assert "Reason: unrecorded\nScope: sweep" in _body(workspace)
    assert _git(["show", "HEAD:context/shell.md"], cwd=workspace) == "from a shell\n"
    assert _git(["status", "--porcelain"], cwd=workspace).strip() == "?? notes.md"

    shell.unlink()
    startup_sweep(workspace)
    assert "Reason: unrecorded" in _body(workspace)
    assert "context/shell.md" not in _git(["ls-files"], cwd=workspace)


@pytest.mark.asyncio
async def test_an_outside_edit_after_an_agent_save_is_not_credited_to_the_agent(history_scope):
    from tools.filesystem_rw import WriteFileTool

    workspace, thread_id = history_scope
    skill = workspace / ".threads" / thread_id / "skills" / "demo" / "SKILL.md"
    await WriteFileTool(workspace=workspace).execute(str(skill), SKILL_BODY, _thread_id=thread_id)
    skill.write_text(SKILL_BODY + "from a shell\n")
    _save(workspace, _history(workspace), "other\n")
    swept = _git(["log", "-1", "--format=%an <%ae>", "--", skill.relative_to(workspace).as_posix()], cwd=workspace)
    assert swept.strip() == "Unknown editor <outside-edit@corsia.local>"


@pytest.mark.asyncio
async def test_a_guidance_save_never_runs_on_the_shared_db_executor():
    from infra.db_async import run_off_db

    assert not (await run_off_db(lambda: threading.current_thread().name)).startswith("db")


def test_git_init_happens_at_the_first_save_never_at_startup(isolated_db, tmp_path):
    from infra.guidance_git import startup_sweep

    workspace = tmp_path / "fresh"
    workspace.mkdir()
    _enable_history_context()
    startup_sweep(workspace)
    assert not (workspace / ".git").exists()
    assert _save(workspace, _history(workspace), "x\n").outcome == "written"
    assert (workspace / ".git").is_dir() and _count(workspace) == 1


def test_an_enclosing_repository_is_never_adopted(isolated_db, tmp_path):
    outer = tmp_path / "outer"
    workspace = outer / "workspace"
    workspace.mkdir(parents=True)
    _git(["init", "--quiet"], cwd=outer)
    _enable_history_context()
    result = _save(workspace, _history(workspace), "x\n")
    assert result.outcome == "history-off" and "enclosing repository" in result.line
    assert _history(workspace).read_text() == "x\n"
    assert not (workspace / ".git").exists()


def test_an_ignored_guidance_path_reports_history_off(history_scope):
    workspace, _ = history_scope
    (workspace / ".gitignore").write_text("context/private.md\n")
    result = _save(workspace, _history(workspace, "private"), "x\n")
    assert result.outcome == "history-off" and "ignored" in result.line


def test_a_committed_save_schedules_carry_in_after_history_lands(history_scope, monkeypatch):
    workspace, _ = history_scope
    scheduled: list[tuple[Path, tuple[Path, ...], str]] = []
    monkeypatch.setattr(
        "core.lesson_carry_in.record_committed_save",
        lambda saved_workspace, paths, commit: scheduled.append((saved_workspace, paths, commit)),
    )

    path = _history(workspace)
    result = _save(workspace, path, "carry-in target\n")

    assert result.outcome == "written"
    assert scheduled == [(workspace, (path,), result.sha)]


def test_forty_save_burst_reports_lock_wait_p95(history_scope, monkeypatch):
    workspace, _ = history_scope
    events: list[dict] = []
    # This stress test owns concurrent Git-save behavior, not post-save C1 execution.
    # Suppress its daemon drains so their independent DB work cannot race the test DB authorizer.
    monkeypatch.setattr("core.lesson_carry_in.record_committed_save", lambda *_args: None)
    monkeypatch.setattr(
        "infra.product_events.emit_product_event",
        lambda category, action, **kwargs: events.append({"event": f"{category}.{action}", **kwargs["properties"]}),
    )
    # A loaded host can push the 40th wait past the bound; measure it, never defer (tested above).
    monkeypatch.setattr("infra.guidance_git._LOCK_WAIT_SECONDS", 600.0)
    workers = [
        threading.Thread(target=_save, args=(
            workspace, _skill(workspace, f"burst{index:02d}"),
            SKILL_BODY.replace("demo-skill", f"burst{index:02d}").replace("Body text.", f"Burst {index}."),
        ))
        for index in range(40)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=120)

    assert not any(worker.is_alive() for worker in workers)
    assert len(events) == 40 and {event["event"] for event in events} == {"guidance_history.save"}
    assert {event["outcome"] for event in events} == {"written"}
    # Lock wait scales with host load, so it is reported, not bounded.
    assert all(event["lock_wait_ms"] >= 0 and event["commit_ms"] >= 0 for event in events)
    waits = sorted(event["lock_wait_ms"] for event in events)
    print(f"40-save burst lock-wait p95: {waits[int(0.95 * (len(waits) - 1))]:.0f} ms")
    assert _count(workspace) == 41


# --- Document editor (integration) ----------------------------------------


@pytest.mark.integration
def test_editor_save_of_a_guidance_file_commits_as_the_user(integration_client, integration_workspace):
    """The doc-editor PUT commits one guidance save authored by the user."""
    from auth_fixtures import OWNER_TOKEN

    workspace = integration_workspace
    _git(["init", "--quiet"], cwd=workspace)  # history home for this test
    auth = "?" + "token=" + OWNER_TOKEN

    session = integration_client.post("/api/threads" + auth, json={"title": "editor-guidance", "project_id": None})
    assert session.status_code == 200, session.text
    thread_id = session.json()["thread_id"]

    skill = workspace / ".threads" / thread_id / "skills" / "demo" / "SKILL.md"
    body = {"path": str(skill), "text": SKILL_BODY, "thread_id": thread_id, "create": True}
    saved = integration_client.put("/api/files/content" + auth, json=body)
    assert saved.status_code == 200, saved.text
    receipt = saved.json()
    assert receipt["saved"] is True
    # The unrecorded line matches the issue's exact wording (no scope shown).
    assert receipt["guidance_history"].startswith("Saved and committed")
    assert "Reason: unrecorded" in receipt["guidance_history"]

    assert skill.read_text(encoding="utf-8") == SKILL_BODY
    relative = f".threads/{thread_id}/skills/demo/SKILL.md"
    sha = _git(["log", "-1", "--format=%H", "--", relative], cwd=workspace).strip()
    assert "Reason: unrecorded\nScope: skill, agent scope" in _body(workspace, sha)
    # The editor save is authored by the signed-in user, not the thread.
    people = _git(["log", "-1", "--format=%ae|%cn <%ce>", sha], cwd=workspace).strip()
    assert people.endswith("@users.corsia.local|CORSIA <corsia@localhost>"), people
    assert _git(["show", "--name-only", "--format=", sha], cwd=workspace).split() == [relative]


@pytest.mark.integration
def test_a_rejected_editor_save_makes_no_repository_or_commit(integration_client, integration_workspace):
    """Every editor rejection comes before Git: no commit, no sweep of other dirty guidance."""
    from auth_fixtures import OWNER_TOKEN

    workspace = integration_workspace  # shared by the session's integration tests
    if not (workspace / ".git").exists():
        _git(["init", "--quiet"], cwd=workspace)
    _git([*_IDENTITY, "commit", "--allow-empty", "-qm", "before"], cwd=workspace)
    head = _git(["rev-parse", "HEAD"], cwd=workspace)
    auth = "?" + "token=" + OWNER_TOKEN
    for name, text in (("shell", "outside\n"), ("demo", SKILL_BODY)):
        _skill(workspace, name).parent.mkdir(parents=True)
        _skill(workspace, name).write_text(text)
    for path, text, status in ((_skill(workspace), SKILL_BODY, 409), (_skill(workspace, "new"), "no front matter\n", 422)):
        saved = integration_client.put("/api/files/content" + auth, json={"path": str(path), "text": text, "create": True})
        assert saved.status_code == status, saved.text
    assert _git(["rev-parse", "HEAD"], cwd=workspace) == head

