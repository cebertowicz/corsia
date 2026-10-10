"""S7 — reliability automations.

Proves: watches arm from concrete conditions, persist, resolve once, expire once;
a missing expected event routes through S4b as delivery_failure (never clean,
never generic dead-letter); duplicate expiry / restart replay cannot double-wake;
startup re-arm + restart drain are explicit; liveness distinguishes
waiting-on-watch from idle-incomplete and never wake-chains parents.
"""

from __future__ import annotations

import pytest

from automation import restart_wakes
from automation.watches import (
    UnboundWatchError, WatchError, arm_watch, due_watches, expire_watch, get_watch, rearm_on_startup,
    resolve_watch, sweep_expired,
)
from registry import attention
from registry import models
from tests.event_routing_helpers import mk_session

_DEADLINE = {"type": "deadline", "value": "2026-06-27T09:00:00Z"}


@pytest.fixture(autouse=True)
def _disable_analytics(monkeypatch):
    monkeypatch.setenv("CORSIA_ANALYTICS_DISABLE", "1")


def _user_raise(external_id: str) -> str:
    owner = mk_session("owner")
    models.register_artifact(integration="local", type="user-raise",
                             external_id=external_id, parent_thread_id=owner)
    return owner


class TestWatchLifecycle:
    def test_arm_is_idempotent(self, isolated_db):
        _user_raise("w-1")
        a = arm_watch("w-1", _DEADLINE)
        b = arm_watch("w-1", _DEADLINE)
        assert a.id == b.id and a.status == "armed"

    def test_arm_is_one_threads_wait_on_one_artifact(self, isolated_db):
        """Another thread's wait, or another integration's artifact, is its own.

        A watch dedupes on the artifact's full identity and its owner — the
        shape resolution matches on — not on the external id alone, which two
        integrations can share, and not across threads, whose waits end
        separately when the event reaches each of them.
        """
        from registry import artifact_record

        owner = _user_raise("w-11")
        other = mk_session("other")
        twin = {"integration": "remote", "type": "user-raise", "external_id": "w-11"}
        models.register_artifact(**twin, parent_thread_id=other)
        twin_id = artifact_record.register_or_resolve(twin).identity["id"]

        mine = arm_watch("w-11", _DEADLINE, owner_thread=owner,
                         expected_event="raise.answered")
        theirs = arm_watch("w-11", _DEADLINE, owner_thread=other,
                           expected_event="raise.answered")
        twins = arm_watch(twin_id, _DEADLINE, owner_thread=other,
                          expected_event="raise.answered")
        again = arm_watch("w-11", _DEADLINE, owner_thread=owner,
                          expected_event="raise.answered")

        assert mine.id == again.id
        assert len({mine.id, theirs.id, twins.id}) == 3
        assert {mine.status, theirs.status, twins.status} == {"armed"}

    def test_arm_requires_concrete_condition(self, isolated_db):
        _user_raise("w-2")
        with pytest.raises(WatchError):
            arm_watch("w-2", {})

    def test_resolve_once(self, isolated_db):
        _user_raise("w-3")
        w = arm_watch("w-3", _DEADLINE)
        assert resolve_watch(w.id) is True
        assert resolve_watch(w.id) is False  # already final
        assert get_watch(w.id).status == "resolved"

    def test_expire_routes_delivery_failure_once(self, isolated_db):
        owner = _user_raise("w-4")
        w = arm_watch("w-4", _DEADLINE)
        first = expire_watch(w.id)
        second = expire_watch(w.id)
        assert first.routed_wakes == 1
        assert first.resolution == "delivery_failure"   # not generic dead_letter
        assert first.target == owner
        assert second.routed_wakes == 0 and second.already_final is True

    def test_resolved_watch_does_not_expire(self, isolated_db):
        _user_raise("w-5")
        w = arm_watch("w-5", _DEADLINE)
        resolve_watch(w.id)
        assert expire_watch(w.id).routed_wakes == 0  # no wake after resolve


