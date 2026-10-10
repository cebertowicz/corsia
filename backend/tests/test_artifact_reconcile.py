"""Artifact reconcile preview/apply: idempotency, races, and non-ENOENT safety.

Apply must never mutate lifecycle except on an authoritative ENOENT/ENOTDIR
result revalidated immediately before mutation, must produce a durable
idempotent receipt per (artifact, identity version, session), and must call
``notify_changed`` only after commit for applied identities.
"""

from __future__ import annotations

import os
import uuid

from infra.database import get_db
from registry import models, reconcile
from tests.registry_helpers import make_session


def _mk_session(db) -> str:
    return make_session(db, prefix="reconcile")


def _register_missing(db, sid: str, path, *, retention: str = "durable") -> dict:
    return models.register_artifact(
        integration="workspace", type="document",
        external_id=str(path), parent_thread_id=sid,
        title=path.name, path=str(path), retention=retention, db=db,
    )


class TestPreview:
    def test_preview_is_read_only_and_reports_correct_action_by_retention(self, tmp_path):
        db = get_db()
        sid = _mk_session(db)
        ephemeral = _register_missing(db, sid, tmp_path / "e.md", retention="ephemeral")
        durable = _register_missing(db, sid, tmp_path / "d.md", retention="durable")

        result = reconcile.preview_for_thread(sid)

        assert result["mode"] == "preview"
        assert result["dry_run"] is True
        actions = {a["artifact_id"]: a["action"] for a in result["actions"]}
        assert actions == {ephemeral["id"]: "tombstone", durable["id"]: "archive"}
        for artifact_id in (ephemeral["id"], durable["id"]):
            state = db.execute(
                "SELECT state FROM artifact_identities WHERE id=?", (artifact_id,),
            ).fetchone()
            assert state["state"] == "active"

    def test_preview_ignores_present_files(self, tmp_path):
        db = get_db()
        sid = _mk_session(db)
        present = tmp_path / "present.md"
        present.write_text("here")
        models.register_artifact(
            integration="workspace", type="document", external_id=str(present),
            parent_thread_id=sid, title="present.md", path=str(present), db=db,
        )

        result = reconcile.preview_for_thread(sid)

        assert result["actions"] == []

    def test_preview_ignores_url_identities(self):
        db = get_db()
        sid = _mk_session(db)
        models.register_artifact(
            integration="github", type="pull-request",
            external_id=f"o/r#{uuid.uuid4().hex[:4]}", parent_thread_id=sid,
            path="https://github.com/o/r/pull/1", db=db,
        )

        result = reconcile.preview_for_thread(sid)

        assert result["actions"] == []


