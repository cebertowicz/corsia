"""Unit tests for the sanctioned self-restart authorization gate and control helpers.

Authorization is a single operator-set flag (fail closed) — no dev-vs-prod or
topology inference. The control helpers provide the durable audit trail and the
in-flight concurrency guard.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

_FLAG = "CORSIA_AGENT_RESTART_ALLOWED"


class TestRestartAuthorization:
    def test_flag_enabled_authorizes(self, monkeypatch):
        from infra.restart_authorization import RestartAuthorization, authorize_self_restart

        monkeypatch.setenv(_FLAG, "1")
        auth = authorize_self_restart()
        assert isinstance(auth, RestartAuthorization)
        assert auth.head_sha  # this worktree is a real git repo
        assert auth.short_sha

    def test_various_truthy_values_authorize(self, monkeypatch):
        from infra.restart_authorization import RestartAuthorization, authorize_self_restart

        for value in ("1", "true", "TRUE", "yes", "on", " On "):
            monkeypatch.setenv(_FLAG, value)
            assert isinstance(authorize_self_restart(), RestartAuthorization), value

    def test_unset_flag_refuses_fail_closed(self, monkeypatch):
        from infra.restart_authorization import RestartRefusal, authorize_self_restart

        monkeypatch.delenv(_FLAG, raising=False)
        refusal = authorize_self_restart()
        assert isinstance(refusal, RestartRefusal)
        assert _FLAG in refusal.reason

    def test_falsey_values_refuse_fail_closed(self, monkeypatch):
        from infra.restart_authorization import RestartRefusal, authorize_self_restart

        for value in ("0", "false", "no", "off", "", "maybe"):
            monkeypatch.setenv(_FLAG, value)
            assert isinstance(authorize_self_restart(), RestartRefusal), value

    def test_authorization_depends_only_on_the_flag(self, monkeypatch):
        # The gate must depend ONLY on the opt-in flag.
        from infra.restart_authorization import RestartAuthorization, authorize_self_restart

        monkeypatch.setenv(_FLAG, "1")
        assert isinstance(authorize_self_restart(), RestartAuthorization)


class TestInFlightGuard:
    def _insert(self, db, *, status="in_flight", requested_at=None):
        requested_at = requested_at or datetime.now(timezone.utc).isoformat()
        db.execute(
            "INSERT INTO self_restart_requests "
            "(id, requester_thread_id, status, requested_at) VALUES (?,?,?,?)",
            ("row-" + status[:3] + str(hash(requested_at) % 1000),
             "t-1", status, requested_at),
        )
        db.commit()

    def test_fresh_in_flight_request_blocks(self, isolated_db):
        from automation import self_restart as sr
        from infra.database import get_db

        db = get_db()
        try:
            self._insert(db)
        finally:
            db.close()
        assert sr.in_flight_request() is not None

    def test_stale_in_flight_request_does_not_block(self, isolated_db):
        from automation import self_restart as sr
        from infra.database import get_db

        old = (
            datetime.now(timezone.utc)
            - timedelta(seconds=sr.IN_FLIGHT_TTL_SECONDS + 60)
        ).isoformat()
        db = get_db()
        try:
            self._insert(db, requested_at=old)
        finally:
            db.close()
        assert sr.in_flight_request() is None

    def test_operational_hold_is_released_by_completion_not_by_cleanup(
        self, isolated_db
    ):
        """Only a finished continuity handoff releases a hold and stamps the SHA.

        ``clear_stale_in_flight`` deliberately cannot touch an operational hold:
        a time-based release would drop the cutoff while work was still being
        handed over, which is the exact failure the durable operation exists to
        prevent. Legacy pre-lifecycle rows are the only thing it may close.
        """
        from automation import self_restart as sr
        from infra.database import get_db

        db = get_db()
        try:
            rid = sr.try_acquire("t-1", "steve", "aaa111", "dev", "test", db=db)
            assert rid is not None
            assert sr.in_flight_request(db=db) is not None

            assert sr.clear_stale_in_flight("bbb222", db=db) == 0
            assert sr.in_flight_request(db=db) is not None

            assert sr.transition_operation(
                rid, expected="holding", target="resuming", db=db,
            )
            assert sr.complete_operation(rid, deployed_sha="bbb222", db=db)

            row = db.execute(
                "SELECT status, from_sha, deployed_sha FROM self_restart_requests "
                "WHERE id=?", (rid,)
            ).fetchone()
            assert row["status"] == "complete"
            assert row["from_sha"] == "aaa111"
            assert row["deployed_sha"] == "bbb222"
            assert sr.in_flight_request(db=db) is None
        finally:
            db.close()

    def test_try_acquire_is_single_flight(self, isolated_db):
        # The atomic acquire must reject a second claim while one is held, and
        # allow a claim once the held one is settled — the single-flight contract.
        from automation import self_restart as sr
        from infra.database import get_db

        db = get_db()
        try:
            first = sr.try_acquire("t-1", "steve", "aaa", "dev", "one", db=db)
            assert first is not None
            second = sr.try_acquire("t-2", "steve", "bbb", "dev", "two", db=db)
            assert second is None  # rejected — one already in flight

            # Cleanup must NOT hand the slot to a second restart.
            sr.clear_stale_in_flight("aaa", db=db)
            assert sr.try_acquire("t-x", "steve", "xxx", "dev", "nope", db=db) is None

            assert sr.abort_operation(first, error="cancelled", db=db)
            third = sr.try_acquire("t-3", "steve", "ccc", "dev", "three", db=db)
            assert third is not None  # allowed — slot genuinely settled
        finally:
            db.close()

    def test_try_acquire_allowed_when_only_stale_in_flight(self, isolated_db):
        from automation import self_restart as sr
        from datetime import datetime, timedelta, timezone
        from infra.database import get_db

        old = (
            datetime.now(timezone.utc)
            - timedelta(seconds=sr.IN_FLIGHT_TTL_SECONDS + 60)
        ).isoformat()
        db = get_db()
        try:
            db.execute(
                "INSERT INTO self_restart_requests "
                "(id, requester_thread_id, status, requested_at) "
                "VALUES ('stale','t-old','in_flight',?)", (old,)
            )
            db.commit()
            acquired = sr.try_acquire("t-new", "steve", "ddd", "dev", "new", db=db)
            assert acquired is not None  # a stale in-flight row does not block
        finally:
            db.close()

    def test_abort_marks_failed_and_frees_the_slot(self, isolated_db):
        from automation import self_restart as sr
        from infra.database import get_db

        db = get_db()
        try:
            rid = sr.try_acquire("t-1", "steve", "aaa", "dev", "x", db=db)
            assert sr.abort_operation(rid, error="restart launch failed", db=db)
            row = db.execute(
                "SELECT status FROM self_restart_requests WHERE id=?", (rid,)
            ).fetchone()
            assert row["status"] == "failed"
            assert sr.in_flight_request(db=db) is None
            # A fresh acquire is allowed once the failed slot is released.
            assert sr.try_acquire("t-2", "steve", "bbb", "dev", "y", db=db) is not None
        finally:
            db.close()

    def test_clear_is_noop_without_in_flight(self, isolated_db):
        from automation import self_restart as sr

        assert sr.clear_stale_in_flight("bbb222") == 0