class TestDeferReraiseOnExpiry:
    def test_deferred_attention_reraises_on_expiry(self, isolated_db):
        _user_raise("w-def")
        attention.raise_attention("w-def")
        attention.clear_attention("w-def", outcome="defer", condition=_DEADLINE)
        assert attention.get_attention_state("w-def") == "deferred"
        w = arm_watch("w-def", _DEADLINE)
        expire_watch(w.id)
        assert attention.get_attention_state("w-def") == "raised"  # re-raised


class TestSweepAndStartup:
    def test_sweep_expires_due_deadline_watches(self, isolated_db):
        _user_raise("w-due")
        arm_watch("w-due", _DEADLINE)
        routed = sweep_expired("2099-01-01T00:00:00Z")  # everything is due
        assert routed == 1

    def test_rearm_reports_orphaned_watch_loud(self, isolated_db):
        _user_raise("w-live")
        arm_watch("w-live", _DEADLINE)
        # An armed watch for an artifact that never registered → orphaned.
        arm_watch("w-ghost", {"type": "deadline", "value": "2030-01-01T00:00:00Z"})
        result = rearm_on_startup()
        assert "w-live" not in result["orphaned"]
        assert result["rearmed"] >= 1 and result["orphaned"]

    def test_a_watch_nothing_could_end_is_refused(self, isolated_db):
        """Only a deadline ends by itself; any other wait needs its artifact (#1707)."""
        _user_raise("w-bound")
        event = {"type": "event", "value": "ci.complete"}
        bound = arm_watch("w-bound", event, expected_event="ci.complete")

        with pytest.raises(UnboundWatchError, match="only a deadline watch ends"):
            arm_watch("w-unbound", event, expected_event="ci.complete")

        assert get_watch(bound.id).condition == event
        unbound_deadline = arm_watch(
            "w-unbound", {"type": "deadline", "value": "2099-01-01T00:00:00Z"})
        assert unbound_deadline.condition["type"] == "deadline"


class TestRestartDrain:
    def test_startup_materializes_live_and_surfaces_dead(self, isolated_db):
        from infra.database import get_db
        live = mk_session("live")
        dead = mk_session("dead")
        db = get_db()
        db.execute(
            "UPDATE threads SET lifecycle_state='archived', "
            "archived_reason='user' WHERE id=?",
            (dead,),
        )
        db.commit(); db.close()
        restart_wakes.register_hard_recovery(live, "turn-live")
        restart_wakes.register_hard_recovery(dead, "turn-dead")
        assert restart_wakes.materialize_pending_registrations() == (live,)
        db = get_db()
        try:
            states = dict(db.execute(
                "SELECT thread_id,status FROM restart_wake_registrations"
            ).fetchall())
            assert states == {live: "woken", dead: "unresolved"}
        finally:
            db.close()

    def test_startup_materialization_is_idempotent(self, isolated_db):
        live = mk_session("live")
        restart_wakes.register_hard_recovery(live, "turn-live")
        first = restart_wakes.materialize_pending_registrations()
        second = restart_wakes.materialize_pending_registrations()
        assert first == (live,) and second == ()


class TestExpiryExactlyOnceOnCrash:
    """#595 flag 2: a crash after claim (expired, routed_at NULL) must re-route
    exactly once — never a silent lost wake, never a double-wake."""

    def test_claimed_but_unrouted_watch_reroutes(self, isolated_db):
        from infra.database import get_db
        _user_raise("w-crash")
        w = arm_watch("w-crash", _DEADLINE)
        # Simulate a crash after the claim commit but before routing.
        db = get_db()
        db.execute("UPDATE expected_event_watches SET status='expired', "
                   "expired_at='t', routed_at=NULL WHERE id=?", (w.id,))
        db.commit(); db.close()
        result = expire_watch(w.id)
        assert result.routed_wakes == 1            # wake recovered, not lost
        assert expire_watch(w.id).routed_wakes == 0  # and not re-fired afterward

    def test_fully_routed_watch_does_not_refire(self, isolated_db):
        _user_raise("w-done")
        w = arm_watch("w-done", _DEADLINE)
        assert expire_watch(w.id).routed_wakes == 1
        # routed_at is now set → any retry/replay is a no-op.
        assert expire_watch(w.id).routed_wakes == 0


