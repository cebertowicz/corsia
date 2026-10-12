"""DCOL-FR5.9: only a document's owner, the user and the conductor edit it.

Every write path reaches the one checker, ``shared_principals.may_edit_document``:
a non-owner agent is refused with a message that names the owner and the suggest
tool; the owner, the owner's temporary subagent, the conductor and the user are
allowed; a conductor edit of a document it does not own tells the owner. The
per-scope adoption setting plays no part: the refusal holds without it.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from tests.test_shared_document_ownership import (
    _event_types,
    _shell_write,
    _write,
    ownership_scope as ownership_scope,  # noqa: F401 - the fixture
)

REFUSED = ("other", "plain")
ALLOWED = ("owner", "worker", "conductor")
SUGGEST_TOOL = "doc_comment_create"


def _shared_doc(scope, name="owned.md"):
    created = _write(scope, name, "alpha beta gamma\n")
    return scope["category"] / name, created["artifact"]["id"]


def _foreign_dir_doc(scope, name="handoff.md"):
    """A non-shared document in ``other``'s own directory that ``owner`` owns."""
    from registry import service

    path = scope["root"] / ".threads" / scope["other"] / "working" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("alpha beta gamma\n", encoding="utf-8")
    artifact = service.register_write_strict(scope["owner"], str(path), db=scope["db"])
    return path, artifact["id"]


def _scope_root_doc(scope, name="brief.md"):
    """A non-shared document in the project root, which only the conductor and the user may write."""
    from registry import service

    path = scope["root"] / name
    path.write_text("alpha beta gamma\n", encoding="utf-8")
    artifact = service.register_write_strict(scope["owner"], str(path), db=scope["db"])
    return path, artifact["id"]


def _own_dir_doc(scope, name="notes.md"):
    """A non-shared document in ``owner``'s own directory that ``owner`` owns."""
    from registry import service

    path = scope["root"] / ".threads" / scope["owner"] / "working" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("alpha beta gamma\n", encoding="utf-8")
    artifact = service.register_write_strict(scope["owner"], str(path), db=scope["db"])
    return path, artifact["id"]


def _write_text(scope, path, actor, *, direct_user=False, content="changed\n"):
    from registry.durable_writes import write_text

    return write_text(
        path, content, thread_id=scope[actor], project_id="project-alpha",
        workspace=scope["workspace"], direct_user=direct_user,
    )


def _managed(scope, path, actor, content="changed\n", *, reviewed=False):
    """The file tools' one write: a plain write for an ungoverned path, else through the registry."""
    from tools.file_write_completion import managed_write

    return managed_write(
        path, content, {"_thread_id": scope[actor], "_thread_project_id": "project-alpha"},
        reviewed=reviewed, producer="write_file", success="ok",
    )


def _project_of(scope, actor):
    """The project the actor thread belongs to; a workspace-level thread has none."""
    return scope["db"].execute("SELECT project_id FROM threads WHERE id=?", (scope[actor],)).fetchone()[0]


def _assert_refusal(message: str, *, owner_title: str = "Documents") -> None:
    assert f"owned by {owner_title}" in message
    assert f"`{SUGGEST_TOOL}`" in message


# --- durable write (the core of every native file tool) -------------------------------

@pytest.mark.parametrize("actor", REFUSED)
def test_non_owner_is_refused_on_a_shared_document_without_the_adoption_setting(
    ownership_scope, actor,
):
    from registry.shared_write_errors import DurableWritePolicyError

    scope = ownership_scope
    path, artifact_id = _shared_doc(scope)
    with pytest.raises(DurableWritePolicyError) as caught:
        _write_text(scope, path, actor)
    _assert_refusal(str(caught.value))
    assert path.read_text(encoding="utf-8") == "alpha beta gamma\n"
    assert "content_edited_by_non_owner" not in _event_types(scope["db"], artifact_id)


