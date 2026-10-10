from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from events import delivery
from registry import models, pinning, registration_writer
from registry.identity_bridge import public_row_from_normalized


def _rows(db, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(row) for row in db.execute(sql, params).fetchall()]


def _register(db, external_id: str = "org/repo#85", owner: str = "thread-a"):
    return models.register_artifact(
        integration="github",
        type="pull-request",
        external_id=external_id,
        parent_thread_id=owner,
        title=f"PR {external_id}",
        db=db,
    )


def test_register_uses_one_identity_and_owner_preserving_registrations(isolated_db):
    from infra.database import get_db

    db = get_db()
    try:
        first = models.register_artifact(
            integration="workspace",
            type="document",
            external_id="/workspace/spec.md",
            parent_thread_id="thread-a",
            title="spec.md",
            path="/workspace/spec.md",
            metadata={"zone": "workspace"},
            db=db,
        )
        second = models.register_artifact(
            integration="workspace",
            type="document",
            external_id="/workspace/spec.md",
            parent_thread_id="thread-b",
            title="spec v2.md",
            db=db,
        )

        assert second["id"] == first["id"]
        assert second["parent_thread_id"] == "thread-a"
        assert second["title"] == "spec v2.md"
        assert _rows(
            db,
            "SELECT parent_thread_id, relationship_state, tmp "
            "FROM artifact_registrations WHERE artifact_id=? "
            "ORDER BY parent_thread_id",
            (first["id"],),
        ) == [
            {"parent_thread_id": "thread-a", "relationship_state": "owner-active", "tmp": 0},
            {"parent_thread_id": "thread-b", "relationship_state": "subscriber-recent", "tmp": 0},
        ]
    finally:
        db.close()


def test_public_projection_has_exact_shape_and_merges_scoped_metadata():
    row = {
        "identity_id": "identity-merge",
        "integration": "workspace",
        "type": "document",
        "external_id": "merge.md",
        "title": "merge.md",
        "description": None,
        "path": "/workspace/merge.md",
        "state": "active",
        "retention": "durable",
        "metadata": '{"display":{"layout":"wide"},"nested":{"identity":true}}',
        "registration_metadata": (
            '{"display":{"custom_flag":true},'
            '"nested":{"registration":true}}'
        ),
        "created_at": "2026-06-21T00:00:00+00:00",
        "updated_at": "2026-06-21T00:00:00+00:00",
        "projected_updated_at": "2026-06-21T00:02:00+00:00",
        "parent_thread_id": "thread-owner",
        "surfacing": "visible",
        "registration_updated_at": "2026-06-21T00:01:00+00:00",
        "relationship_state": "owner-active",
    }

    public = public_row_from_normalized(row)

    assert set(public) == {
        "id", "integration", "type", "external_id", "parent_thread_id",
        "title", "description", "path", "state", "retention", "surfacing",
        "relationship_state", "metadata", "created_at", "updated_at",
    }
    assert json.loads(public["metadata"]) == {
        "display": {"custom_flag": True, "layout": "wide"},
        "nested": {"identity": True, "registration": True},
    }
    assert public["relationship_state"] == "owner-active"
    assert public["updated_at"] == "2026-06-21T00:02:00+00:00"


def test_update_pinned_transitions_owner_and_audits_changes_once(isolated_db):
    from infra.database import get_db

    db = get_db()
    try:
        artifact = _register(db)
        pinned = pinning.update_pinned(
            artifact["id"], pinned=True, parent_thread_id="thread-a", db=db,
        )
        assert pinned["relationship_state"] == "owner-pinned"
        unpinned = pinning.update_pinned(
            artifact["id"], pinned=False, parent_thread_id="thread-a", db=db,
        )
        assert unpinned["relationship_state"] == "owner-active"
        pinning.update_pinned(
            artifact["id"], pinned=False, parent_thread_id="thread-a", db=db,
        )

        events = _rows(
            db,
            """SELECT event_type, authority, source_path, old_relationship_state,
                      new_relationship_state
               FROM artifact_registration_events
               WHERE artifact_id=? AND event_type='pin_changed'
               ORDER BY created_at, id""",
            (artifact["id"],),
        )
        assert events == [
            {
                "event_type": "pin_changed",
                "authority": "pane-persistence",
                "source_path": "registry.pinning.update_pinned",
                "old_relationship_state": "owner-active",
                "new_relationship_state": "owner-pinned",
            },
            {
                "event_type": "pin_changed",
                "authority": "pane-persistence",
                "source_path": "registry.pinning.update_pinned",
                "old_relationship_state": "owner-pinned",
                "new_relationship_state": "owner-active",
            },
        ]
    finally:
        db.close()


