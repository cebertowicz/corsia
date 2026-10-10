"""One way to find a document's scope: legacy project rows resolve like every other caller's.

``document_scope`` resolves each project row with the canonical project-root
resolver, so a row the startup converter still keeps (no stored path, a path
that is not in resolved form, or a ``.../projects/<name>`` path under another
root) holds its documents exactly as the governed shared-write path decides
them, whichever path asks: file tool, shell detection, plain save or accept.
"""
from __future__ import annotations

import time

import pytest

from tests.test_owner_only_edit import (
    SUGGEST_TOOL,
    _owner_events,
    _shared_doc,
    _write_text,
    ownership_scope as ownership_scope,  # noqa: F401 - the fixture
)
from tests.test_shared_document_ownership import _shell_write

LEGACY_ROWS = {
    "null_path": lambda workspace: None,
    "not_resolved_form": lambda workspace: f"{workspace}/projects/../projects/alpha",
    "under_another_root": lambda workspace: "/old-install/workspace/projects/alpha",
}


@pytest.fixture(params=sorted(LEGACY_ROWS))
def legacy_scope(ownership_scope, request):
    scope = ownership_scope
    scope["db"].execute(
        "UPDATE projects SET path=? WHERE id='project-alpha'",
        (LEGACY_ROWS[request.param](scope["workspace"]),),
    )
    scope["db"].commit()
    return scope


def test_document_scope_and_the_governed_path_agree_on_a_legacy_row(legacy_scope):
    from registry.shared_documents import _scope_root
    from registry.shared_principals import document_scope, scope_conductor

    scope = legacy_scope
    path, _ = _shared_doc(scope, "legacy-row.md")
    root, conductor = document_scope(path, workspace=scope["workspace"], db=scope["db"])
    governed_root, project = _scope_root(
        workspace=scope["workspace"], thread_id=scope["conductor"], project_id="project-alpha",
        direct_user=False, db=scope["db"],
    )
    assert (root, conductor) == (governed_root, scope_conductor(project, db=scope["db"]))
    assert root == scope["root"].resolve()
    assert conductor == scope["conductor"]


def test_conductor_shell_edit_and_file_tool_edit_agree_on_a_legacy_row(legacy_scope):
    from tools.shared_write_detection import detect_shell_shared_writes

    scope = legacy_scope
    path, artifact_id = _shared_doc(scope, "legacy-shell.md")
    _write_text(scope, path, "conductor", content="file tool edit\n")
    started = time.time_ns()
    _shell_write(path, "shell edit\n", started)
    notices = detect_shell_shared_writes(
        scope["conductor"], "project-alpha", "printf x > shared/documents/legacy-shell.md",
        started, scope["root"],
    )
    assert not any(SUGGEST_TOOL in notice for notice in notices)
    assert [kind for kind, _ in _owner_events(scope["db"], artifact_id)] == [
        "content_edited_by_non_owner", "content_edited_by_non_owner",
    ]


def test_a_non_owner_agent_is_refused_on_a_legacy_row(legacy_scope):
    from registry.shared_write_errors import DurableWritePolicyError

    scope = legacy_scope
    path, _ = _shared_doc(scope, "legacy-refused.md")
    with pytest.raises(DurableWritePolicyError):
        _write_text(scope, path, "other")
