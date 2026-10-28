"""An ownership change records accountability; it never adds a pane row."""

from __future__ import annotations

import pytest

from infra import config
from infra.database import get_db
from registry import ownership
from registry.view import _artifacts_for_thread
from tests.registry_helpers import make_session, seed_artifact

_THREADS = ("owner", "target", "bystander")


@pytest.fixture
def owned(isolated_db):
    db = get_db()
    threads = {name: make_session(db, prefix=name) for name in _THREADS}
    seed_artifact(
        db, artifact_id="doc", integration="test", type="document",
        external_id="doc", parent_thread_id=threads["owner"], title="A document",
    )
    db.commit()
    return db, threads


def _pane(db, threads) -> dict[str, list[str]]:
    """What each thread's pane shows, through the pane's own predicate."""
    return {
        name: [row["id"] for row in _artifacts_for_thread(db, thread_id)]
        for name, thread_id in threads.items()
    }


def _change(operation, db, **extra):
    return getattr(ownership, f"{operation}_artifact")(
        integration="test", type="document", external_id="doc",
        actor_thread_id=None, authority="test", reason="test",
        source_path="test:ownership", db=db, **extra,
    )


def _owner(db) -> str | None:
    row = db.execute(
        "SELECT parent_thread_id FROM artifact_registrations "
        "WHERE artifact_id='doc' AND relationship_state IN ('owner-pinned','owner-active')",
    ).fetchone()
    return row[0] if row else None


def _retirement_events(db) -> list[tuple[str, str]]:
    return [
        (row["affected_thread_id"], row["metadata"])
        for row in db.execute(
            "SELECT affected_thread_id,metadata FROM artifact_registration_events "
            "WHERE event_type='ownership_row_retired' ORDER BY rowid",
        )
    ]


def test_automatic_rehome_moves_the_owner_and_leaves_every_pane_as_it_was(owned):
    db, threads = owned
    before = _pane(db, threads)

    _change(
        "rehome", db,
        new_owner_thread_id=threads["target"], show_to_new_owner=False,
    )

    assert _owner(db) == threads["target"]
    assert _pane(db, threads) == before
    events = _retirement_events(db)
    assert [thread for thread, _ in events] == [threads["target"]]
    assert '"trigger": "ownership_rehomed"' in events[0][1]


def test_deliberate_hand_off_shows_the_document_to_the_new_owner(owned):
    db, threads = owned
    before = _pane(db, threads)

    _change("rehome", db, new_owner_thread_id=threads["target"])

    assert _pane(db, threads) == {**before, "target": ["doc"]}
    assert _retirement_events(db) == []


def test_deliberate_hand_off_shows_a_document_an_earlier_ownership_change_hid(owned):
    db, threads = owned
    _change(
        "rehome", db,
        new_owner_thread_id=threads["target"], show_to_new_owner=False,
    )
    _change("rehome", db, new_owner_thread_id=threads["owner"])
    assert _pane(db, threads)["target"] == []

    _change("rehome", db, new_owner_thread_id=threads["target"])

    assert _pane(db, threads)["target"] == ["doc"]


def test_automatic_rehome_does_not_bring_back_a_retired_row(owned):
    db, threads = owned
    automatic = {"new_owner_thread_id": threads["target"], "show_to_new_owner": False}
    _change("rehome", db, **automatic)
    _change("rehome", db, new_owner_thread_id=threads["owner"])

    _change("rehome", db, **automatic)

    assert _pane(db, threads)["target"] == []


def test_automatic_rehome_never_hides_a_row_the_new_owner_already_had(owned):
    db, threads = owned
    db.execute(
        "INSERT INTO artifact_registrations "
        "(id,artifact_id,parent_thread_id,relationship_state,created_at,updated_at) "
        "VALUES ('reg:target','doc',?,'subscriber-recent','t','t')",
        (threads["target"],),
    )
    before = _pane(db, threads)
    assert before["target"] == ["doc"]

    _change(
        "rehome", db,
        new_owner_thread_id=threads["target"], show_to_new_owner=False,
    )

    assert _pane(db, threads) == before
    assert _retirement_events(db) == []


@pytest.mark.parametrize("operation", ("rehome", "release"))
def test_another_threads_triage_row_does_not_become_a_pane_row(owned, operation):
    db, threads = owned
    db.execute(
        "INSERT INTO artifact_registrations "
        "(id,artifact_id,parent_thread_id,relationship_state,created_at,updated_at) "
        "VALUES ('reg:bystander','doc',?,'unassigned-triage','t','t')",
        (threads["bystander"],),
    )
    before = _pane(db, threads)
    assert before["bystander"] == []
    extra = {"new_owner_thread_id": threads["target"]} if operation == "rehome" else {}

    _change(operation, db, **extra)

    assert _pane(db, threads)["bystander"] == []
    assert threads["bystander"] in [thread for thread, _ in _retirement_events(db)]


def test_release_keeps_the_previous_owners_row_and_adds_none(owned):
    db, threads = owned
    before = _pane(db, threads)

    _change("release", db, triage_thread_id=threads["target"])

    assert _owner(db) is None
    assert _pane(db, threads) == before
    assert _retirement_events(db) == []


def test_pin_brings_back_a_row_an_ownership_change_retired(owned):
    from registry.pinning import update_pinned

    db, threads = owned
    _change(
        "rehome", db,
        new_owner_thread_id=threads["target"], show_to_new_owner=False,
    )
    assert _pane(db, threads)["target"] == []

    update_pinned("doc", pinned=True, parent_thread_id=threads["target"], db=db)

    assert _pane(db, threads)["target"] == ["doc"]


def test_unpin_hides_a_task_scoped_row_an_ownership_change_retired(owned):
    from registry.pinning import update_pinned

    db, threads = owned
    _change(
        "rehome", db,
        new_owner_thread_id=threads["target"], show_to_new_owner=False,
    )
    update_pinned("doc", pinned=True, parent_thread_id=threads["target"], db=db)

    update_pinned("doc", pinned=False, parent_thread_id=threads["target"], db=db)

    assert _pane(db, threads)["target"] == []


def test_unpin_keeps_an_exempt_document_an_ownership_change_retired(owned):
    """Once pinned it is there by the user's choice, like any exempt document."""
    from registry.pinning import update_pinned

    db, threads = owned
    _change(
        "rehome", db,
        new_owner_thread_id=threads["target"], show_to_new_owner=False,
    )
    db.execute(
        "UPDATE artifact_identities SET retention='durable', metadata=?, path=?, "
        "external_id='workspace:' || '//ws/shared/notes/doc.md' WHERE id='doc'",
        ('{"zone": "workspace"}', str(config.WORKSPACE / "shared/notes/doc.md")),
    )
    # The user's comment is what exempts it; being shared does not.
    db.execute(
        "INSERT INTO doc_comments "
        "(id,artifact_id,author,author_kind,body,created_at,updated_at) "
        "VALUES ('c-doc','doc','user','user','Check this.','t0','t0')"
    )
    db.commit()
    update_pinned("doc", pinned=True, parent_thread_id=threads["target"], db=db)

    update_pinned("doc", pinned=False, parent_thread_id=threads["target"], db=db)

    assert _pane(db, threads)["target"] == ["doc"]