def test_update_pinned_rejects_subscriber_registration(isolated_db):
    from infra.database import get_db

    db = get_db()
    try:
        artifact = _register(db, owner="thread-owner")
        models.register_artifact(
            integration="github",
            type="pull-request",
            external_id=artifact["external_id"],
            parent_thread_id="thread-subscriber",
            db=db,
        )

        assert pinning.update_pinned(
            artifact["id"],
            pinned=True,
            parent_thread_id="thread-subscriber",
            db=db,
        ) is None
        registration = db.execute(
            """SELECT relationship_state FROM artifact_registrations
               WHERE artifact_id=? AND parent_thread_id='thread-subscriber'""",
            (artifact["id"],),
        ).fetchone()
        assert registration["relationship_state"] == "subscriber-recent"
    finally:
        db.close()


def test_public_updated_at_tracks_identity_registration_and_archive_updates(
    isolated_db, monkeypatch
):
    from infra.database import get_db

    clock = {"now": "2026-06-21T00:00:00+00:00"}
    monkeypatch.setattr(models, "_now", lambda: clock["now"])
    monkeypatch.setattr(registration_writer, "_now", lambda: clock["now"])
    db = get_db()
    try:
        artifact = models.register_artifact(
            integration="workspace",
            type="document",
            external_id="/workspace/timestamps.md",
            parent_thread_id="thread-a",
            title="timestamps.md",
            db=db,
        )

        clock["now"] = "2026-06-21T00:01:00+00:00"
        described = models.update_description(
            artifact["id"], "current description", db=db
        )
        assert described["updated_at"] == clock["now"]

        clock["now"] = "2026-06-21T00:02:00+00:00"
        metadata = models.update_metadata(
            "workspace",
            "document",
            "/workspace/timestamps.md",
            {"phase": "review"},
            db=db,
        )
        assert metadata["updated_at"] == clock["now"]

        clock["now"] = "2026-06-21T00:03:00+00:00"
        displayed = models.update_surfacing(
            artifact["id"], surfacing=models.SURFACING_QUIET,
            parent_thread_id="thread-a", db=db,
        )
        assert displayed["updated_at"] == clock["now"]

        clock["now"] = "2026-06-21T00:04:00+00:00"
        registered = models.register_artifact(
            integration="workspace",
            type="document",
            external_id="/workspace/timestamps.md",
            parent_thread_id="thread-a",
            db=db,
        )
        assert registered["updated_at"] == clock["now"]

        clock["now"] = "2026-06-21T00:05:00+00:00"
        models.retire(artifact["id"], db=db)
        archived = models.get_artifact(artifact["id"], db=db)
        assert archived["state"] == "archived"
        assert archived["updated_at"] == clock["now"]
    finally:
        db.close()


@pytest.mark.parametrize("field,key", [
    ("metadata", "identity"),
    ("registration_metadata", "registration"),
])
def test_public_projection_marks_malformed_metadata(field, key):
    row = {
        "identity_id": "identity-bad-json",
        "integration": "workspace",
        "type": "document",
        "external_id": "bad-json.md",
        "title": "bad-json.md",
        "description": None,
        "path": "/workspace/bad-json.md",
        "state": "active",
        "metadata": '{"source":"identity"}',
        "registration_metadata": None,
        "created_at": "2026-06-21T00:00:00+00:00",
        "updated_at": "2026-06-21T00:00:00+00:00",
        "parent_thread_id": "thread-owner",
        "retention": "durable",
        "surfacing": "visible",
        "registration_updated_at": "2026-06-21T00:01:00+00:00",
        "relationship_state": "owner-active",
    }
    row[field] = '{"bad"'

    public = public_row_from_normalized(row)

    assert json.loads(public["metadata"])["registry_metadata_error"] == {
        key: "invalid_json"
    }