@pytest.mark.parametrize("actor", ALLOWED)
def test_owner_subagent_and_conductor_edit_a_shared_document(ownership_scope, actor):
    scope = ownership_scope
    path, _ = _shared_doc(scope)
    receipt = _write_text(scope, path, actor)
    assert receipt["artifact"]["parent_thread_id"] == scope["owner"]
    assert "ownership_warning" not in receipt
    assert path.read_text(encoding="utf-8") == "changed\n"


def test_user_edits_a_shared_document(ownership_scope):
    scope = ownership_scope
    path, _ = _shared_doc(scope)
    receipt = _write_text(scope, path, "plain", direct_user=True)
    assert receipt["artifact"]["parent_thread_id"] == scope["owner"]
    assert path.read_text(encoding="utf-8") == "changed\n"


def test_conductors_subagent_counts_as_the_conductor(ownership_scope):
    from tests.registry_helpers import make_session
    from tests.test_shared_document_ownership import _enforce

    scope = ownership_scope
    path, artifact_id = _shared_doc(scope)
    _enforce(scope)  # adoption on, as in production
    helper = make_session(scope["db"], project_id="project-alpha", title="Conductor helper", prefix="chelper")
    scope["db"].execute("UPDATE threads SET spawned_by=? WHERE id=?", (scope["conductor"], helper))
    scope["db"].commit()
    scope["helper"] = helper
    _write_text(scope, path, "helper")
    assert "content_edited_by_non_owner" in _event_types(scope["db"], artifact_id)


def test_non_owner_is_refused_on_a_non_shared_owned_document(ownership_scope):
    from registry.shared_write_errors import DurableWritePolicyError

    scope = ownership_scope
    # ``other`` may write in its own directory at the structure level; only ownership refuses it.
    path, artifact_id = _foreign_dir_doc(scope)
    with pytest.raises(DurableWritePolicyError) as caught:
        _managed(scope, path, "other")
    _assert_refusal(str(caught.value))
    assert path.read_text(encoding="utf-8") == "alpha beta gamma\n"
    assert "content_edited_by_non_owner" not in _event_types(scope["db"], artifact_id)


def test_owner_edits_its_own_non_shared_document(ownership_scope):
    scope = ownership_scope
    own, _ = _own_dir_doc(scope)
    _managed(scope, own, "owner")
    assert own.read_text(encoding="utf-8") == "changed\n"


def test_conductor_edits_a_non_shared_owned_document_and_tells_the_owner(ownership_scope):
    scope = ownership_scope
    path, artifact_id = _scope_root_doc(scope)
    _managed(scope, path, "conductor", "by conductor\n")
    assert path.read_text(encoding="utf-8") == "by conductor\n"
    assert "content_edited_by_non_owner" in _event_types(scope["db"], artifact_id)


def test_user_edits_a_non_shared_owned_document(ownership_scope):
    scope = ownership_scope
    path, _ = _scope_root_doc(scope)
    # The user writes through the editor save, which is direct_user; the caller
    # writes, and the owner hears of it like any edit that is not their own.
    decision = _write_text(scope, path, "plain", direct_user=True)
    assert decision.allowed and decision.basis == "user" and decision.reaches_owner


def test_unowned_unregistered_file_stays_writable_until_assignment(ownership_scope):
    scope = ownership_scope
    path = scope["root"] / ".threads" / scope["other"] / "working" / "scratch.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("old\n", encoding="utf-8")
    assert _write_text(scope, path, "other") is None
    assert path.read_text(encoding="utf-8") == "old\n"


# --- native file tools -----------------------------------------------------------------

@pytest.fixture
def authorized_tools(monkeypatch):
    from tools import filesystem_edit, filesystem_rw
    from tools.filesystem_gate import AuthorizedPath

    async def authorize(*, path, **_kwargs):
        return AuthorizedPath(Path(path).resolve(strict=False), reviewed=False)

    monkeypatch.setattr(filesystem_rw, "_resolve_path_for_action", authorize)
    monkeypatch.setattr(filesystem_edit, "_resolve_path_for_action", authorize)