class TestApply:
    def test_apply_tombstones_ephemeral_and_archives_durable(self, tmp_path):
        db = get_db()
        sid = _mk_session(db)
        ephemeral = _register_missing(db, sid, tmp_path / "e.md", retention="ephemeral")
        durable = _register_missing(db, sid, tmp_path / "d.md", retention="durable")

        result = reconcile.apply_for_thread(sid)

        assert result["mode"] == "apply"
        assert result["counts"]["applied"] == 2
        states = {
            row["id"]: row["state"]
            for row in db.execute(
                "SELECT id, state FROM artifact_identities WHERE id IN (?,?)",
                (ephemeral["id"], durable["id"]),
            )
        }
        assert states == {ephemeral["id"]: "tombstone", durable["id"]: "archived"}

    def test_apply_never_touches_present_files(self, tmp_path):
        db = get_db()
        sid = _mk_session(db)
        present = tmp_path / "present.md"
        present.write_text("here")
        art = models.register_artifact(
            integration="workspace", type="document", external_id=str(present),
            parent_thread_id=sid, title="present.md", path=str(present), db=db,
        )

        result = reconcile.apply_for_thread(sid)

        assert result["counts"]["applied"] == 0
        assert present.exists()  # never deletes a file
        state = db.execute(
            "SELECT state FROM artifact_identities WHERE id=?", (art["id"],),
        ).fetchone()
        assert state["state"] == "active"

    def test_apply_writes_a_durable_receipt(self, tmp_path):
        db = get_db()
        sid = _mk_session(db)
        art = _register_missing(db, sid, tmp_path / "e.md", retention="ephemeral")

        reconcile.apply_for_thread(sid)

        receipt = db.execute(
            "SELECT action, status, thread_id FROM artifact_reconcile_receipts "
            "WHERE artifact_id=?",
            (art["id"],),
        ).fetchone()
        assert dict(receipt) == {
            "action": "tombstone", "status": "applied", "thread_id": sid,
        }

    def test_apply_is_idempotent_on_replay_after_success(self, tmp_path):
        db = get_db()
        sid = _mk_session(db)
        art = _register_missing(db, sid, tmp_path / "e.md", retention="ephemeral")

        first = reconcile.apply_for_thread(sid)
        second = reconcile.apply_for_thread(sid)

        assert first["counts"]["applied"] == 1
        # Already tombstoned -> no longer a candidate -> nothing to re-apply.
        assert second["counts"]["applied"] == 0
        receipts = db.execute(
            "SELECT COUNT(*) c FROM artifact_reconcile_receipts WHERE artifact_id=?",
            (art["id"],),
        ).fetchone()
        assert receipts["c"] == 1

    def test_apply_replay_against_existing_receipt_does_not_remutate(self, tmp_path):
        """Directly exercises the receipt-lookup short-circuit: a pre-existing
        applied receipt for the current identity version is honored without
        touching artifact_identities again."""
        db = get_db()
        sid = _mk_session(db)
        art = _register_missing(db, sid, tmp_path / "e.md", retention="ephemeral")
        identity = db.execute(
            "SELECT updated_at FROM artifact_identities WHERE id=?", (art["id"],),
        ).fetchone()

        reconcile._write_receipt(
            db, artifact_id=art["id"], identity_version=identity["updated_at"],
            thread_id=sid, action="tombstone", status="applied", reason=None,
        )

        result = reconcile.apply_for_thread(sid)

        # The pre-seeded receipt is reported as this pass's evidence, but the
        # summary counts only work this pass actually did — see
        # TestApplyCountsReportOnlyThisPassesWork.
        assert result["counts"]["applied"] == 0
        assert result["receipts"][0]["id"] == db.execute(
            "SELECT id FROM artifact_reconcile_receipts WHERE artifact_id=?",
            (art["id"],),
        ).fetchone()["id"]
        count = db.execute(
            "SELECT COUNT(*) c FROM artifact_reconcile_receipts WHERE artifact_id=?",
            (art["id"],),
        ).fetchone()
        assert count["c"] == 1
        # Apply's candidate scan still requires state='active'; since we never
        # actually retired the identity, apply would be free to retire it —
        # but the receipt short-circuit fires first because it is keyed on
        # this exact identity_version, proving idempotent-replay safety: the
        # short-circuited call never reaches the mutation branch at all.
        state = db.execute(
            "SELECT state FROM artifact_identities WHERE id=?", (art["id"],),
        ).fetchone()
        assert state["state"] == "active"

    def test_apply_skips_when_identity_changes_between_scan_and_mutation(
        self, tmp_path, monkeypatch,
    ):
        """Simulates the exact race the design calls out: revalidate path and
        identity version immediately before mutation; skip with receipt on
        mismatch."""
        db = get_db()
        sid = _mk_session(db)
        art = _register_missing(db, sid, tmp_path / "e.md", retention="ephemeral")

        real_candidates = reconcile._owner_scoped_candidates

        def _racing_candidates(db_, thread_id):
            rows = real_candidates(db_, thread_id)
            # A concurrent writer touches the identity after the scan.
            db_.execute(
                "UPDATE artifact_identities SET updated_at='raced' WHERE id=?",
                (art["id"],),
            )
            db_.commit()
            return rows

        monkeypatch.setattr(reconcile, "_owner_scoped_candidates", _racing_candidates)

        result = reconcile.apply_for_thread(sid)

        assert result["counts"]["applied"] == 0
        assert result["counts"]["skipped"] == 1
        receipt = db.execute(
            "SELECT status, reason FROM artifact_reconcile_receipts WHERE artifact_id=?",
            (art["id"],),
        ).fetchone()
        assert receipt["status"] == "skipped"
        assert "changed" in receipt["reason"]
        state = db.execute(
            "SELECT state FROM artifact_identities WHERE id=?", (art["id"],),
        ).fetchone()
        assert state["state"] == "active"

    def test_apply_treats_recreated_file_as_skip_not_apply(self, tmp_path):
        """File reappears between the initial screen and the revalidation
        check — must skip, never mutate."""
        db = get_db()
        sid = _mk_session(db)
        target = tmp_path / "reappears.md"
        art = _register_missing(db, sid, target, retention="ephemeral")
        # Recreate the file before apply runs at all — apply's own first
        # authoritative check already sees it present.
        target.write_text("back")

        result = reconcile.apply_for_thread(sid)

        assert result["counts"]["applied"] == 0
        state = db.execute(
            "SELECT state FROM artifact_identities WHERE id=?", (art["id"],),
        ).fetchone()
        assert state["state"] == "active"

    def test_apply_non_enoent_io_error_never_mutates_and_is_reported_failed(
        self, tmp_path, monkeypatch,
    ):
        db = get_db()
        sid = _mk_session(db)
        art = _register_missing(db, sid, tmp_path / "denied.md", retention="ephemeral")

        real_stat = os.stat

        def _denying_stat(path, *a, **kw):
            if str(path) == art["path"]:
                raise PermissionError(f"denied: {path}")
            return real_stat(path, *a, **kw)

        monkeypatch.setattr(reconcile.os, "stat", _denying_stat)

        result = reconcile.apply_for_thread(sid)

        assert result["counts"]["applied"] == 0
        assert result["counts"]["failed"] == 1
        receipt = db.execute(
            "SELECT status, reason FROM artifact_reconcile_receipts WHERE artifact_id=?",
            (art["id"],),
        ).fetchone()
        assert receipt["status"] == "failed"
        assert "PermissionError" in receipt["reason"]
        state = db.execute(
            "SELECT state FROM artifact_identities WHERE id=?", (art["id"],),
        ).fetchone()
        assert state["state"] == "active"

    def test_apply_survives_one_candidates_unexpected_failure(
        self, tmp_path, monkeypatch,
    ):
        """One bad row must not abort repair for the rest of the thread."""
        db = get_db()
        sid = _mk_session(db)
        first = _register_missing(db, sid, tmp_path / "aaa.md", retention="ephemeral")
        second = _register_missing(db, sid, tmp_path / "bbb.md", retention="ephemeral")

        real_retire = reconcile.models.retire

        def _exploding_retire(artifact_id, **kwargs):
            if artifact_id == first["id"]:
                raise RuntimeError("boom")
            return real_retire(artifact_id, **kwargs)

        monkeypatch.setattr(reconcile.models, "retire", _exploding_retire)

        result = reconcile.apply_for_thread(sid)

        assert result["counts"]["applied"] == 1
        assert result["counts"]["failed"] == 1
        states = {
            row["id"]: row["state"]
            for row in db.execute(
                "SELECT id, state FROM artifact_identities WHERE id IN (?,?)",
                (first["id"], second["id"]),
            )
        }
        assert states == {first["id"]: "active", second["id"]: "tombstone"}
        receipt = db.execute(
            "SELECT status, reason FROM artifact_reconcile_receipts WHERE artifact_id=?",
            (first["id"],),
        ).fetchone()
        assert receipt["status"] == "failed"
        assert "RuntimeError" in receipt["reason"]

    def test_apply_is_owner_scoped_subscriber_cannot_repair_owners_artifact(
        self, tmp_path,
    ):
        db = get_db()
        owner = _mk_session(db)
        subscriber = _mk_session(db)
        ghost = tmp_path / "ghost.md"
        art = models.register_artifact(
            integration="workspace", type="document", external_id=str(ghost),
            parent_thread_id=owner, title="ghost.md", path=str(ghost), db=db,
        )
        models.register_artifact(
            integration="workspace", type="document", external_id=str(ghost),
            parent_thread_id=subscriber, title="ghost.md", db=db,
        )

        subscriber_result = reconcile.apply_for_thread(subscriber)
        assert subscriber_result["counts"]["applied"] == 0
        state = db.execute(
            "SELECT state FROM artifact_identities WHERE id=?", (art["id"],),
        ).fetchone()
        assert state["state"] == "active"

        owner_result = reconcile.apply_for_thread(owner)
        assert owner_result["counts"]["applied"] == 1

    def test_apply_notifies_only_after_commit_for_applied_identities(
        self, tmp_path, monkeypatch,
    ):
        db = get_db()
        sid = _mk_session(db)
        applied_art = _register_missing(db, sid, tmp_path / "e.md", retention="ephemeral")
        present = tmp_path / "present.md"
        present.write_text("here")
        models.register_artifact(
            integration="workspace", type="document", external_id=str(present),
            parent_thread_id=sid, title="present.md", path=str(present), db=db,
        )

        notified: list[dict] = []

        def _capture_notify(identity):
            # By the time notify fires, the mutation must already be committed.
            row = db.execute(
                "SELECT state FROM artifact_identities WHERE id=?",
                (applied_art["id"],),
            ).fetchone()
            assert row["state"] == "tombstone"
            notified.append(identity)

        monkeypatch.setattr(reconcile, "notify_changed", _capture_notify)

        reconcile.apply_for_thread(sid)

        assert len(notified) == 1
        assert notified[0]["external_id"] == applied_art["external_id"]


