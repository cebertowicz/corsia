from unittest.mock import Mock

import pytest

from api.files_revision import _writable
from core.system_projection import ensure_system_projection, is_system_projection_path
from doceditor.document_save import DocumentSave, DocumentSaveError, _validate_document_write
from infra import config
from registry.path_access import ReadRefused, checked_read


def project_system_file(workspace):
    """Project the System package into ``workspace``; return one generated context file."""
    projection = ensure_system_projection(workspace)
    assert projection is not None
    return next(path for path in sorted((projection / 'context').rglob('*.md')) if path.is_file())


def test_projection_is_readable_from_project_but_cannot_be_saved(tmp_path, monkeypatch):
    workspace = tmp_path / 'workspace'
    projection = project_system_file(workspace)
    original = projection.read_text()
    project = workspace / 'projects' / 'example'
    project.mkdir(parents=True)
    monkeypatch.setattr(config, 'WORKSPACE', workspace)
    monkeypatch.setattr('doceditor.document_save.get_db', lambda: Mock())
    monkeypatch.setattr('doceditor.document_save.scope_bases', lambda *_: [project])

    assert is_system_projection_path(projection, workspace)
    assert checked_read(str(projection), [project]) == projection
    assert not _writable(projection, [workspace])
    with pytest.raises(DocumentSaveError) as refusal:
        _validate_document_write(DocumentSave(path=str(projection), text='changed', project_id='project'), 'user')
    assert refusal.value.kind == 'context_read_only'
    assert projection.read_text() == original
    assert not is_system_projection_path(projection.parent, workspace)
    assert not is_system_projection_path(project, workspace)


def test_other_files_under_dot_corsia_are_not_the_projection(tmp_path, monkeypatch):
    workspace = tmp_path / 'workspace'
    project_system_file(workspace)
    root = workspace / '.corsia'
    others = [root / 'notes.md', root / 'context' / 'undeclared.md', root / 'corsia.db', root / 'MANIFEST.json']
    for path in others[:3]:
        path.write_text('not generated')
    project = workspace / 'projects' / 'example'
    project.mkdir(parents=True)
    monkeypatch.setattr(config, 'WORKSPACE', workspace)

    for path in others:
        assert not is_system_projection_path(path, workspace)
        with pytest.raises(ReadRefused):
            checked_read(str(path), [project])
    assert _writable(root / 'notes.md', [workspace])


def test_a_workspace_skill_opens_from_a_project_and_saves_only_from_the_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / 'workspace'
    skill = workspace / 'skills' / 'alpha' / 'SKILL.md'
    skill.parent.mkdir(parents=True)
    skill.write_text('skill content')
    beside = workspace / 'skillsets.md'
    beside.write_text('not a skill')
    project = workspace / 'projects' / 'example'
    project.mkdir(parents=True)
    monkeypatch.setattr(config, 'WORKSPACE', workspace)

    assert checked_read(str(skill), [project]) == skill
    assert not _writable(skill, [project])
    assert _writable(skill, [workspace])
    with pytest.raises(ReadRefused):
        checked_read(str(beside), [project])


def test_a_workspace_role_file_opens_from_a_project_and_saves_only_from_the_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / 'workspace'
    role = workspace / 'context' / 'roles' / 'agent'
    role_skill = role / 'skills' / 'alpha' / 'SKILL.md'
    role_skill.parent.mkdir(parents=True)
    role_skill.write_text('role skill content')
    role_context = role / '10-role.md'
    role_context.write_text('role context')
    beside = workspace / 'context' / 'rolesets' / 'agent.md'
    beside.parent.mkdir(parents=True)
    beside.write_text('not a role file')
    project = workspace / 'projects' / 'example'
    project.mkdir(parents=True)
    monkeypatch.setattr(config, 'WORKSPACE', workspace)

    for role_file in (role_skill, role_context):
        assert checked_read(str(role_file), [project]) == role_file
        assert not _writable(role_file, [project])
        assert _writable(role_file, [workspace])
    with pytest.raises(ReadRefused):
        checked_read(str(beside), [project])


def test_a_file_no_verified_projection_generated_is_not_the_projection(tmp_path, monkeypatch):
    workspace = tmp_path / 'workspace'
    by_hand = workspace / '.corsia' / 'context' / 'example.md'
    by_hand.parent.mkdir(parents=True)
    by_hand.write_text('looks generated')
    assert not is_system_projection_path(by_hand, workspace)
    monkeypatch.setattr('core.system_projection._agent_readable', lambda _projection: False)
    assert ensure_system_projection(workspace) is None
    assert not is_system_projection_path(by_hand, workspace)