async def _tool_call(scope, kind, path, actor):
    from tools.filesystem_edit import EditFileTool, MultiEditTool
    from tools.filesystem_rw import WriteFileTool

    context = {"_thread_id": scope[actor], "_thread_project_id": _project_of(scope, actor)}
    workspace = scope["workspace"]
    if kind == "write_file":
        return await WriteFileTool(workspace=workspace).execute(path=str(path), content="ALPHA beta gamma\n", **context)
    if kind == "edit_file":
        return await EditFileTool(workspace=workspace).execute(
            path=str(path), old_string="beta", new_string="BETA", **context)
    return await MultiEditTool(workspace=workspace).execute(
        path=str(path), edits=[{"old_string": "alpha", "new_string": "ALPHA"}], **context)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["write_file", "edit_file", "multi_edit"])
@pytest.mark.parametrize("where", ["shared", "non_shared"])
async def test_native_tools_refuse_a_non_owner(ownership_scope, authorized_tools, kind, where):
    scope = ownership_scope
    path, _ = _shared_doc(scope) if where == "shared" else _foreign_dir_doc(scope)
    result = await _tool_call(scope, kind, path, "other")
    assert result.startswith("Error:"), result
    _assert_refusal(result)
    assert path.read_text(encoding="utf-8") == "alpha beta gamma\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["write_file", "edit_file", "multi_edit"])
@pytest.mark.parametrize("actor", ALLOWED)
async def test_native_tools_allow_owner_subagent_and_conductor(ownership_scope, authorized_tools, kind, actor):
    scope = ownership_scope
    path, _ = _shared_doc(scope)
    receipt = json.loads(await _tool_call(scope, kind, path, actor))
    assert receipt["registry_managed"] is True
    assert "ownership_warning" not in receipt
    assert path.read_text(encoding="utf-8") != "alpha beta gamma\n"


@pytest.mark.parametrize("where", ["shared", "non_shared"])
def test_reviewed_host_write_by_a_non_owner_is_refused(ownership_scope, where):
    """A reviewed host permit never turns an ownership refusal into a write."""
    from registry.shared_write_errors import DurableWritePolicyError

    scope = ownership_scope
    path, artifact_id = _shared_doc(scope) if where == "shared" else _foreign_dir_doc(scope)
    with pytest.raises(DurableWritePolicyError) as caught:
        _managed(scope, path, "other", "reviewed edit\n", reviewed=True)
    _assert_refusal(str(caught.value))
    assert path.read_text(encoding="utf-8") == "alpha beta gamma\n"
    assert not {"ownership_bypass_write", "content_edited_by_non_owner"} & set(_event_types(scope["db"], artifact_id))


def test_reviewed_host_write_by_the_conductor_lands_and_tells_the_owner(ownership_scope):
    scope = ownership_scope
    path, artifact_id = _foreign_dir_doc(scope)
    result = _managed(scope, path, "conductor", "reviewed edit\n", reviewed=True)
    assert path.read_text(encoding="utf-8") == "reviewed edit\n"
    assert SUGGEST_TOOL not in result
    assert _event_types(scope["db"], artifact_id).count("content_edited_by_non_owner") == 1


@pytest.mark.asyncio
async def test_a_host_lease_never_sends_an_ownership_refusal_to_the_reviewer(ownership_scope, monkeypatch):
    from types import SimpleNamespace

    from tools import host_access_gate as gate

    scope = ownership_scope
    path, _ = _shared_doc(scope)
    reviewed: list[dict] = []

    async def active(_thread_id):
        return {"id": "lease", "revision": 1, "application_hash": "a" * 64, "state": "active"}

    async def review(lease_id, **kwargs):
        reviewed.append(kwargs)
        raise AssertionError("an ownership refusal reached the host reviewer")

    monkeypatch.setattr(gate, "is_path_allowed", lambda *_a, **_k: True)
    monkeypatch.setattr(gate, "check_repo_write_path", lambda _path: None)
    monkeypatch.setattr(gate, "_resolve_active_lease", active)
    monkeypatch.setattr(gate, "_review_host_action", review)
    tool = SimpleNamespace(_workspace=scope["workspace"], _allowed_dir=None, working_dir=None, default_timeout=120)
    with pytest.raises(gate.HostAccessBlocked) as caught:
        await gate.authorize_host_action(
            thread_id=scope["other"], project_id="project-alpha", tool=tool,
            tool_name="edit_file", params={"path": str(path), "old_string": "beta", "new_string": "BETA"},
        )
    _assert_refusal(str(caught.value))
    assert reviewed == []


