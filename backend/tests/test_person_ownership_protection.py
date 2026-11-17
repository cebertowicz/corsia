"""A person's document stays theirs across writes and ownership assignment."""

import pytest

from tests.test_shared_ownership_assignment import assignment_scope as assignment_scope, _register
from tests.test_owner_only_edit import ownership_scope as ownership_scope, _shared_doc, _write_text, _suggestion
from tests.test_shared_document_ownership import ownership_scope as index_scope, _enforce


def test_assignment_preserves_person_owner_despite_agent_header(assignment_scope):
    from registry import models
    from registry.shared_ownership_assignment import apply_shared_ownership
    from registry.shared_principals import artifact_owner

    scope = assignment_scope
    path = scope["records"] / "person.md"
    header = f"**Status:** current · **Owner:** Owner Lane ({scope['owner']}) · **Review:** annually\n"
    path.write_text(header, encoding="utf-8")
    artifact_id = _register(scope, path, scope["owner"])
    artifact = models.get_artifact(artifact_id, db=scope["db"])
    models.rehome_artifact(
        integration=artifact["integration"], type=artifact["type"], external_id=artifact["external_id"],
        new_owner_thread_id="ui", actor_thread_id=None, authority="signed-in-person",
        reason="test", source_path="test", db=scope["db"],
    )
    apply_shared_ownership(scope["workspace"], "project-alpha", apply=True, db=scope["db"])
    assert artifact_owner(artifact_id, db=scope["db"]) == "ui"
    assert path.read_text(encoding="utf-8") == header


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["conductor", "helper"])
@pytest.mark.parametrize("owner", ["ui", "project:project-alpha"])
async def test_conductor_and_worker_cannot_edit_person_owned_document_but_suggest(ownership_scope, actor, owner):
    from registry import models
    from registry.shared_write_errors import DurableWritePolicyError
    from tests.registry_helpers import make_session

    scope = ownership_scope
    path, artifact_id = _shared_doc(scope)
    helper = make_session(scope["db"], project_id="project-alpha", title="Helper", prefix="helper")
    scope["db"].execute("UPDATE threads SET spawned_by=? WHERE id=?", (scope["conductor"], helper))
    scope["db"].commit()
    scope["helper"] = helper
    artifact = models.get_artifact(artifact_id, db=scope["db"])
    models.rehome_artifact(
        integration=artifact["integration"], type=artifact["type"], external_id=artifact["external_id"],
        new_owner_thread_id=owner, actor_thread_id=None, authority="signed-in-person",
        reason="test", source_path="test", db=scope["db"],
    )
    with pytest.raises(DurableWritePolicyError) as caught:
        _write_text(scope, path, actor)
    refusal = str(caught.value)
    assert "Only the person edits it directly." in refusal
    assert "the conductor, or the user edits" not in refusal
    assert "doc_comment_create" in refusal and "comment instead" in refusal
    assert path.read_text(encoding="utf-8") == "alpha beta gamma\n"
    assert await _suggestion(scope, path, author=actor)


def test_conductor_can_still_edit_unowned_and_agent_owned_documents(ownership_scope):
    from tests.test_owner_only_edit import _managed

    scope = ownership_scope
    unowned = scope["root"] / "context" / "unowned.md"
    unowned.parent.mkdir(exist_ok=True)
    unowned.write_text("before\n", encoding="utf-8")
    _managed(scope, unowned, "conductor", "after\n")
    assert unowned.read_text(encoding="utf-8") == "after\n"
    owned, _ = _shared_doc(scope)
    _write_text(scope, owned, "conductor")
    assert owned.read_text(encoding="utf-8") == "changed\n"


def test_person_owned_index_refuses_conductor_content_but_allows_mechanical_append(index_scope):
    from registry import models, service
    from registry.durable_writes import write_text
    from registry.shared_write_errors import DurableWritePolicyError

    scope = index_scope
    index = scope["category"] / "INDEX.md"
    artifact = service.register_write_strict(scope["conductor"], str(index), db=scope["db"])
    models.rehome_artifact(
        integration=artifact["integration"], type=artifact["type"], external_id=artifact["external_id"],
        new_owner_thread_id="ui", actor_thread_id=None, authority="signed-in-person",
        reason="test", source_path="test", db=scope["db"],
    )
    _enforce(scope)
    common = {"thread_id": scope["conductor"], "project_id": "project-alpha", "workspace": scope["workspace"]}
    with pytest.raises(DurableWritePolicyError):
        write_text(index, "# Documents\nEditorial change.\n", **common)
    assert index.read_text(encoding="utf-8") == "# Documents\n"
    (scope["category"] / "new.md").write_text("body\n", encoding="utf-8")
    write_text(index, "# Documents\n- `new.md` — New\n", **common)
    assert index.read_text(encoding="utf-8") == "# Documents\n- `new.md` — New\n"


def test_conductor_cannot_relocate_person_owned_document(ownership_scope):
    from registry import models
    from registry.relocate import RelocationError, checked_source_path

    scope = ownership_scope
    path, artifact_id = _shared_doc(scope)
    artifact = models.get_artifact(artifact_id, db=scope["db"])
    models.rehome_artifact(
        integration=artifact["integration"], type=artifact["type"], external_id=artifact["external_id"],
        new_owner_thread_id="ui", actor_thread_id=None, authority="signed-in-person",
        reason="test", source_path="test", db=scope["db"],
    )
    with pytest.raises(RelocationError, match="Only the person"):
        checked_source_path(artifact_id, str(path.with_name("moved.md")), actor_thread_id=scope["conductor"], db=scope["db"])
    assert path.exists()
