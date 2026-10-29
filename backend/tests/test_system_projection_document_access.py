from unittest.mock import Mock

import pytest

from api.files_revision import _writable
from core.system_projection import is_system_projection_path
from doceditor.document_save import DocumentSave, DocumentSaveError, _validate_document_write
from infra import config
from registry.path_access import checked_read


def test_projection_is_readable_from_project_but_cannot_be_saved(tmp_path, monkeypatch):
    workspace = tmp_path / 'workspace'
    projection = workspace / '.corsia' / 'context' / 'example.md'
    projection.parent.mkdir(parents=True)
    projection.write_text('system content')
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
    assert projection.read_text() == 'system content'
    assert not is_system_projection_path(projection.parent, workspace)
    assert not is_system_projection_path(project, workspace)