# --- live editor / CRDT lane ------------------------------------------------------------

async def _lane(scope, tmp_path, path, actor, *, direct_user=False):
    from doceditor.live_document import LiveDocument
    from doceditor.live_lane import DocumentLane
    from doceditor.live_publication_record import WriteActor
    from doceditor.live_runtime import LiveDocuments
    from doceditor.live_shared_policy import SharedLivePolicy
    from doceditor.document_save import write_atomic_plain

    live = await LiveDocument.open(path, tmp_path / "store.sqlite")
    db_path = Path(scope["db"].execute("PRAGMA database_list").fetchone()[2])
    policy = SharedLivePolicy(live, owner=LiveDocuments(tmp_path, workspace=scope["workspace"], db_path=db_path))

    async def valid(text, actor=None):
        pass

    async def writer(text, prior_hash):
        await asyncio.to_thread(write_atomic_plain, path, text, prior_hash)

    lane = DocumentLane(live, validate_actor=valid, write=writer, policy=policy.decide)
    return live, lane, WriteActor(scope[actor], _project_of(scope, actor), direct_user=direct_user)


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["shared", "non_shared"])
async def test_live_lane_refuses_a_non_owner_before_anything_is_published(ownership_scope, tmp_path, where):
    from doceditor.live_lane import Operation
    from registry.shared_write_errors import DurableWritePolicyError

    scope = ownership_scope
    path, _ = _shared_doc(scope) if where == "shared" else _foreign_dir_doc(scope)
    live, lane, actor = await _lane(scope, tmp_path, path, "other")
    with pytest.raises(DurableWritePolicyError) as caught:
        result = await lane.submit(Operation("replace", content="alpha BETA gamma\n", actor=actor))
        result.require_landed()
    _assert_refusal(str(caught.value))
    assert path.read_text(encoding="utf-8") == "alpha beta gamma\n"
    assert live.text == "alpha beta gamma\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("who", ["owner", "worker", "conductor", "user"])
async def test_live_lane_allows_owner_subagent_conductor_and_user(ownership_scope, tmp_path, who):
    from doceditor.live_lane import Operation

    scope = ownership_scope
    path, _ = _shared_doc(scope)
    actor_key = "plain" if who == "user" else who
    live, lane, actor = await _lane(scope, tmp_path, path, actor_key, direct_user=who == "user")
    result = await lane.submit(Operation("replace", content="alpha BETA gamma\n", actor=actor))
    result.require_landed()
    assert path.read_text(encoding="utf-8") == "alpha BETA gamma\n"


# --- doceditor plain-save fallback ------------------------------------------------------

def _save(scope, path, actor, *, direct_user=False):
    from doceditor.document_save import DocumentSave, _write_document

    body = DocumentSave(str(path), "saved text\n", project_id="project-alpha", thread_id=scope[actor])
    return _write_document(body, path, direct_user=direct_user)


@pytest.mark.parametrize("where", ["shared", "non_shared"])
def test_plain_save_refuses_a_non_owner_agent(ownership_scope, where):
    from doceditor.document_save import DocumentSaveError

    scope = ownership_scope
    path, _ = _shared_doc(scope) if where == "shared" else _foreign_dir_doc(scope)
    with pytest.raises(DocumentSaveError) as caught:
        _save(scope, path, "other")
    assert caught.value.kind == "durable_write_policy"
    _assert_refusal(caught.value.message)
    assert path.read_text(encoding="utf-8") == "alpha beta gamma\n"


