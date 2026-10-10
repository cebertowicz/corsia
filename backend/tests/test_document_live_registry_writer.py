"""Only writers set last_written_sha256; registration never overwrites it."""
import hashlib

from doceditor.live_publisher import publish_one
from registry import service
from registry.identity_metadata import last_written_sha256
from registry.write_preparation import prepare_file_write
from tests.test_document_live_publisher import setup, owe
from tests.test_shared_document_ownership import ownership_scope as ownership_scope, _write


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def stored_hash(scope, name="live.md"):
    artifact = service.find_file_identity(str(scope["category"] / name), db=scope["db"])
    return last_written_sha256(artifact["metadata"])


def test_explicit_writer_hash_survives_a_publication_committed_after_it_prepared(ownership_scope, tmp_path):
    scope = ownership_scope
    store = setup(scope, tmp_path)
    path = scope["category"] / "live.md"
    owe(scope, store, "old", "new")
    path.write_text("explicit")
    # A durable writer prepares its file facts outside its transaction, as
    # durable_writes does, and the publisher commits in between.
    prepared = prepare_file_write(str(path), scope["owner"], db=scope["db"])
    row = store.next_publication()
    publish_one(store, row, db=scope["db"])
    service.register_write_strict(scope["owner"], prepared, metadata={"last_written_sha256": digest("explicit")},
                                  db=scope["db"])
    assert stored_hash(scope) == digest("explicit")


def test_registration_without_a_hash_never_overwrites_a_published_one(ownership_scope, tmp_path):
    scope = ownership_scope
    store = setup(scope, tmp_path)
    owe(scope, store, "old", "new")
    row = store.next_publication()
    publish_one(store, row, db=scope["db"])
    path = scope["category"] / "live.md"
    path.write_text("changed outside")
    service.register_write_strict(scope["owner"], str(path), db=scope["db"])
    assert stored_hash(scope) == digest("new")


def test_registration_without_a_hash_never_overwrites_a_written_one(ownership_scope):
    scope = ownership_scope
    _write(scope, "plain.md", "written")
    path = scope["category"] / "plain.md"
    path.write_text("changed outside")
    service.register_write_strict(scope["owner"], str(path), db=scope["db"])
    assert stored_hash(scope, "plain.md") == digest("written")


def test_observed_tool_write_states_the_hash_it_wrote(ownership_scope):
    from registry.observed_write import register_observed_write
    scope = ownership_scope
    _write(scope, "plain.md", "written")
    path = scope["category"] / "plain.md"
    path.write_text("tool wrote")
    register_observed_write(scope["owner"], {"path": str(path)}, ("path", None), "", name="edit_file",
                            is_cc_builtin=False, origin=None, db=scope["db"])
    assert stored_hash(scope, "plain.md") == digest("tool wrote")


def test_plain_save_states_the_hash_it_wrote(ownership_scope):
    from doceditor.document_save import DocumentSave, _write_document
    scope = ownership_scope
    path = scope["root"] / ".threads" / scope["owner"] / "notes.md"
    for text in ("first", "second"):
        body = DocumentSave(str(path), text, project_id="project-alpha", thread_id=scope["owner"], create=True)
        assert _write_document(body, path, direct_user=False)["artifact"] is not None
    artifact = service.find_file_identity(str(path), db=scope["db"])
    assert last_written_sha256(artifact["metadata"]) == digest("second")


def test_first_registration_seeds_the_local_hash(ownership_scope):
    scope = ownership_scope
    path = scope["category"] / "unregistered.md"
    path.write_text("first")
    service.register_write_strict(scope["owner"], str(path), db=scope["db"])
    assert stored_hash(scope, "unregistered.md") == digest("first")
