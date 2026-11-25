"""Priorities ownership at creation and in the one-time repair."""
from tests.test_shared_document_ownership import ownership_scope as ownership_scope


def test_priority_creation_and_other_context_owner(ownership_scope):
    from registry import service
    from registry.shared_principals import artifact_owner
    scope = ownership_scope
    context = scope['root'] / 'context'
    context.mkdir(exist_ok=True)
    for name, expected in [('PRIORITIES.md', 'ui'), ('OTHER.md', scope['conductor'])]:
        path = context / name
        path.write_text('content\n')
        artifact = service.register_write_strict(scope['conductor'], str(path), db=scope['db'])
        assert artifact_owner(artifact['id'], db=scope['db']) == expected


def test_observed_project_write_and_agent_edits(ownership_scope):
    import pytest
    from registry.observed_write import register_observed_write
    from registry.shared_principals import artifact_owner
    from registry.shared_write_errors import DurableWritePolicyError
    from tests.test_owner_only_edit import _managed

    scope = ownership_scope
    path = scope['root'] / 'context' / 'PRIORITIES.md'
    path.parent.mkdir(exist_ok=True)
    _managed(scope, path, 'conductor', 'content\n')
    artifact = register_observed_write(scope['conductor'], {'path': str(path)}, ('path', None), 'ok',
                                       name='write_file', is_cc_builtin=False, origin=None, db=scope['db'])
    assert artifact_owner(artifact['id'], db=scope['db']) == 'ui'
    with pytest.raises(DurableWritePolicyError, match='doc_comment_create'):
        _managed(scope, path, 'conductor', 'changed\n')
    with pytest.raises(DurableWritePolicyError, match='doc_comment_create'):
        _managed(scope, path, 'owner', 'changed\n')
    assert path.read_text() == 'content\n'


def test_ordinary_agent_is_sent_to_the_person_only_for_priorities(ownership_scope):
    import pytest
    from registry.shared_write_errors import DurableWritePolicyError
    from tests.test_owner_only_edit import _managed

    scope = ownership_scope
    context = scope['root'] / 'context'
    context.mkdir(exist_ok=True)
    agent_file = scope['root'] / '.threads' / scope['owner'] / 'context' / 'PRIORITIES.md'
    agent_file.parent.mkdir(parents=True, exist_ok=True)
    agent_file.write_text('content\n')
    with pytest.raises(DurableWritePolicyError, match='created only by the conductor'):
        _managed(scope, context / 'PRIORITIES.md', 'other', 'changed\n')
    with pytest.raises(DurableWritePolicyError) as refused:
        _managed(scope, agent_file, 'other', 'changed\n')
    assert 'doc_comment_create' in str(refused.value)
    assert 'Only the person edits it directly.' in str(refused.value)
    assert 'conductor thread' not in str(refused.value)
    with pytest.raises(DurableWritePolicyError, match='conductor thread') as refused:
        _managed(scope, context / 'OTHER.md', 'other', 'changed\n')
    assert 'doc_comment_create' not in str(refused.value)
    with pytest.raises(DurableWritePolicyError, match="not inside another thread's directory"):
        _managed(scope, agent_file.parent / 'NOTES.md', 'other', 'changed\n')
    assert agent_file.read_text() == 'content\n'
    assert not (context / 'PRIORITIES.md').exists()


def test_startup_completes_when_the_priorities_repair_fails(ownership_scope, monkeypatch):
    from app_startup_init import _hand_priorities_to_person
    from infra.settings import get_setting
    from registry import priorities_ownership

    def broken(workspace, *, db):
        from infra.settings import set_setting
        set_setting('priorities_person_ownership_v1', {'done': True}, db=db)
        raise RuntimeError('boom')

    logged = []

    class Logger:
        def exception(self, message, *args):
            logged.append(message)

    monkeypatch.setattr(priorities_ownership, 'migrate_priorities_ownership', broken)
    _hand_priorities_to_person(ownership_scope['workspace'], Logger())
    assert logged == ['priorities ownership repair failed; it runs again at the next start']
    assert not get_setting('priorities_person_ownership_v1', db=ownership_scope['db'])