def test_plain_save_allows_owner_conductor_and_user(ownership_scope):
    scope = ownership_scope
    own, _ = _own_dir_doc(scope)
    _save(scope, own, "owner")
    assert own.read_text(encoding="utf-8") == "saved text\n"
    root_doc, artifact_id = _scope_root_doc(scope)
    _save(scope, root_doc, "conductor")
    assert root_doc.read_text(encoding="utf-8") == "saved text\n"
    assert "content_edited_by_non_owner" in _event_types(scope["db"], artifact_id)
    _save(scope, root_doc, "plain", direct_user=True)


# --- suggestion accept -------------------------------------------------------------------

async def _suggestion(scope, path, author="other"):
    from unittest import mock

    from tools.documents import DocCommentCreateTool

    # No live-document owner here: the create runs straight on the database, whatever an earlier test left running.
    with mock.patch("doceditor.live_runtime.get_documents", return_value=None):
        result = await DocCommentCreateTool().execute(
            doc_path=str(path), quote="beta", replacement="BETA", _thread_id=scope[author],
        )
    return json.loads(result)["comment"]["id"]


async def _accept(scope, comment_id, actor, *, writes):
    from doceditor.comment_actions import decide_suggestion

    async def write(path, text, project_id, reason, expected_sha256):
        writes.append(text)

    user_id = scope["db"].execute("SELECT user_id FROM threads WHERE id=?", (scope["owner"],)).fetchone()[0]
    return await decide_suggestion(
        comment_id, "accept", user_id=user_id, decided_by="decider",
        actor_thread_id=None if actor == "user" else scope[actor], write=write,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["other", "plain"])
async def test_accept_refuses_a_non_owner_agent_and_names_owner_and_suggest_tool(ownership_scope, actor):
    from doceditor.comment_actions import CommentActionError

    scope = ownership_scope
    path, _ = _shared_doc(scope)
    comment_id = await _suggestion(scope, path)
    writes: list[str] = []
    with pytest.raises(CommentActionError) as caught:
        await _accept(scope, comment_id, actor, writes=writes)
    assert caught.value.kind == "not_owner"
    assert "Documents" in caught.value.message
    assert SUGGEST_TOOL in caught.value.message
    assert writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["owner", "worker", "conductor", "user"])
async def test_accept_allows_owner_subagent_conductor_and_user(ownership_scope, actor):
    scope = ownership_scope
    path, _ = _shared_doc(scope)
    comment_id = await _suggestion(scope, path)
    writes: list[str] = []
    row = await _accept(scope, comment_id, actor, writes=writes)
    assert row["decision"] == "accepted"
    assert writes == ["alpha BETA gamma\n"]


@pytest.mark.asyncio
async def test_accept_uses_the_registration_owner_the_write_gate_uses(ownership_scope, monkeypatch):
    """The artifact projection's parent does not decide who may accept; the registration owner does."""
    from doceditor import comment_actions
    from doceditor.comment_actions import CommentActionError

    scope = ownership_scope
    path, _ = _shared_doc(scope)
    comment_id = await _suggestion(scope, path)
    real = comment_actions.registry_models.get_artifact

    def drifted(artifact_id, db=None):
        artifact = real(artifact_id, db=db)
        return {**artifact, "parent_thread_id": scope["other"]} if artifact else artifact

    monkeypatch.setattr(comment_actions.registry_models, "get_artifact", drifted)
    writes: list[str] = []
    with pytest.raises(CommentActionError):
        await _accept(scope, comment_id, "other", writes=writes)
    await _accept(scope, comment_id, "owner", writes=writes)
    assert writes == ["alpha BETA gamma\n"]


# --- conductor edits tell the owner ------------------------------------------------------

def _owner_events(db, artifact_id: str) -> list[tuple[str, dict]]:
    rows = db.execute(
        "SELECT event_type, metadata FROM artifact_registration_events WHERE artifact_id=? "
        "AND event_type IN ('content_edited_by_non_owner','ownership_bypass_write') ORDER BY created_at,id",
        (artifact_id,),
    ).fetchall()
    return [(str(row[0]), json.loads(row[1] or "{}")) for row in rows]


