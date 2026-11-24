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
    with pytest.raises(DurableWritePolicyError):
        _managed(scope, path, 'owner', 'changed\n')
    assert path.read_text() == 'content\n'


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