class TestUnresolvedReceiptNeverBlocksRepair:
    """``identity_version`` is ``artifact_identities.updated_at``, which does
    NOT change when a backing file disappears. So a non-applied receipt at the
    current version must never behave as an idempotency barrier: if it did, one
    transient error or one lost race would strand that artifact forever.
    """

    def test_transient_io_failure_then_genuine_delete_repairs_on_retry(
        self, tmp_path, monkeypatch,
    ):
        db = get_db()
        sid = _mk_session(db)
        art = _register_missing(db, sid, tmp_path / "flaky.md", retention="durable")
        version = db.execute(
            "SELECT updated_at FROM artifact_identities WHERE id=?", (art["id"],),
        ).fetchone()["updated_at"]

        real_stat = os.stat

        def _denying_stat(path, *a, **kw):
            if str(path) == art["path"]:
                raise PermissionError(f"denied: {path}")
            return real_stat(path, *a, **kw)

        monkeypatch.setattr(reconcile.os, "stat", _denying_stat)
        first = reconcile.apply_for_thread(sid)
        assert first["counts"]["failed"] == 1

        # The EACCES clears; the file really is gone. Nothing touched the
        # identity, so identity_version is still exactly what the failed
        # receipt recorded.
        monkeypatch.setattr(reconcile.os, "stat", real_stat)
        unchanged = db.execute(
            "SELECT updated_at FROM artifact_identities WHERE id=?", (art["id"],),
        ).fetchone()["updated_at"]
        assert unchanged == version

        second = reconcile.apply_for_thread(sid)

        assert second["counts"]["applied"] == 1
        state = db.execute(
            "SELECT state FROM artifact_identities WHERE id=?", (art["id"],),
        ).fetchone()
        assert state["state"] == "archived"

    def test_lost_path_recreation_race_then_genuine_delete_repairs_on_retry(
        self, tmp_path, monkeypatch,
    ):
        db = get_db()
        sid = _mk_session(db)
        art = _register_missing(db, sid, tmp_path / "recreated.md", retention="durable")

        real_stat = os.stat
        seen = {"n": 0}

        def _racing_stat(path, *a, **kw):
            """Gone at the screening check, back by the revalidation check."""
            if str(path) == art["path"]:
                seen["n"] += 1
                if seen["n"] == 1:
                    raise FileNotFoundError(path)
                return real_stat(str(tmp_path), *a, **kw)
            return real_stat(path, *a, **kw)

        monkeypatch.setattr(reconcile.os, "stat", _racing_stat)
        first = reconcile.apply_for_thread(sid)

        assert first["counts"]["applied"] == 0
        assert first["counts"]["skipped"] == 1
        # The skip is recorded, not silently swallowed.
        receipt = db.execute(
            "SELECT status, reason FROM artifact_reconcile_receipts WHERE artifact_id=?",
            (art["id"],),
        ).fetchone()
        assert receipt["status"] == "skipped"
        assert receipt["reason"] == "path recreated"

        monkeypatch.setattr(reconcile.os, "stat", real_stat)
        second = reconcile.apply_for_thread(sid)

        assert second["counts"]["applied"] == 1
        state = db.execute(
            "SELECT state FROM artifact_identities WHERE id=?", (art["id"],),
        ).fetchone()
        assert state["state"] == "archived"

    def test_applied_receipt_is_terminal_and_never_overwritten(self, tmp_path):
        db = get_db()
        sid = _mk_session(db)
        art = _register_missing(db, sid, tmp_path / "done.md", retention="durable")
        version = db.execute(
            "SELECT updated_at FROM artifact_identities WHERE id=?", (art["id"],),
        ).fetchone()["updated_at"]

        reconcile.apply_for_thread(sid)
        reconcile._write_receipt(
            db, artifact_id=art["id"], identity_version=version, thread_id=sid,
            action="skip", status="failed", reason="a later attempt must not win",
        )

        rows = db.execute(
            "SELECT status, reason FROM artifact_reconcile_receipts WHERE artifact_id=?",
            (art["id"],),
        ).fetchall()
        assert [(r["status"], r["reason"]) for r in rows] == [("applied", None)]