@pytest.mark.parametrize("actor", ["conductor", "conductor_helper"])
def test_conductor_shell_edit_is_reported_like_its_file_tool_edit(ownership_scope, actor):
    """The conductor may edit, so its shell edit is an allowed edit the owner hears of, not a bypass."""
    from tests.registry_helpers import make_session
    from tools.shared_write_detection import detect_shell_shared_writes

    scope = ownership_scope
    helper = make_session(scope["db"], project_id="project-alpha", title="Conductor helper", prefix="chelper")
    scope["db"].execute("UPDATE threads SET spawned_by=? WHERE id=?", (scope["conductor"], helper))
    scope["db"].commit()
    scope["conductor_helper"] = helper
    path, artifact_id = _shared_doc(scope, "conductor-shell.md")
    started = time.time_ns()
    _shell_write(path, "conductor shell edit\n", started)
    notices = detect_shell_shared_writes(
        scope[actor], "project-alpha", "printf x > shared/documents/conductor-shell.md",
        started, scope["root"],
    )
    assert not any(SUGGEST_TOOL in notice for notice in notices)
    events = _owner_events(scope["db"], artifact_id)
    assert [kind for kind, _ in events] == ["content_edited_by_non_owner"]
    assert events[0][1]["path"] == "shared/documents/conductor-shell.md"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["write_file", "edit_file", "multi_edit"])
async def test_conductor_file_tool_edit_of_a_non_shared_document_tells_the_owner(
    ownership_scope, authorized_tools, kind,
):
    """edit_file and multi_edit write through an open handle; that branch reports too."""
    scope = ownership_scope
    path, artifact_id = _scope_root_doc(scope)
    result = await _tool_call(scope, kind, path, "conductor")
    assert not result.startswith("Error:"), result
    assert SUGGEST_TOOL not in result
    events = _owner_events(scope["db"], artifact_id)
    assert [kind for kind, _ in events] == ["content_edited_by_non_owner"]
    assert events[0][1]["path"] == "brief.md"


def test_conductor_file_tool_edit_tells_the_owner(ownership_scope):
    scope = ownership_scope
    path, artifact_id = _shared_doc(scope, "conductor-edit.md")
    _write_text(scope, path, "conductor")
    assert "content_edited_by_non_owner" in _event_types(scope["db"], artifact_id)


# --- the adoption setting only governs claiming unowned legacy files -------------------------------

def test_unowned_file_is_not_claimed_by_an_edit_while_the_adoption_setting_is_off(ownership_scope):
    from registry import service

    scope = ownership_scope
    legacy = scope["category"] / "legacy.md"
    legacy.write_text("legacy\n", encoding="utf-8")
    _write_text(scope, legacy, "other")
    artifact = service.find_file_identity(str(legacy), db=scope["db"])
    owner = scope["db"].execute(
        "SELECT parent_thread_id FROM artifact_registrations "
        "WHERE artifact_id=? AND relationship_state LIKE 'owner-%'",
        (artifact["id"],),
    ).fetchone()["parent_thread_id"]
    assert owner == scope["other"]


# --- the conductor exception is the conductor of the document's own scope -----------------

@pytest.fixture
def scopes(ownership_scope):
    """The alpha scope plus a second project and a workspace conductor, each with its own conductor."""
    from infra.conductor import set_conductor
    from tests.registry_helpers import make_session

    scope = ownership_scope
    db = scope["db"]
    (scope["workspace"] / "projects" / "beta").mkdir(parents=True)
    db.execute(
        "INSERT INTO projects (id,name,path,status,created_at,updated_at) VALUES (?,?,?,?,?,?)",
        ("project-beta", "Beta", "workspace://workspace-stable-id/projects/beta", "active", "t", "t"),
    )
    beta_conductor = make_session(db, project_id="project-beta", title="Beta Conductor", prefix="betacond")
    workspace_conductor = make_session(db, project_id=None, title="Workspace Conductor", prefix="wsconductor")
    conductor_worker = make_session(db, project_id="project-alpha", title="Conductor Worker", prefix="cworker")
    db.execute("UPDATE threads SET agent_enabled=1 WHERE id IN (?,?)", (beta_conductor, workspace_conductor))
    db.execute("UPDATE threads SET spawned_by=? WHERE id=?", (scope["conductor"], conductor_worker))
    db.execute("UPDATE projects SET conductor_thread_id=? WHERE id='project-beta'", (beta_conductor,))
    db.commit()
    assert set_conductor("workspace", workspace_conductor, db=db)
    scope.update(beta_conductor=beta_conductor, workspace_conductor=workspace_conductor,
                 conductor_worker=conductor_worker)
    return scope


