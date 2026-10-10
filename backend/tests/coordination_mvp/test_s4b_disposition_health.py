"""S4b — delivery disposition table + live health sink.

Proves: (relationship, event_class, lifecycle) -> wake|digest; parent_chain is
structurally digest-only (can never be woken); the combined taxonomy stays
within the five ED5 values (no user-attention class); the health chain always
terminates at a live non-archivable sink; and delivery_failure stays distinct
from no-owner dead-letter.
"""

from __future__ import annotations

import pytest

from events import health_sink
from events.disposition import WAKE, delivery_disposition, disposition
from events.resolve import resolve_delivery_failure, resolve_targets
from tests.coordination_mvp.fixtures import DELIVERY_DISPOSITIONS, FORBIDDEN_DELIVERY_CLASSES
from tests.event_routing_helpers import mk_session


@pytest.fixture(autouse=True)
def _disable_analytics(monkeypatch):
    monkeypatch.setenv("CORSIA_ANALYTICS_DISABLE", "1")


class TestDispositionTable:
    def test_owner_actionable_wakes_informational_digests(self):
        assert disposition("owner", "actionable", "active") == "wake"
        assert disposition("owner", "informational", "active") == "digest"

    def test_triage_and_repair_wake(self):
        assert disposition("triage", "informational", "active") == "wake"
        assert disposition("repair", "informational", "active") == "wake"

    def test_subscriber_digests_by_default(self):
        assert disposition("subscriber", "actionable", "active") == "digest"

    def test_dead_artifact_never_wakes(self):
        assert disposition("owner", "actionable", "archived") == "digest"
        assert disposition("owner", "actionable", "tombstone") == "digest"

    def test_unknown_relationship_never_wakes(self):
        assert disposition("mystery", "actionable", "active") == "digest"


class TestParentChainHardwiredDigest:
    @pytest.mark.parametrize("event_class", ["actionable", "informational"])
    @pytest.mark.parametrize("lifecycle", ["active", "archived", "tombstone"])
    def test_parent_chain_is_always_digest(self, event_class, lifecycle):
        assert disposition("parent_chain", event_class, lifecycle) != WAKE
        assert delivery_disposition("parent_chain", event_class, lifecycle) == "subscriber_digest"


class TestCombinedTaxonomy:
    def test_labels_are_within_the_five_values(self):
        for rel in ["owner", "subscriber", "parent_chain", "triage", "repair", "none"]:
            for ec in ["actionable", "informational"]:
                label = delivery_disposition(rel, ec)
                assert label in DELIVERY_DISPOSITIONS
                assert label not in FORBIDDEN_DELIVERY_CLASSES

    def test_triage_and_repair_fold_to_owner_wake(self):
        assert delivery_disposition("triage", "informational") == "owner_wake"
        assert delivery_disposition("repair", "informational") == "owner_wake"

    def test_none_is_dead_letter(self):
        assert delivery_disposition("none", "actionable") == "dead_letter"


class TestHealthSink:
    def test_resolves_first_live_owner(self, isolated_db):
        live = mk_session("live-owner")
        assert health_sink.resolve_live_health_owner([live]) == live

    def test_reresolves_past_archived_to_next_live(self, isolated_db):
        from infra.database import get_db
        dead = mk_session("dead-owner")
        live = mk_session("live-owner")
        db = get_db()
        db.execute(
            "UPDATE threads SET lifecycle_state='archived', "
            "archived_reason='user' WHERE id=?",
            (dead,),
        )
        db.commit(); db.close()
        assert health_sink.resolve_live_health_owner([dead, live]) == live

    def test_all_dead_terminates_at_non_archivable_sentinel(self, isolated_db):
        from infra.database import get_db
        dead = mk_session("dead-owner")
        db = get_db()
        db.execute(
            "UPDATE threads SET lifecycle_state='archived', "
            "archived_reason='user' WHERE id=?",
            (dead,),
        )
        db.commit(); db.close()
        sink = health_sink.resolve_live_health_owner([dead, None])
        assert sink == health_sink.WORKSPACE_HEALTH_SINK
        assert health_sink.is_health_sink(sink)

    def test_empty_chain_never_dead_ends(self, isolated_db):
        assert health_sink.resolve_live_health_owner([]) == health_sink.WORKSPACE_HEALTH_SINK


class TestDeliveryFailureDistinctFromDeadLetter:
    def test_delivery_failure_routes_to_live_owner(self, isolated_db):
        owner = mk_session("owner")
        target = resolve_delivery_failure(owner, reason="watch_expired")
        assert target.resolution == "delivery_failure"
        assert target.thread == owner
        assert target.health_sink_is_live is True

    def test_delivery_failure_falls_back_to_sentinel(self, isolated_db):
        target = resolve_delivery_failure(None, reason="mechanism_fault")
        assert target.resolution == "delivery_failure"
        assert target.thread == health_sink.WORKSPACE_HEALTH_SINK

    def test_failure_and_dead_letter_are_distinct_resolutions(self, isolated_db):
        from events.envelope import EventEnvelope
        env = EventEnvelope(event_id="e", type="x.y", connector=None, facts={},
                            relationships=(), artifact_ref=None, provenance={},
                            event_class="actionable", ownerless_ok=False)
        dead = resolve_targets(env)[0]
        failure = resolve_delivery_failure(None, reason="mechanism_fault")
        assert dead.resolution == "dead_letter"
        assert failure.resolution == "delivery_failure"
        assert dead.resolution != failure.resolution
