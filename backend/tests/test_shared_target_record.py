"""A shared write decision crosses the live journal as one explicit record shape."""
from dataclasses import asdict, fields
import json
from pathlib import Path

import pytest

from doceditor.live_publication_record import Publication
from registry.shared_principals import WriteAuthority
from registry.shared_target import SharedTarget


def target() -> SharedTarget:
    root = Path("/ws/shared/notes")
    return SharedTarget(root / "a.md", root, root / "INDEX.md", True, "project-alpha",
                        actor_principal="t-1", owner_thread_id="t-2",
                        update_trigger="monthly", outside_change=True, classification="minor",
                        authority=WriteAuthority("conductor", "t-2", "shared/notes/a.md", "t-1", "a-1"))


def test_record_round_trips_every_field_through_json():
    record = json.loads(json.dumps(target().to_record()))

    assert SharedTarget.from_record(record) == target()
    assert set(record) == {field.name for field in fields(SharedTarget)}


def test_record_written_before_the_record_methods_still_decodes():
    """Pending journal rows written by the asdict shape must survive an upgrade."""
    legacy = asdict(target())
    for key in ("path", "category_root", "index_path"):
        legacy[key] = str(legacy[key])

    assert SharedTarget.from_record(legacy) == target()


def test_record_missing_optional_fields_uses_their_defaults():
    record = {key: value for key, value in target().to_record().items()
              if key in ("path", "category_root", "index_path", "create_parent")}

    restored = SharedTarget.from_record(record)

    assert restored.owner_thread_id is None and restored.outside_change is False


def test_record_refuses_unknown_keys():
    with pytest.raises(ValueError, match="unexpected"):
        SharedTarget.from_record({**target().to_record(), "surprise": 1})


def test_publication_owns_the_plain_and_shared_record_shapes():
    plain = Publication.decision_for(Path("/ws/notes.md"), None)
    shared = Publication.decision_for(target().path, target())
    authority = WriteAuthority("user", "t-2", "notes.md", "ui", "a-2")
    decided = Publication.decision_for(Path("/ws/notes.md"), None, authority)

    assert plain == {"path": "/ws/notes.md", "plain": True, "authority": None}
    assert Publication("t-1", None, None, b"", decided).authority() == authority
    assert Publication("t-1", None, None, b"", shared).authority() == target().authority
    assert Publication("t-1", None, None, b"", plain).shared_target() is None
    assert Publication("t-1", None, None, b"", shared).shared_target() == target()
    assert Publication("t-1", None, None, b"", shared).target_path() == target().path
    assert Publication("t-1", None, None, b"", plain).target_path() == Path("/ws/notes.md")