OTHER_SCOPE_CONDUCTORS = ("beta_conductor", "workspace_conductor")


def test_document_scope_is_the_scope_holding_the_path(scopes):
    from registry.shared_principals import document_scope

    scope = scopes
    doc, _ = _shared_doc(scope)
    workspace = scope["workspace"].resolve()

    def resolve(path):
        return document_scope(path, workspace=scope["workspace"], db=scope["db"])

    assert resolve(doc) == (workspace / "projects" / "alpha", scope["conductor"])
    assert resolve(workspace / "projects" / "beta" / "shared" / "x.md") == (
        workspace / "projects" / "beta", scope["beta_conductor"],
    )
    assert resolve(workspace / "loose.md") == (workspace, scope["workspace_conductor"])
    # A folder no project is registered at belongs to the workspace scope.
    assert resolve(workspace / "projects" / "gamma" / "x.md") == (workspace, scope["workspace_conductor"])


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", OTHER_SCOPE_CONDUCTORS)
@pytest.mark.parametrize("where", ["shared", "non_shared"])
@pytest.mark.parametrize("kind", ["write_file", "edit_file"])
async def test_another_scopes_conductor_is_refused_by_the_file_tools(scopes, authorized_tools, actor, where, kind):
    scope = scopes
    path, artifact_id = _shared_doc(scope) if where == "shared" else _foreign_dir_doc(scope)
    result = await _tool_call(scope, kind, path, actor)
    assert result.startswith("Error:"), result
    if actor == "workspace_conductor":
        _assert_refusal(result)
    assert path.read_text(encoding="utf-8") == "alpha beta gamma\n"
    assert "content_edited_by_non_owner" not in _event_types(scope["db"], artifact_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", OTHER_SCOPE_CONDUCTORS)
async def test_another_scopes_conductor_is_refused_by_the_live_lane(scopes, tmp_path, actor):
    from doceditor.live_lane import Operation
    from registry.shared_write_errors import DurableWritePolicyError

    scope = scopes
    path, _ = _shared_doc(scope)
    live, lane, write_actor = await _lane(scope, tmp_path, path, actor)
    with pytest.raises(DurableWritePolicyError):
        result = await lane.submit(Operation("replace", content="alpha BETA gamma\n", actor=write_actor))
        result.require_landed()
    assert path.read_text(encoding="utf-8") == "alpha beta gamma\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", OTHER_SCOPE_CONDUCTORS)
async def test_another_scopes_conductor_cannot_accept_a_suggestion(scopes, actor):
    from doceditor.comment_actions import CommentActionError

    scope = scopes
    path, _ = _shared_doc(scope)
    comment_id = await _suggestion(scope, path)
    writes: list[str] = []
    with pytest.raises(CommentActionError) as caught:
        await _accept(scope, comment_id, actor, writes=writes)
    assert caught.value.kind == "not_owner"
    assert SUGGEST_TOOL in caught.value.message
    assert writes == []


def _save_as(scope, path, actor):
    from doceditor.document_save import DocumentSave, _write_document

    body = DocumentSave(str(path), "saved text\n", project_id=_project_of(scope, actor), thread_id=scope[actor])
    return _write_document(body, path, direct_user=False)


@pytest.mark.parametrize("actor", OTHER_SCOPE_CONDUCTORS)
def test_another_scopes_conductor_is_refused_by_the_plain_save(scopes, actor):
    from doceditor.document_save import DocumentSaveError

    scope = scopes
    path, artifact_id = _foreign_dir_doc(scope)
    with pytest.raises(DocumentSaveError):
        _save_as(scope, path, actor)
    assert path.read_text(encoding="utf-8") == "alpha beta gamma\n"
    assert "content_edited_by_non_owner" not in _event_types(scope["db"], artifact_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["write_file", "edit_file", "multi_edit"])