def test_resolve_and_delivery_use_owner_registration(isolated_db):
    from infra.database import get_db

    db = get_db()
    try:
        row = _register(db, "org/repo#42", "thread-owner")
        assert models.resolve(
            "github", "pull-request", "org/repo#42", db=db
        )["id"] == row["id"]
        assert delivery.owning_thread_for_identity(
            "github", "pull-request", "org/repo#42"
        ) == "thread-owner"
    finally:
        db.close()


def test_register_appends_registration_event(isolated_db):
    from infra.database import get_db

    db = get_db()
    try:
        row = _register(db)
        assert _rows(
            db,
            "SELECT event_type, actor_thread_id, authority, affected_thread_id, "
            "old_owner_thread_id, new_owner_thread_id, new_relationship_state "
            "FROM artifact_registration_events WHERE artifact_id=?",
            (row["id"],),
        ) == [{
            "event_type": "registered",
            "actor_thread_id": "thread-a",
            "authority": "register",
            "affected_thread_id": "thread-a",
            "old_owner_thread_id": None,
            "new_owner_thread_id": "thread-a",
            "new_relationship_state": "owner-active",
        }]
    finally:
        db.close()


def test_release_routes_only_to_explicit_triage(isolated_db):
    from infra.database import get_db

    db = get_db()
    try:
        _register(db, "org/repo#44", "thread-owner")
        models.release_artifact(
            integration="github",
            type="pull-request",
            external_id="org/repo#44",
            actor_thread_id="thread-owner",
            authority="current-owner-handoff",
            reason="owner released",
            source_path="test",
            triage_thread_id="thread-triage",
            db=db,
        )
        assert models.resolve(
            "github", "pull-request", "org/repo#44", db=db
        ) is None
        assert delivery.owning_thread_for_identity(
            "github", "pull-request", "org/repo#44"
        ) == "thread-triage"
    finally:
        db.close()


def test_unknown_identity_has_no_wake_target(isolated_db):
    assert delivery.owning_thread_for_identity(
        "github", "pull-request", "org/repo#999"
    ) is None


def test_rehome_appends_event_and_moves_owner(isolated_db):
    from infra.database import get_db

    db = get_db()
    try:
        original = _register(db, "org/repo#86", "thread-a")
        moved = models.rehome_artifact(
            integration="github",
            type="pull-request",
            external_id="org/repo#86",
            new_owner_thread_id="thread-b",
            actor_thread_id="thread-a",
            authority="current-owner-handoff",
            reason="rescue worker takeover",
            source_path="test",
            db=db,
        )

        assert moved["artifact"]["parent_thread_id"] == "thread-b"
        routing = models.routing_registrations(
            "github", "pull-request", "org/repo#86", db=db
        )
        assert routing["owner"]["parent_thread_id"] == "thread-b"
        assert [row["parent_thread_id"] for row in routing["subscribers"]] == [
            "thread-a"
        ]
        event = db.execute(
            "SELECT * FROM artifact_registration_events WHERE artifact_id=? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (original["id"],),
        ).fetchone()
        assert event["event_type"] == "ownership_rehomed"
        assert event["old_owner_thread_id"] == "thread-a"
        assert event["new_owner_thread_id"] == "thread-b"
    finally:
        db.close()


