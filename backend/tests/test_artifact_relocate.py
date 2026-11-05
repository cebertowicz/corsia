"""Artifact relocation: identity-preserving local moves, cross-device
rejection, and deterministic interruption recovery.

A raw filesystem move is recoverable but not identity-preserving;
``relocate_artifact`` instead moves the file and updates the SAME identity's
path/external key via a durable pending intent, since the rename and the
registry commit are not one transaction. Targets must be inside a registrable
zone, so these tests need a real directory under the current workspace rather
than a bare ``tmp_path`` (see ``workspace_zone``).
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from infra.database import get_db
from registry import models
from registry.relocate import (
    RelocationError,
    recover_interrupted_relocations,
    relocate_artifact,
)
from tests.registry_helpers import make_session


@pytest.fixture
def workspace_zone() -> Path:
    """A private directory inside the current workspace — a registrable zone.

    A fresh subdirectory per test, so relocations never meet another test's
    files. These are unit tests with no app or TestClient, so they need no
    ``integration_workspace``.
    """
    from infra.config import WORKSPACE

    zone = WORKSPACE / f"relocate-zone-{uuid.uuid4().hex[:8]}"
    zone.mkdir(parents=True, exist_ok=True)
    return zone


def _mk_session(db) -> str:
    return make_session(db, prefix="relocate")


def _register_file(db, sid: str, path, *, content: str = "content") -> dict:
    path.write_text(content)
    return models.register_artifact(
        integration="workspace", type="document", external_id=str(path),
        parent_thread_id=sid, title=path.name, path=str(path), db=db,
    )


def _paths(workspace_zone):
    tag = uuid.uuid4().hex[:8]
    return (
        workspace_zone / f"reloc-src-{tag}.md",
        workspace_zone / f"reloc-dst-{tag}.md",
    )


class TestRelocateSuccess:
    def test_moves_file_and_preserves_identity(self, workspace_zone):
        db = get_db()
        sid = _mk_session(db)
        source, target = _paths(workspace_zone)
        art = _register_file(db, sid, source)

        result = relocate_artifact(art["id"], str(target), actor_thread_id=sid, db=db)

        assert result["id"] == art["id"]
        assert result["path"] == str(target.resolve())
        assert not source.exists()
        assert target.read_text() == "content"

    def test_updates_external_id_without_creating_a_duplicate_identity(
        self, workspace_zone,
    ):
        db = get_db()
        sid = _mk_session(db)
        source, target = _paths(workspace_zone)
        art = _register_file(db, sid, source)

        relocate_artifact(art["id"], str(target), actor_thread_id=sid, db=db)

        count = db.execute(
            "SELECT COUNT(*) c FROM artifact_identities WHERE id=?", (art["id"],),
        ).fetchone()
        assert count["c"] == 1
        row = db.execute(
            "SELECT external_id, path FROM artifact_identities WHERE id=?",
            (art["id"],),
        ).fetchone()
        # workspace_zone is inside the "workspace" zone, so the external_id
        # is the relocatable workspace:// reference, not the raw path.
        assert row["external_id"].startswith("workspace://")
        assert row["external_id"].endswith(target.name)
        assert row["path"] == str(target.resolve())

    def test_intent_row_is_cleaned_up_after_success(self, workspace_zone):
        db = get_db()
        sid = _mk_session(db)
        source, target = _paths(workspace_zone)
        art = _register_file(db, sid, source)

        relocate_artifact(art["id"], str(target), actor_thread_id=sid, db=db)

        remaining = db.execute(
            "SELECT COUNT(*) c FROM artifact_relocation_intents WHERE artifact_id=?",
            (art["id"],),
        ).fetchone()
        assert remaining["c"] == 0


class TestRelocateValidation:
    def test_rejects_non_owner(self, workspace_zone):
        db = get_db()
        owner = _mk_session(db)
        other = _mk_session(db)
        source, target = _paths(workspace_zone)
        art = _register_file(db, owner, source)

        try:
            relocate_artifact(art["id"], str(target), actor_thread_id=other, db=db)
            assert False, "expected RelocationError"
        except RelocationError as exc:
            assert "owning thread" in str(exc)
        assert source.exists()
        assert not target.exists()

    def test_rejects_existing_target(self, workspace_zone):
        db = get_db()
        sid = _mk_session(db)
        source, target = _paths(workspace_zone)
        target.write_text("already here")
        art = _register_file(db, sid, source)

        try:
            relocate_artifact(art["id"], str(target), actor_thread_id=sid, db=db)
            assert False, "expected RelocationError"
        except RelocationError as exc:
            assert "already exists" in str(exc)
        assert source.exists()

    def test_rejects_cross_device_target(self, workspace_zone, monkeypatch):
        db = get_db()
        sid = _mk_session(db)
        source, target = _paths(workspace_zone)
        art = _register_file(db, sid, source)

        import registry.relocate as relocate_mod

        real_stat = relocate_mod.Path.stat

        class _DevOverride:
            def __init__(self, real_result, st_dev):
                self._real = real_result
                self.st_dev = st_dev

            def __getattr__(self, name):
                return getattr(self._real, name)

        def _spoofed_stat(self, *a, **kw):
            result = real_stat(self, *a, **kw)
            if self == target.parent:
                return _DevOverride(result, result.st_dev + 1)
            return result

        monkeypatch.setattr(relocate_mod.Path, "stat", _spoofed_stat)

        try:
            relocate_artifact(art["id"], str(target), actor_thread_id=sid, db=db)
            assert False, "expected RelocationError"
        except RelocationError as exc:
            assert "cross-device" in str(exc)
        assert source.exists()
        assert not target.exists()

    def test_rejects_missing_source_file(self, workspace_zone):
        db = get_db()
        sid = _mk_session(db)
        source, target = _paths(workspace_zone)
        art = models.register_artifact(
            integration="workspace", type="document", external_id=str(source),
            parent_thread_id=sid, title="gone.md", path=str(source), db=db,
        )

        try:
            relocate_artifact(art["id"], str(target), actor_thread_id=sid, db=db)
            assert False, "expected RelocationError"
        except RelocationError as exc:
            assert "does not exist" in str(exc)


class TestRelocateInterruptionRecovery:
    def test_recovers_when_rename_succeeded_but_identity_update_did_not(
        self, workspace_zone,
    ):
        """Simulates a crash after os.rename but before the identity row
        update — recovery must forward-complete, never re-run the rename
        (source no longer exists)."""
        db = get_db()
        sid = _mk_session(db)
        source, target = _paths(workspace_zone)
        art = _register_file(db, sid, source)

        import os
        import uuid as uuid_mod

        from registry.registration_events import _now

        target_external_id = str(target.resolve())
        intent_id = str(uuid_mod.uuid4())[:12]
        now = _now()
        db.execute(
            "INSERT INTO artifact_relocation_intents "
            "(id, artifact_id, actor_thread_id, source_path, target_path, "
            " source_external_id, target_external_id, phase, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,'file_moved',?,?)",
            (
                intent_id, art["id"], sid, str(source), str(target.resolve()),
                art["external_id"], target_external_id, now, now,
            ),
        )
        os.rename(str(source), str(target))
        db.commit()

        recovered = recover_interrupted_relocations(db=db)

        assert intent_id in recovered
        row = db.execute(
            "SELECT external_id, path FROM artifact_identities WHERE id=?",
            (art["id"],),
        ).fetchone()
        assert row["path"] == str(target.resolve())
        assert row["external_id"] == target_external_id
        remaining = db.execute(
            "SELECT COUNT(*) c FROM artifact_relocation_intents WHERE id=?",
            (intent_id,),
        ).fetchone()
        assert remaining["c"] == 0

    def test_recovers_when_crash_happened_before_rename(self, workspace_zone):
        """A 'recorded' intent whose rename never actually happened yet must
        complete forward normally (source still present)."""
        db = get_db()
        sid = _mk_session(db)
        source, target = _paths(workspace_zone)
        art = _register_file(db, sid, source)

        import uuid as uuid_mod

        from registry.registration_events import _now

        target_external_id = str(target.resolve())
        intent_id = str(uuid_mod.uuid4())[:12]
        now = _now()
        db.execute(
            "INSERT INTO artifact_relocation_intents "
            "(id, artifact_id, actor_thread_id, source_path, target_path, "
            " source_external_id, target_external_id, phase, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,'recorded',?,?)",
            (
                intent_id, art["id"], sid, str(source), str(target.resolve()),
                art["external_id"], target_external_id, now, now,
            ),
        )
        db.commit()

        recovered = recover_interrupted_relocations(db=db)

        assert intent_id in recovered
        assert not source.exists()
        assert target.exists()
        row = db.execute(
            "SELECT path FROM artifact_identities WHERE id=?", (art["id"],),
        ).fetchone()
        assert row["path"] == str(target.resolve())

    def test_recovery_is_a_no_op_when_nothing_is_pending(self):
        db = get_db()
        assert recover_interrupted_relocations(db=db) == []

    def test_unresolvable_intent_is_left_in_place_without_blocking_the_pass(
        self, workspace_zone,
    ):
        """An ambiguous intent must not abort recovery for the others, and must
        not be able to keep the backend from starting."""
        db = get_db()
        sid = _mk_session(db)
        stuck_source, stuck_target = _paths(workspace_zone)
        stuck = _register_file(db, sid, stuck_source)
        stuck_target.write_text("also here")  # both present: ambiguous
        _seed_intent(db, stuck, sid, stuck_source, stuck_target, phase="recorded")

        good_source, good_target = _paths(workspace_zone)
        good = _register_file(db, sid, good_source)
        good_intent = _seed_intent(
            db, good, sid, good_source, good_target, phase="recorded",
        )

        recovered = recover_interrupted_relocations(db=db)

        assert good_intent in recovered
        assert good_target.exists()
        remaining = db.execute(
            "SELECT COUNT(*) c FROM artifact_relocation_intents WHERE artifact_id=?",
            (stuck["id"],),
        ).fetchone()
        assert remaining["c"] == 1
        # Deliberately left behind by the code under test — drop it here so
        # this test leaves no pending intent for any later one to trip on.
        db.execute(
            "DELETE FROM artifact_relocation_intents WHERE artifact_id=?",
            (stuck["id"],),
        )
        db.commit()


class TestRelocationRecoveryIsWiredIntoStartup:
    def test_lifespan_calls_recovery_next_to_its_named_precedent(self):
        """``recover_interrupted_relocations`` has to actually run in
        production, next to its named precedent ``recover_write_intents``."""
        import inspect

        import app_lifecycle

        body = inspect.getsource(app_lifecycle.app_startup_init.initialize_startup)
        assert "recover_interrupted_relocations" in body
        assert body.index("recover_write_intents") < body.index(
            "recover_interrupted_relocations"
        )

    async def test_startup_advances_a_stranded_recorded_intent(
        self, workspace_zone, monkeypatch,
    ):
        db = get_db()
        sid = _mk_session(db)
        source, target = _paths(workspace_zone)
        art = _register_file(db, sid, source)
        intent_id = _seed_intent(db, art, sid, source, target, phase="recorded")

        await _run_lifespan_recovery(monkeypatch)

        assert not source.exists()
        assert target.exists()
        row = db.execute(
            "SELECT path FROM artifact_identities WHERE id=?", (art["id"],),
        ).fetchone()
        assert row["path"] == str(target.resolve())
        remaining = db.execute(
            "SELECT COUNT(*) c FROM artifact_relocation_intents WHERE id=?",
            (intent_id,),
        ).fetchone()
        assert remaining["c"] == 0


class _StopStartup(Exception):
    """Sentinel: stop lifespan once its recovery steps have run."""


async def _run_lifespan_recovery(monkeypatch) -> None:
    """Drive real ``lifespan`` through its recovery steps and stop there.

    The stop is anchored on the step that *follows* recovery, so it is bounded
    whether or not relocation recovery is wired in — the rest of startup boots
    provider servers and background loops this test has no business starting.
    """
    import app_lifecycle
    from registry import durable_writes

    def _stop(*_args, **_kwargs):
        raise _StopStartup()

    monkeypatch.setattr(app_lifecycle.app_startup_init, "init_db", lambda: None)
    monkeypatch.setattr(durable_writes, "recover_write_intents", lambda **kw: None)
    monkeypatch.setattr("app_startup_workspace.initialize_workspace", _stop)
    try:
        async with app_lifecycle.lifespan(None):
            pass
    except _StopStartup:
        pass


def _seed_intent(db, art, sid, source, target, *, phase: str) -> str:
    """Insert one pending relocation intent the way a crash would leave it."""
    from registry.registration_events import _now

    intent_id = str(uuid.uuid4())[:12]
    now = _now()
    db.execute(
        "INSERT INTO artifact_relocation_intents "
        "(id, artifact_id, actor_thread_id, source_path, target_path, "
        " source_external_id, target_external_id, target_type, target_title, "
        " phase, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            intent_id, art["id"], sid, str(source), str(target.resolve()),
            art["external_id"], str(target.resolve()), "document", target.name,
            phase, now, now,
        ),
    )
    db.commit()
    return intent_id


class TestRelocateDoesNotForkIdentity:
    """A move that changes the derived type must land the new type too.

    Otherwise the identity keeps a stale ``type``, ``find_file_identity`` on the
    new path resolves nothing, and the next observer registration mints a
    SECOND identity sharing one external id.
    """

    def test_type_changing_move_updates_the_single_identity(
        self, workspace_zone,
    ):
        from registry import service

        db = get_db()
        sid = _mk_session(db)
        tag = uuid.uuid4().hex[:8]
        source = workspace_zone / f"notes-{tag}.md"
        target = workspace_zone / f"notes-{tag}.py"
        art = _register_file(db, sid, source)
        assert art["type"] == "document"

        relocate_artifact(art["id"], str(target), actor_thread_id=sid, db=db)

        row = db.execute(
            "SELECT type, title, path FROM artifact_identities WHERE id=?",
            (art["id"],),
        ).fetchone()
        assert row["type"] == "code"
        assert row["title"] == target.name
        assert row["path"] == str(target.resolve())

        # Exactly one identity exists for the new location, and it resolves.
        resolved = service.find_file_identity(str(target), db=db)
        assert resolved is not None
        assert resolved["id"] == art["id"]
        same_key = db.execute(
            "SELECT COUNT(*) c FROM artifact_identities WHERE external_id=?",
            (row_external_id(db, art["id"]),),
        ).fetchone()
        assert same_key["c"] == 1

    def test_an_explicit_title_survives_a_move(self, workspace_zone):
        db = get_db()
        sid = _mk_session(db)
        source, target = _paths(workspace_zone)
        source.write_text("content")
        art = models.register_artifact(
            integration="workspace", type="document", external_id=str(source),
            parent_thread_id=sid, title="Quarterly plan", path=str(source), db=db,
        )

        relocate_artifact(art["id"], str(target), actor_thread_id=sid, db=db)

        row = db.execute(
            "SELECT title FROM artifact_identities WHERE id=?", (art["id"],),
        ).fetchone()
        assert row["title"] == "Quarterly plan"

    def test_rejects_a_target_identity_that_already_exists(
        self, workspace_zone,
    ):
        from registry import service

        db = get_db()
        sid = _mk_session(db)
        tag = uuid.uuid4().hex[:8]
        source = workspace_zone / f"src-{tag}.md"
        occupied = workspace_zone / f"dst-{tag}.md"
        art = _register_file(db, sid, source)
        # Someone registered the target identity through the production write
        # path (so it holds the same workspace:// key relocate will compute),
        # then the file was removed.
        occupied.write_text("was here")
        squatter = service.register_write_strict(sid, str(occupied), db=db)
        occupied.unlink()

        try:
            relocate_artifact(art["id"], str(occupied), actor_thread_id=sid, db=db)
            assert False, "expected RelocationError"
        except RelocationError as exc:
            assert "already registered" in str(exc)
        assert source.exists()
        assert squatter["id"] != art["id"]


class TestRelocateRespectsThreadIsolation:
    def test_rejects_a_target_inside_another_threads_tray(
        self, workspace_zone,
    ):
        """Registrability is not authority: every thread's tray is registrable,
        but only its own thread may write into it."""
        db = get_db()
        actor = _mk_session(db)
        other = _mk_session(db)
        source, _unused = _paths(workspace_zone)
        art = _register_file(db, actor, source)
        foreign = workspace_zone / ".threads" / other / "working" / "stolen.md"
        foreign.parent.mkdir(parents=True, exist_ok=True)

        try:
            relocate_artifact(art["id"], str(foreign), actor_thread_id=actor, db=db)
            assert False, "expected RelocationError"
        except RelocationError as exc:
            assert "another thread's private directory" in str(exc)
        assert source.exists()
        assert not foreign.exists()

    def test_allows_a_target_inside_the_actors_own_tray(self, workspace_zone):
        db = get_db()
        actor = _mk_session(db)
        source, _unused = _paths(workspace_zone)
        art = _register_file(db, actor, source)
        mine = workspace_zone / ".threads" / actor / "working" / "kept.md"
        mine.parent.mkdir(parents=True, exist_ok=True)

        relocate_artifact(art["id"], str(mine), actor_thread_id=actor, db=db)

        assert mine.exists()
        assert not source.exists()


class TestRelocateAuditTrail:
    def test_appends_a_relocated_registration_event(self, workspace_zone):
        import json

        db = get_db()
        sid = _mk_session(db)
        source, target = _paths(workspace_zone)
        art = _register_file(db, sid, source)

        relocate_artifact(art["id"], str(target), actor_thread_id=sid, db=db)

        event = db.execute(
            "SELECT * FROM artifact_registration_events "
            "WHERE artifact_id=? AND event_type='relocated'",
            (art["id"],),
        ).fetchone()
        assert event is not None
        assert event["actor_thread_id"] == sid
        assert event["source_path"] == str(source)
        metadata = json.loads(event["metadata"])
        assert metadata["source_path"] == str(source)
        assert metadata["target_path"] == str(target.resolve())
        assert metadata["source_external_id"] == art["external_id"]
        assert metadata["target_external_id"].endswith(target.name)


def row_external_id(db, artifact_id: str) -> str:
    return db.execute(
        "SELECT external_id FROM artifact_identities WHERE id=?", (artifact_id,),
    ).fetchone()["external_id"]


class TestRelocateOnlyHandlesWorkspaceIdentities:
    """Every step of relocation is workspace-shaped.

    ``service.classify`` only returns workspace document/code/skill and
    ``external_identity_for_path`` mints a ``workspace://`` key, so running
    them against another integration silently rewrites a good identity into an
    unregistered ``(integration, type)`` pair the pane can only render as a
    disabled "No handler is registered for this artifact type" row - with an
    external id that no longer matches the path its serving URL is built from.
    """

    def _upload(self, db, sid: str, path) -> dict:
        path.write_text("binary-ish")
        return models.register_artifact(
            integration="upload", type="upload", external_id="upload-abc123",
            parent_thread_id=sid, title=path.name, path=str(path), db=db,
        )

    def test_rejects_a_non_workspace_artifact(self, workspace_zone):
        db = get_db()
        sid = _mk_session(db)
        source, target = _paths(workspace_zone)
        art = self._upload(db, sid, source)

        with pytest.raises(RelocationError, match="only workspace artifacts"):
            relocate_artifact(art["id"], str(target), actor_thread_id=sid, db=db)

        row = db.execute(
            "SELECT integration, type, external_id, path FROM artifact_identities "
            "WHERE id=?", (art["id"],),
        ).fetchone()
        assert row["integration"] == "upload"
        assert row["type"] == "upload"
        assert row["external_id"] == "upload-abc123"
        assert row["path"] == str(source)
        assert source.exists() and not target.exists()

    def test_rejection_leaves_no_relocation_intent_behind(self, workspace_zone):
        db = get_db()
        sid = _mk_session(db)
        source, target = _paths(workspace_zone)
        art = self._upload(db, sid, source)

        with pytest.raises(RelocationError):
            relocate_artifact(art["id"], str(target), actor_thread_id=sid, db=db)

        pending = db.execute(
            "SELECT COUNT(*) c FROM artifact_relocation_intents WHERE artifact_id=?",
            (art["id"],),
        ).fetchone()
        assert pending["c"] == 0

    def test_target_collision_is_rejected_cleanly(
        self, workspace_zone,
    ):
        """A target identity already registered at the destination must be
        rejected with a clean ``RelocationError``, not a UNIQUE violation
        surfaced from the identity UPDATE.
        """
        db = get_db()
        sid = _mk_session(db)
        source, target = _paths(workspace_zone)
        art = _register_file(db, sid, source)
        occupant = _register_file(db, sid, target)

        with pytest.raises(RelocationError, match="already"):
            relocate_artifact(art["id"], str(target), actor_thread_id=sid, db=db)

        assert occupant["id"] != art["id"]
        assert db.execute(
            "SELECT path FROM artifact_identities WHERE id=?", (art["id"],),
        ).fetchone()["path"] == str(source)