async def test_the_scope_conductors_subagent_edits_with_the_file_tools(scopes, authorized_tools, kind):
    scope = scopes
    path, artifact_id = _shared_doc(scope)
    result = await _tool_call(scope, kind, path, "conductor_worker")
    assert not result.startswith("Error:"), result
    assert path.read_text(encoding="utf-8") != "alpha beta gamma\n"
    assert "content_edited_by_non_owner" in _event_types(scope["db"], artifact_id)


@pytest.mark.asyncio
async def test_the_scope_conductors_subagent_edits_through_the_live_lane(scopes, tmp_path):
    from doceditor.live_lane import Operation

    scope = scopes
    path, _ = _shared_doc(scope)
    live, lane, actor = await _lane(scope, tmp_path, path, "conductor_worker")
    result = await lane.submit(Operation("replace", content="alpha BETA gamma\n", actor=actor))
    result.require_landed()
    assert path.read_text(encoding="utf-8") == "alpha BETA gamma\n"


@pytest.mark.asyncio
async def test_the_scope_conductors_subagent_can_accept_a_suggestion(scopes):
    scope = scopes
    path, _ = _shared_doc(scope)
    comment_id = await _suggestion(scope, path)
    writes: list[str] = []
    row = await _accept(scope, comment_id, "conductor_worker", writes=writes)
    assert row["decision"] == "accepted"
    assert writes == ["alpha BETA gamma\n"]


# --- tenant access stays on the artifact's owning thread ----------------------------------

@pytest.mark.asyncio
async def test_a_user_owned_registration_does_not_open_another_users_artifact(ownership_scope, monkeypatch):
    """Tenant access reads the artifact's owning thread; a `ui` registration owner never widens it."""
    from doceditor import comment_actions
    from doceditor.comment_actions import CommentActionError
    from tests.registry_helpers import make_session

    scope = ownership_scope
    path, artifact_id = _shared_doc(scope)
    comment_id = await _suggestion(scope, path)
    foreign = make_session(scope["db"], project_id="project-alpha", user_id="leksi", prefix="leksi")
    scope["db"].execute(
        "UPDATE artifact_registrations SET parent_thread_id='ui' "
        "WHERE artifact_id=? AND relationship_state LIKE 'owner-%'",
        (artifact_id,),
    )
    scope["db"].commit()
    real = comment_actions.registry_models.get_artifact

    def foreign_owned(artifact_id, db=None):
        artifact = real(artifact_id, db=db)
        return {**artifact, "parent_thread_id": foreign} if artifact else artifact

    monkeypatch.setattr(comment_actions.registry_models, "get_artifact", foreign_owned)
    writes: list[str] = []
    with pytest.raises(CommentActionError) as caught:
        await _accept(scope, comment_id, "user", writes=writes)
    assert caught.value.kind == "not_found"
    assert writes == []


# --- journaled live publications from before this change ----------------------------------

def test_a_journaled_record_with_the_retired_warning_restores_without_it(ownership_scope):
    from dataclasses import fields

    from doceditor.live_operation import WriteResult
    from doceditor.live_publication_record import Publication

    scope = ownership_scope
    path, _ = _shared_doc(scope)
    legacy = {
        "path": str(path), "category_root": str(path.parent), "index_path": str(path.parent / "INDEX.md"),
        "create_parent": False, "scope_id": "project-alpha", "actor_principal": scope["other"],
        "owner_thread_id": scope["owner"], "warning": "After ownership enforcement this write will be refused.",
        "update_trigger": None, "outside_change": False, "classification": "content",
    }
    restored = Publication.decode(Publication(scope["other"], None, None, b"", legacy).encode())
    assert "warning" not in restored.shared_target().to_record()
    assert "warning" not in {field.name for field in fields(WriteResult)}