def test_register_after_release_adds_subscriber_without_recreating_owner(isolated_db):
    from infra.database import get_db

    db = get_db()
    try:
        _register(db, "org/repo#88", "thread-a")
        models.release_artifact(
            integration="github",
            type="pull-request",
            external_id="org/repo#88",
            actor_thread_id="thread-a",
            authority="current-owner-handoff",
            reason="owner released",
            source_path="test",
            db=db,
        )
        row = _register(db, "org/repo#88", "thread-b")

        assert row is None
        routing = models.routing_registrations(
            "github", "pull-request", "org/repo#88", db=db
        )
        assert routing["owner"] is None
        assert sorted(r["parent_thread_id"] for r in routing["subscribers"]) == [
            "thread-a", "thread-b",
        ]
    finally:
        db.close()


def test_register_after_release_stays_passive_when_all_registrations_were_deleted(
    isolated_db,
):
    from infra.database import get_db

    db = get_db()
    try:
        artifact = _register(db, "org/repo#released-without-registrations", "thread-a")
        models.release_artifact(
            integration="github",
            type="pull-request",
            external_id=artifact["external_id"],
            actor_thread_id="thread-a",
            authority="current-owner-handoff",
            reason="owner released",
            source_path="test",
            db=db,
        )
        db.execute(
            "DELETE FROM artifact_registrations WHERE artifact_id=?",
            (artifact["id"],),
        )

        registered = _register(
            db, "org/repo#released-without-registrations", "thread-b"
        )

        assert registered is None
        routing = models.routing_registrations(
            "github", "pull-request", artifact["external_id"], db=db
        )
        assert routing["owner"] is None
        assert [
            (row["parent_thread_id"], row["relationship_state"])
            for row in routing["subscribers"]
        ] == [("thread-b", "subscriber-recent")]
    finally:
        db.close()


def test_explicit_durable_promotes_when_unreleased_identity_becomes_owned(isolated_db):
    """An ownerless identity may promote when this registration claims ownership."""
    from infra.database import get_db

    db = get_db()
    try:
        artifact = models.register_artifact(
            integration="github",
            type="pull-request",
            external_id="org/repo#unreleased-ownerless",
            parent_thread_id="thread-a",
            retention="ephemeral",
            db=db,
        )
        db.execute(
            "DELETE FROM artifact_registrations WHERE artifact_id=?", (artifact["id"],)
        )

        promoted = models.register_artifact(
            integration="github",
            type="pull-request",
            external_id=artifact["external_id"],
            parent_thread_id="thread-b",
            retention="durable",
            db=db,
        )

        assert promoted["retention"] == "durable"
        assert promoted["parent_thread_id"] == "thread-b"
    finally:
        db.close()


def test_subscriber_cannot_promote_another_owners_ephemeral_identity(isolated_db):
    """Generic registration preserves the owner's identity-level policy."""
    from infra.database import get_db

    db = get_db()
    try:
        artifact = models.register_artifact(
            integration="github",
            type="pull-request",
            external_id="org/repo#foreign-owner",
            parent_thread_id="thread-a",
            retention="ephemeral",
            db=db,
        )

        models.register_artifact(
            integration="github",
            type="pull-request",
            external_id=artifact["external_id"],
            parent_thread_id="thread-b",
            retention="durable",
            db=db,
        )

        assert models.get_artifact(artifact["id"], db=db)["retention"] == "ephemeral"
        subscriber = db.execute(
            "SELECT relationship_state FROM artifact_registrations "
            "WHERE artifact_id=? AND parent_thread_id='thread-b'",
            (artifact["id"],),
        ).fetchone()
        assert subscriber["relationship_state"] == "subscriber-recent"
    finally:
        db.close()


