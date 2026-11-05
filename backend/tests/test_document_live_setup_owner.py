"""Open and write setup share one process fact, without durable setup budgets."""
from pathlib import Path

import pytest

from api.files_load import _live_open_setup
from doceditor.document_save import DocumentSaveError
from doceditor.live_admission import OpenRefused
from doceditor.live_lane import Operation
from doceditor.live_runtime import LiveDocuments
from doceditor import live_shared_policy
from registry import shared_documents
from registry.shared_write_errors import SharedIdentitySetupRequired
from tests.test_document_live_file_tools import opened
from tests.test_shared_document_ownership import ownership_scope as ownership_scope


def test_live_open_skips_setup_only_for_shared_context_it_cannot_save(ownership_scope, monkeypatch):
    from api.files_load import read_document
    from infra import config
    from unittest.mock import Mock
    from tests.test_system_projection_document_access import project_system_file

    scope = ownership_scope
    projection = project_system_file(scope['workspace'])
    skill = config.WORKSPACE / 'skills' / 'alpha' / 'SKILL.md'
    skill.parent.mkdir(parents=True)
    skill.write_text('skill content', encoding='utf-8')
    role_skill = config.WORKSPACE / 'context' / 'roles' / 'agent' / 'skills' / 'beta' / 'SKILL.md'
    role_skill.parent.mkdir(parents=True)
    role_skill.write_text('role skill content', encoding='utf-8')
    normal = scope['root'] / 'note.md'
    normal.write_text('ordinary content', encoding='utf-8')
    manager = Mock()
    manager.setup_shared.side_effect = AssertionError('a read-only open must not prepare writes')
    monkeypatch.setattr('doceditor.live_runtime.get_documents', lambda: manager)
    monkeypatch.setattr('infra.guidance_targets.guidance_target', lambda *_: None)
    user_id = scope['db'].execute('SELECT user_id FROM threads WHERE id=?', (scope['owner'],)).fetchone()[0]
    for read_only in (projection, skill, role_skill):
        result = read_document(str(read_only), user_id, 'project-alpha', scope['owner'], live_open=True)
        assert result['text'] == read_only.read_text(encoding='utf-8')
        assert result['writable'] is False
    manager.setup_shared.assert_not_called()

    manager.setup_shared.side_effect = None
    result = read_document(str(normal), user_id, 'project-alpha', scope['owner'], live_open=True)
    assert result['text'] == 'ordinary content'
    assert result['writable'] is True
    manager.setup_shared.assert_called_once()

    # Not writable from here but not a shared context file: setup still runs before register-on-read.
    manager.setup_shared.reset_mock()
    monkeypatch.setattr('api.files_load._writable', lambda *_args, **_kwargs: False)
    result = read_document(str(normal), user_id, 'project-alpha', scope['owner'], live_open=True)
    assert result['writable'] is False
    manager.setup_shared.assert_called_once()