def test_agent_cannot_create_its_own_priorities(ownership_scope):
    import pytest
    from registry.shared_write_errors import DurableWritePolicyError
    from tests.test_owner_only_edit import _managed

    scope = ownership_scope
    path = scope['root'] / '.threads' / scope['owner'] / 'context' / 'PRIORITIES.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    with pytest.raises(DurableWritePolicyError, match='created only by the conductor'):
        _managed(scope, path, 'owner', 'made up\n')
    assert not path.exists()
    _managed(scope, path.parent / 'NOTES.md', 'owner', 'fine\n')


def test_thread_priority_refuses_agent_and_conductor(ownership_scope):
    import pytest
    from registry import service
    from registry.shared_write_errors import DurableWritePolicyError
    from tests.test_owner_only_edit import _managed

    scope = ownership_scope
    path = scope['root'] / '.threads' / scope['owner'] / 'context' / 'PRIORITIES.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('content\n')
    service.register_write_strict(scope['owner'], str(path), db=scope['db'])
    for actor in ('owner', 'conductor'):
        with pytest.raises(DurableWritePolicyError, match='doc_comment_create'):
            _managed(scope, path, actor, 'changed\n')
    assert path.read_text() == 'content\n'


def test_startup_repair_registers_and_rehomes_once(ownership_scope):
    from registry import service
    from registry.ownership import rehome_artifact
    from registry.priorities_ownership import migrate_priorities_ownership
    from registry.shared_principals import artifact_owner
    scope = ownership_scope
    db = scope['db']
    project = scope['root'] / 'context' / 'PRIORITIES.md'
    agent = scope['root'] / '.threads' / scope['owner'] / 'context' / 'PRIORITIES.md'
    for path in (project, agent):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('content\n')
    artifact = service.register_write_strict(scope['owner'], str(agent), db=db)
    rehome_artifact(integration=artifact['integration'], type=artifact['type'], external_id=artifact['external_id'],
                    new_owner_thread_id=scope['owner'], actor_thread_id=None, authority='test', reason='test',
                    source_path='test', db=db)
    assert artifact_owner(artifact['id'], db=db) == scope['owner']
    assert migrate_priorities_ownership(scope['workspace'], db=db) == 2
    for path in (project, agent):
        artifact = service.find_file_identity(str(path), db=db)
        assert artifact_owner(artifact['id'], db=db) == 'ui'
    events = db.execute('SELECT count(*) FROM artifact_registration_events').fetchone()[0]
    assert migrate_priorities_ownership(scope['workspace'], db=db) == 0
    assert db.execute('SELECT count(*) FROM artifact_registration_events').fetchone()[0] == events


def test_later_starts_register_missed_files_but_keep_the_persons_reassignment(ownership_scope):
    from registry import service
    from registry.ownership import rehome_artifact
    from registry.priorities_ownership import migrate_priorities_ownership
    from registry.shared_principals import artifact_owner
    scope = ownership_scope
    db = scope['db']
    project = scope['root'] / 'context' / 'PRIORITIES.md'
    project.parent.mkdir(parents=True, exist_ok=True)
    project.write_text('content\n')
    assert migrate_priorities_ownership(scope['workspace'], db=db) == 1
    artifact = service.find_file_identity(str(project), db=db)
    rehome_artifact(integration=artifact['integration'], type=artifact['type'], external_id=artifact['external_id'],
                    new_owner_thread_id=scope['conductor'], actor_thread_id=None, authority='test', reason='test',
                    source_path='test', db=db)
    missed = scope['root'] / '.threads' / scope['other'] / 'context' / 'PRIORITIES.md'
    missed.parent.mkdir(parents=True, exist_ok=True)
    missed.write_text('content\n')
    assert service.find_file_identity(str(missed), db=db) is None
    assert migrate_priorities_ownership(scope['workspace'], db=db) == 1
    assert artifact_owner(service.find_file_identity(str(missed), db=db)['id'], db=db) == 'ui'
    assert artifact_owner(artifact['id'], db=db) == scope['conductor']