class TestConcurrentExpiry:
    """#595 flag (watches.py race): two concurrent expire_watch() calls must route
    EXACTLY once — the routed_at-IS-NULL atomic claim is the set-once guard."""

    def test_concurrent_expiry_routes_exactly_once(self, isolated_db):
        import threading
        _user_raise("w-conc")
        w = arm_watch("w-conc", _DEADLINE)
        results: list = []
        lock = threading.Lock()
        barrier = threading.Barrier(2)

        def worker():
            barrier.wait()  # release both at once to force the interleave
            r = expire_watch(w.id)
            with lock:
                results.append(r)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sum(r.routed_wakes for r in results) == 1     # no double-wake
        assert sorted(r.routed_wakes for r in results) == [0, 1]


class TestDeadlineCanonicalComparison:
    """#595 flag: deadlines compare chronologically (parsed), not as raw strings;
    naive (tz-less) deadlines fail loud before arming."""

    def test_due_watches_compares_chronologically_across_formats(self, isolated_db):
        for eid, owner in (("w-past", "own-past"), ("w-future", "own-future")):
            models.register_artifact(integration="local", type="user-raise",
                                     external_id=eid, parent_thread_id=mk_session(owner))
        arm_watch("w-past", {"type": "deadline", "value": "2020-01-01T00:00:00+00:00"})
        arm_watch("w-future", {"type": "deadline", "value": "2099-01-01T00:00:00+00:00"})
        due = due_watches("2026-06-26T00:00:00Z")  # 'Z' now vs stored '+00:00'
        ids = {w.artifact_ref["external_id"] for w in due if w.artifact_ref}
        assert "w-past" in ids and "w-future" not in ids

    def test_naive_deadline_fails_loud_before_arming(self, isolated_db):
        _user_raise("w-naive")
        with pytest.raises(WatchError):
            arm_watch("w-naive", {"type": "deadline", "value": "2026-07-01T00:00"})


class TestDrainConcurrency:
    """#595 flag: concurrent drains report each registration at most once."""

    def test_concurrent_drain_reports_exactly_once(self, isolated_db):
        import threading
        live = mk_session("live-c")
        restart_wakes.register_hard_recovery(live, "turn-live-c")
        results: list = []
        lock = threading.Lock()
        barrier = threading.Barrier(2)

        def worker():
            barrier.wait()
            r = restart_wakes.materialize_pending_registrations()
            with lock:
                results.append(r)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sum(len(r) for r in results) == 1  # not double-reported


class TestMissingArtifactAtExpiry:
    """#595 flag: a vanished artifact identity at expiry is parked LOUD to the
    dead-letter sink — never a silent dropped wake reported as success."""

    def test_missing_identity_parks_not_silent(self, isolated_db):
        from infra.database import get_db
        from events import dead_letter
        _user_raise("w-gone")
        w = arm_watch("w-gone", _DEADLINE)
        db = get_db()
        db.execute("DELETE FROM artifact_identities WHERE external_id='w-gone'")
        db.commit(); db.close()
        result = expire_watch(w.id)
        assert result.routed_wakes == 1              # routed (to the dead-letter sink)
        assert result.resolution == "dead_letter"    # not a false owner-wake success
        parked = dead_letter.list_parked()
        assert any(p["reason"] == "artifact_identity_missing_at_expiry" for p in parked)

    def test_plan_node_watch_expiry_parks_no_dead_letter(self, isolated_db):
        """#505: a watch armed on a plan node has no artifact identity to find.

        Its owner is woken by the continuation T2 source, so expiry must not
        look the node id up as an artifact and park a false dead letter.
        """
        from events import dead_letter
        owner = mk_session("owner")
        w = arm_watch("node-505", _DEADLINE, owner_thread=owner, plan_node_id="node-505")
        assert w.artifact_ref["integration"] is None

        result = expire_watch(w.id)

        assert result.routed_wakes == 1
        assert result.resolution != "dead_letter"
        assert dead_letter.list_parked() == []
        assert get_watch(w.id).status == "expired"