@pytest.mark.asyncio
async def test_open_setup_failure_blocks_live_write_until_success(ownership_scope, tmp_path, monkeypatch):
    scope = ownership_scope
    manager, entry, context = await opened(scope, tmp_path, monkeypatch)
    from doceditor.live_publication_record import WriteActor
    actor = WriteActor(scope["owner"], "project-alpha")
    original = shared_documents.resolve_shared_target
    def unavailable(*args, **kwargs):
        raise OSError("shared setup unavailable")
    monkeypatch.setattr(shared_documents, "resolve_shared_target", unavailable)
    try:
        with pytest.raises(OpenRefused, match="shared setup unavailable"):
            _live_open_setup(entry.live.path, "project-alpha", scope["owner"], scope["db"])
        assert manager.setup_state(entry.live.path)[0].reason == "shared setup unavailable"
        result = await entry.lane.submit(Operation("replace", content="refused", actor=actor))
        assert result.outcome == "refused"
        with pytest.raises(DocumentSaveError, match="shared setup unavailable") as error:
            result.require_landed()
        assert error.value.kind == "setup_failed"
        assert entry.live.store.failure("setup_failed") is None
        monkeypatch.setattr(shared_documents, "resolve_shared_target", original)
        monkeypatch.setattr(live_shared_policy, "resolve_shared_target", original)
        _live_open_setup(entry.live.path, "project-alpha", scope["owner"], scope["db"])
        assert entry.live.path not in manager.setup_failures
        result = await entry.lane.submit(Operation("replace", content="saved", actor=actor))
        assert result.server_saved and entry.live.path.read_text() == "saved"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_write_setup_failure_is_cleared_by_open_success(ownership_scope, tmp_path, monkeypatch):
    scope = ownership_scope
    manager, entry, context = await opened(scope, tmp_path, monkeypatch)
    from doceditor.live_publication_record import WriteActor
    actor = WriteActor(scope["owner"], "project-alpha")
    original = shared_documents.resolve_shared_target
    def denied(*args, **kwargs):
        raise PermissionError("identity registration denied")
    monkeypatch.setattr(shared_documents, "resolve_shared_target", denied)
    monkeypatch.setattr(live_shared_policy, "resolve_shared_target", denied)
    try:
        with pytest.raises(DocumentSaveError, match="identity registration denied"):
            await entry.policy.setup(actor)
        assert manager.setup_state(entry.live.path)[0].reason == "identity registration denied"
        assert entry.live.store.failure("setup_failed") is None
        monkeypatch.setattr(shared_documents, "resolve_shared_target", original)
        monkeypatch.setattr(live_shared_policy, "resolve_shared_target", original)
        _live_open_setup(entry.live.path, "project-alpha", scope["owner"], scope["db"])
        assert entry.live.path not in manager.setup_failures
        assert not hasattr(entry.policy, "budget")
        assert not hasattr(entry.policy, "retry_setup")
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_setup_failure_is_forgotten_on_restart_and_next_write_tries_once(ownership_scope, tmp_path, monkeypatch):
    scope = ownership_scope
    manager, entry, context = await opened(scope, tmp_path, monkeypatch)
    from doceditor.live_publication_record import WriteActor
    actor = WriteActor(scope["owner"], "project-alpha")
    original = shared_documents.resolve_shared_target
    def denied(*args, **kwargs):
        raise OSError("setup failed before restart")
    monkeypatch.setattr(shared_documents, "resolve_shared_target", denied)
    monkeypatch.setattr(live_shared_policy, "resolve_shared_target", denied)
    try:
        with pytest.raises(DocumentSaveError):
            await entry.policy.setup(actor)
        assert entry.live.store.failure("setup_failed") is None
    finally:
        await manager.close()
    monkeypatch.setattr(shared_documents, "resolve_shared_target", original)
    monkeypatch.setattr(live_shared_policy, "resolve_shared_target", original)
    restarted = LiveDocuments(manager.root, workspace=scope["workspace"],
                              db_path=Path(scope["db"].execute("PRAGMA database_list").fetchone()[2]))
    reopened = await restarted.open(entry.artifact_id, entry.live.path)
    calls = []
    def missing(*args):
        raise SharedIdentitySetupRequired()
    monkeypatch.setattr(reopened.policy, "decision", missing)
    def checked(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)
    monkeypatch.setattr(shared_documents, "resolve_shared_target", checked)
    try:
        assert restarted.setup_failures == {}
        result = await reopened.lane.submit(Operation("replace", content="waiting", actor=actor))
        with pytest.raises(SharedIdentitySetupRequired):
            result.require_landed()
        await reopened.lane._worker
        assert calls == [entry.live.path]
        assert reopened.live.store.failure("setup_failed") is None
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_open_write_and_ws_retry_call_the_same_setup_function(ownership_scope, tmp_path, monkeypatch):
    from doceditor.live_publication_record import WriteActor
    from tests.test_document_live_transport import Socket
    from ws import document_control
    scope = ownership_scope
    manager, entry, context = await opened(scope, tmp_path, monkeypatch)
    for registry in ("_subscriptions", "_tasks", "_openings", "_commits"):
        monkeypatch.setattr(document_control, registry, {})
    calls = []
    original = manager.setup_shared
    def checked(path, actor, **kwargs):
        calls.append((path, actor.thread_id))
        return original(path, actor, **kwargs)
    monkeypatch.setattr(manager, "setup_shared", checked)
    socket = Socket()
    user = {"id": scope["db"].execute("SELECT user_id FROM threads WHERE id=?", (scope["owner"],)).fetchone()[0]}
    try:
        await document_control.handle_frame(socket, user, {"type": "doc_open", "path": str(entry.live.path),
            "project_id": "project-alpha", "thread_id": scope["owner"]})
        opened_frame = await socket.next("doc_opened")
        calls.clear()
        await entry.policy.setup(WriteActor(scope["owner"], "project-alpha"))
        _live_open_setup(entry.live.path, "project-alpha", scope["owner"], scope["db"])
        await document_control.handle_frame(socket, user, {"type": "doc_flush", "retry": True,
            "artifact_id": entry.artifact_id, "store_id": opened_frame["store_id"]})
        assert (await socket.next("doc_saved"))["server_saved"]
        assert calls == [(entry.live.path, scope["owner"])] * 3
        assert entry.live.store.failure("setup_failed") is None
    finally:
        await document_control.disconnect(socket)
        await manager.close()


@pytest.mark.asyncio
async def test_setup_connection_failure_is_also_one_process_fact(ownership_scope, tmp_path, monkeypatch):
    from doceditor.live_publication_record import WriteActor
    scope = ownership_scope
    manager, entry, context = await opened(scope, tmp_path, monkeypatch)
    manager.db_path = tmp_path / "missing-parent" / "application.sqlite"
    try:
        with pytest.raises(DocumentSaveError, match="unable to open database file"):
            await entry.policy.setup(WriteActor(scope["owner"], "project-alpha"))
        assert manager.setup_state(entry.live.path)[0].reason == "unable to open database file"
        assert entry.live.store.failure("setup_failed") is None
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_open_from_another_thread_goes_live_for_a_document_in_an_agents_thread_directory(
        ownership_scope, tmp_path, monkeypatch):
    """The user viewing a document from a different thread still gets the live editor.

    A refused open leaves the document plain, so an agent's later accept is a disk
    write the browser reports as an outside change instead of landing live.
    """
    scope = ownership_scope
    manager, _entry, _context = await opened(scope, tmp_path, monkeypatch)
    path = scope["root"] / ".threads" / scope["owner"] / "working" / "note.md"
    path.parent.mkdir(parents=True)
    path.write_text("first paragraph\n", encoding="utf-8")
    try:
        _live_open_setup(path, "project-alpha", scope["other"], scope["db"])
        assert path not in manager.setup_failures
    finally:
        await manager.close()