@pytest.mark.parametrize(("initial_retention", "requested_retention"), [
    ("durable", "ephemeral"),
    ("ephemeral", "durable"),
])
def test_register_after_release_never_changes_identity_retention(
    isolated_db, initial_retention, requested_retention
):
    """Retention is identity-level and set once at creation; a later register
    call — even to a released, ownerless identity — never mutates it (write-
    time defaults are not ongoing semantics)."""
    from infra.database import get_db

    db = get_db()
    try:
        artifact = models.register_artifact(
            integration="github",
            type="pull-request",
            external_id=f"org/repo#retention-{initial_retention}",
            parent_thread_id="thread-a",
            retention=initial_retention,
            db=db,
        )
        models.release_artifact(
            integration="github",
            type="pull-request",
            external_id=artifact["external_id"],
            actor_thread_id="thread-a",
            authority="current-owner-handoff",
            reason="owner released",
            source_path="test",
            db=db,
        )

        models.register_artifact(
            integration="github",
            type="pull-request",
            external_id=artifact["external_id"],
            parent_thread_id="thread-b",
            retention=requested_retention,
            db=db,
        )

        registration = db.execute(
            "SELECT relationship_state, surfacing FROM artifact_registrations "
            "WHERE artifact_id=? AND parent_thread_id='thread-b'",
            (artifact["id"],),
        ).fetchone()
        assert dict(registration) == {
            "relationship_state": "subscriber-recent",
            "surfacing": "visible",
        }
        identity = db.execute(
            "SELECT retention FROM artifact_identities WHERE id=?",
            (artifact["id"],),
        ).fetchone()
        assert identity["retention"] == initial_retention
    finally:
        db.close()


def test_fresh_registration_surfacing_always_defaults_visible(isolated_db):
    """A new registration row (owner or subscriber) always starts visible —
    surfacing is never inherited from another thread's registration."""
    from infra.database import get_db

    db = get_db()
    try:
        artifact = models.register_artifact(
            integration="github",
            type="pull-request",
            external_id="org/repo#surfacing-default",
            parent_thread_id="thread-a",
            db=db,
        )
        models.update_surfacing(
            artifact["id"], surfacing=models.SURFACING_QUIET,
            parent_thread_id="thread-a", db=db,
        )

        models.register_artifact(
            integration="github",
            type="pull-request",
            external_id=artifact["external_id"],
            parent_thread_id="thread-b",
            db=db,
        )

        rows = {
            row["parent_thread_id"]: row["surfacing"]
            for row in db.execute(
                "SELECT parent_thread_id, surfacing FROM artifact_registrations "
                "WHERE artifact_id=?",
                (artifact["id"],),
            )
        }
        assert rows == {"thread-a": "quiet", "thread-b": "visible"}
    finally:
        db.close()


def test_register_reclaims_ownerless_artifact_without_current_release(isolated_db):
    from infra.database import get_db

    db = get_db()
    try:
        artifact = _register(db, "org/repo#ownerless", "thread-a")
        _register(db, "org/repo#ownerless", "thread-b")
        db.execute(
            "DELETE FROM artifact_registrations "
            "WHERE artifact_id=? AND parent_thread_id='thread-a'",
            (artifact["id"],),
        )

        reclaimed = _register(db, "org/repo#ownerless", "thread-c")

        assert reclaimed["parent_thread_id"] == "thread-c"
        assert models.resolve(
            "github", "pull-request", "org/repo#ownerless", db=db
        )["parent_thread_id"] == "thread-c"
    finally:
        db.close()