class TestHealthyCandidateClearsStaleAttention:
    """The other half of the receipt contract: an unresolved receipt must be
    cleared once its cause is gone.

    ``_write_receipt`` only replaces an unresolved receipt on a *later*
    attempt that has something to record. The likeliest outcome of a transient
    EACCES/EIO — a file that was never actually missing — produces no such
    attempt, so nothing ever retired the ``failed`` receipt. Since
    ``identity_version`` is ``updated_at`` and nothing on that path changes
    it, the pane matched it forever and Retry could not clear it.
    """

    def _present(self, db, sid: str, path):
        path.write_text("here")
        return models.register_artifact(
            integration="workspace", type="document", external_id=str(path),
            parent_thread_id=sid, title=path.name, path=str(path), db=db,
        )

    def _receipts(self, db, artifact_id: str) -> int:
        return db.execute(
            "SELECT COUNT(*) c FROM artifact_reconcile_receipts WHERE artifact_id=?",
            (artifact_id,),
        ).fetchone()["c"]

    def test_transient_io_error_on_a_present_file_clears_on_the_next_pass(
        self, tmp_path, monkeypatch,
    ):
        from registry import view

        db = get_db()
        sid = _mk_session(db)
        art = self._present(db, sid, tmp_path / "present.md")

        real_stat = os.stat

        def _denying_stat(path, *a, **kw):
            if str(path) == art["path"]:
                raise PermissionError(f"denied: {path}")
            return real_stat(path, *a, **kw)

        monkeypatch.setattr(reconcile.os, "stat", _denying_stat)
        first = reconcile.apply_for_thread(sid)
        assert first["counts"]["failed"] == 1
        assert [
            i["artifact_id"] for i in view.pane_for_thread(sid)["needs_attention"]
        ] == [art["id"]]

        # The EACCES clears. The file was never actually missing, so nothing
        # else will ever record an outcome against this identity version.
        monkeypatch.setattr(reconcile.os, "stat", real_stat)
        second = reconcile.apply_for_thread(sid)

        assert view.pane_for_thread(sid)["needs_attention"] == []
        assert second["counts"] == {"applied": 0, "skipped": 0, "failed": 0}
        assert self._receipts(db, art["id"]) == 0
        assert db.execute(
            "SELECT state FROM artifact_identities WHERE id=?", (art["id"],),
        ).fetchone()["state"] == "active"

    def test_clearing_never_removes_a_terminal_applied_receipt(self, tmp_path):
        """A repaired artifact whose path is later recreated keeps its audit
        trail: ``applied`` is terminal in both directions."""
        db = get_db()
        sid = _mk_session(db)
        path = tmp_path / "gone.md"
        art = _register_missing(db, sid, path, retention="durable")

        reconcile.apply_for_thread(sid)
        assert self._receipts(db, art["id"]) == 1

        # The path comes back and the identity is restored to the pane.
        path.write_text("recreated")
        db.execute(
            "UPDATE artifact_identities SET state='active' WHERE id=?", (art["id"],),
        )
        db.commit()

        reconcile.apply_for_thread(sid)

        rows = db.execute(
            "SELECT status FROM artifact_reconcile_receipts WHERE artifact_id=?",
            (art["id"],),
        ).fetchall()
        assert [r["status"] for r in rows] == ["applied"]

    def test_a_healthy_pass_does_not_clear_another_sessions_receipt(
        self, tmp_path,
    ):
        db = get_db()
        owner = _mk_session(db)
        other = _mk_session(db)
        art = self._present(db, owner, tmp_path / "shared.md")
        version = db.execute(
            "SELECT updated_at FROM artifact_identities WHERE id=?", (art["id"],),
        ).fetchone()["updated_at"]
        reconcile._write_receipt(
            db, artifact_id=art["id"], identity_version=version, thread_id=other,
            action="skip", status="failed", reason="another thread's attempt",
        )

        reconcile.apply_for_thread(owner)

        rows = db.execute(
            "SELECT thread_id FROM artifact_reconcile_receipts WHERE artifact_id=?",
            (art["id"],),
        ).fetchall()
        assert [r["thread_id"] for r in rows] == [other]


class TestApplyCountsReportOnlyThisPassesWork:
    """``counts.applied`` is work this pass did, not rows in an applied state.

    The barrier-replay case is covered by
    ``TestApply.test_apply_replay_against_existing_receipt_does_not_remutate``,
    which asserts the count directly. This adds the pass that repairs nothing
    at all.
    """

    def test_a_pass_with_nothing_to_repair_reports_zero(self, tmp_path):
        db = get_db()
        sid = _mk_session(db)
        present = tmp_path / "present.md"
        present.write_text("here")
        models.register_artifact(
            integration="workspace", type="document", external_id=str(present),
            parent_thread_id=sid, title=present.name, path=str(present), db=db,
        )

        result = reconcile.apply_for_thread(sid)

        assert result["counts"] == {"applied": 0, "skipped": 0, "failed": 0}
        assert result["receipts"] == []