def test_equal_timestamp_dispositions_follow_append_order(
    isolated_db, monkeypatch
):
    from types import SimpleNamespace
    from infra.database import get_db
    from registry import ownership, registration_events

    db = get_db()
    try:
        artifact = _register(db, "org/repo#rehome-ownerless", "thread-a")
        frozen = "2026-06-21T00:00:00+00:00"
        monkeypatch.setattr(ownership, "_now", lambda: frozen)
        ids = iter([
            "z-release-001",
            "y-rehome-003",
            "e-registered-6",
            "a-release-007",
        ])
        monkeypatch.setattr(
            registration_events, "uuid", SimpleNamespace(uuid4=lambda: next(ids)),
        )
        models.release_artifact(
            integration="github",
            type="pull-request",
            external_id=artifact["external_id"],
            actor_thread_id="thread-a",
            authority="current-owner-handoff",
            reason="owner released",
            source_path="test",
            db=db,
        )
        models.rehome_artifact(
            integration="github",
            type="pull-request",
            external_id=artifact["external_id"],
            new_owner_thread_id="thread-b",
            actor_thread_id="thread-a",
            authority="current-owner-handoff",
            reason="owner restored",
            source_path="test",
            db=db,
        )
        db.execute(
            "DELETE FROM artifact_registrations "
            "WHERE artifact_id=? AND parent_thread_id='thread-b'",
            (artifact["id"],),
        )

        reclaimed = _register(db, "org/repo#rehome-ownerless", "thread-c")

        assert reclaimed["parent_thread_id"] == "thread-c"
        models.release_artifact(
            integration="github",
            type="pull-request",
            external_id=artifact["external_id"],
            actor_thread_id="thread-c",
            authority="current-owner-handoff",
            reason="owner released again",
            source_path="test",
            db=db,
        )

        assert models.latest_released_owner_thread_id(
            artifact["id"], db=db
        ) == "thread-c"
        archived = models.get_artifact(artifact["id"], db=db)
        assert (archived["parent_thread_id"], archived["state"]) == (
            "thread-c",
            "archived",
        )
        assert [
            row["id"]
            for row in models.list_for_thread(
                "thread-c", include_archived=True, db=db
            )
        ] == [artifact["id"]]
        assert models.list_for_thread(
            "thread-a", include_archived=True, db=db
        ) == []
    finally:
        db.close()


def test_released_artifact_remains_in_former_owner_history(isolated_db):
    from infra.database import get_db

    db = get_db()
    try:
        artifact = _register(db, "org/repo#90", "thread-a")
        models.release_artifact(
            integration="github",
            type="pull-request",
            external_id="org/repo#90",
            actor_thread_id="thread-a",
            authority="current-owner-handoff",
            reason="owner released",
            source_path="test",
            db=db,
        )

        assert models.list_for_thread("thread-a", db=db) == []
        history = models.list_for_thread(
            "thread-a", include_archived=True, db=db
        )
        assert [(row["id"], row["state"]) for row in history] == [
            (artifact["id"], "archived")
        ]
    finally:
        db.close()


def test_release_to_former_owner_triage_keeps_public_history(isolated_db):
    from infra.database import get_db

    db = get_db()
    try:
        artifact = _register(db, "org/repo#triage-history", "thread-a")
        models.release_artifact(
            integration="github",
            type="pull-request",
            external_id=artifact["external_id"],
            actor_thread_id="thread-a",
            authority="current-owner-handoff",
            reason="owner released to its triage queue",
            source_path="test",
            triage_thread_id="thread-a",
            db=db,
        )

        routing = models.routing_registrations(
            "github", "pull-request", artifact["external_id"], db=db
        )
        assert routing["owner"] is None
        assert routing["triage"]["parent_thread_id"] == "thread-a"
        assert routing["triage"]["relationship_state"] == "unassigned-triage"

        public = models.get_artifact(artifact["id"], db=db)
        assert (public["parent_thread_id"], public["state"]) == (
            "thread-a",
            "archived",
        )
        history = models.list_for_thread(
            "thread-a", include_archived=True, db=db
        )
        assert [(row["id"], row["state"]) for row in history] == [
            (artifact["id"], "archived")
        ]
    finally:
        db.close()


def test_rehome_clears_prior_triage_projection(isolated_db):
    from infra.database import get_db

    db = get_db()
    try:
        _register(db, "org/repo#89", "thread-a")
        models.release_artifact(
            integration="github",
            type="pull-request",
            external_id="org/repo#89",
            actor_thread_id="thread-a",
            authority="current-owner-handoff",
            reason="owner released",
            source_path="test",
            triage_thread_id="thread-triage",
            db=db,
        )
        models.rehome_artifact(
            integration="github",
            type="pull-request",
            external_id="org/repo#89",
            new_owner_thread_id="thread-b",
            actor_thread_id="thread-a",
            authority="current-owner-handoff",
            reason="rescue",
            source_path="test",
            db=db,
        )

        routing = models.routing_registrations(
            "github", "pull-request", "org/repo#89", db=db
        )
        assert routing["owner"]["parent_thread_id"] == "thread-b"
        assert routing["triage"] is None
    finally:
        db.close()
