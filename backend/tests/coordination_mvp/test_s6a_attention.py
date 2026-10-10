"""S6a — user-attention lifecycle + closeout fan-out.

Proves: attention is artifact state (not an Event Delivery class); closeout clears
ONE state decisively and fans out to every subscribed level without archiving the
artifact or any subscription; close-viewer (reading) does not clear; defer records
a concrete S7-armable condition without arming; the fan-out is passive/digest
(no wake chaining).
"""

from __future__ import annotations

import json

import pytest

from registry import attention
from registry import artifact_record as AR
from registry import models
from registry.attention import AttentionError, clear_attention
from tests.coordination_mvp.fixtures import DELIVERY_DISPOSITIONS
from tests.event_routing_helpers import mk_session


@pytest.fixture(autouse=True)
def _disable_analytics(monkeypatch):
    monkeypatch.setenv("CORSIA_ANALYTICS_DISABLE", "1")


def _raised_artifact(external_id: str):
    """A raised user-raise owned by owner, with a subscriber + parent_chain level."""
    owner = mk_session("owner")
    sub = mk_session("subscriber")
    parent = mk_session("parent-chain")
    identity = {"integration": "local", "type": "user-raise", "external_id": external_id}
    models.register_artifact(integration="local", type="user-raise",
                             external_id=external_id, parent_thread_id=owner)
    models.register_artifact(integration="local", type="user-raise",
                             external_id=external_id, parent_thread_id=sub)
    AR.subscribe_parent_chain(identity, parent)
    attention.raise_attention(external_id)
    return identity, {"owner": owner, "sub": sub, "parent": parent}


class TestCloseoutFanOut:
    def test_clear_fans_out_across_all_levels(self, isolated_db):
        identity, t = _raised_artifact("ur-1")
        result = clear_attention("ur-1", outcome="answer")
        assert set(result.cleared_levels) == {t["owner"], t["sub"], t["parent"]}
        assert result.attention_state == "cleared"

    def test_clear_does_not_archive_artifact_or_subscription(self, isolated_db):
        identity, t = _raised_artifact("ur-2")
        result = clear_attention("ur-2", outcome="dismiss")
        assert result.artifact_archived is False
        assert result.subscription_archived is False
        record = AR.register_or_resolve(identity)
        assert record.lifecycle == "active"                 # artifact intact
        assert record.kind_of(t["sub"]) == AR.SUBSCRIBER_KIND  # subscription intact
        assert record.kind_of(t["parent"]) == AR.PARENT_CHAIN_KIND

    def test_closeout_is_decisive_single_state(self, isolated_db):
        _raised_artifact("ur-3")
        clear_attention("ur-3", outcome="answer")
        assert attention.get_attention_state("ur-3") == "cleared"
        # Cleared across every level at once — not raised anywhere anymore.
        assert attention.list_raised() == []

    def test_one_change_is_passive_digest_never_wake(self, isolated_db):
        from infra.database import get_db
        identity, t = _raised_artifact("ur-4")
        clear_attention("ur-4", outcome="acknowledge")
        db = get_db()
        rows = db.execute(
            "SELECT details_json FROM context_change_journal "
            "WHERE change_kind='artifact.attention_cleared'").fetchall()
        db.close()
        assert len(rows) == 1, "closeout must append one subscriber-independent change"
        for r in rows:
            prov = json.loads(r["details_json"])["provenance"]
            assert prov["wake"] is False and prov["delivery"] == "digest"


class TestCloseViewerIsNotClear:
    def test_reading_does_not_clear(self, isolated_db):
        _raised_artifact("ur-5")
        # Read paths must not mutate attention state.
        assert attention.get_attention_state("ur-5") == "raised"
        assert any(a["external_id"] == "ur-5" for a in attention.list_raised())
        assert attention.get_attention_state("ur-5") == "raised"  # still raised


class TestDefer:
    def test_defer_records_armable_condition_without_arming(self, isolated_db):
        _raised_artifact("ur-6")
        cond = {"type": "deadline", "value": "2026-06-27T09:00:00Z"}
        result = clear_attention("ur-6", outcome="defer", condition=cond)
        assert result.attention_state == "deferred"
        # Deadline normalized to canonical UTC ISO (S7 arms from this).
        assert result.defer_condition == {"type": "deadline",
                                          "value": "2026-06-27T09:00:00+00:00"}

    def test_naive_deadline_without_timezone_is_rejected(self, isolated_db):
        _raised_artifact("ur-naive")
        with pytest.raises(AttentionError):
            clear_attention("ur-naive", outcome="defer",
                            condition={"type": "deadline", "value": "2026-07-01T00:00"})

    def test_unconditioned_defer_rejected(self, isolated_db):
        _raised_artifact("ur-7")
        with pytest.raises(AttentionError):
            clear_attention("ur-7", outcome="defer")

    @pytest.mark.parametrize(
        "condition",
        [
            {"type": "event"},
            {"type": "event", "value": ""},
        ],
    )
    def test_condition_requires_type_and_nonempty_value(self, isolated_db, condition):
        artifact_id = f"ur-invalid-{len(condition)}-{condition.get('value', 'none')}"
        _raised_artifact(artifact_id)
        with pytest.raises(AttentionError):
            clear_attention(
                artifact_id, outcome="defer", condition=condition,
            )


class TestListRaisedFiltering:
    """list_raised SQL-filters by attention_state and scopes by subscriber thread."""

    def test_only_raised_state_is_listed(self, isolated_db):
        def seed(eid: str, owner: str) -> None:
            models.register_artifact(integration="local", type="user-raise",
                                     external_id=eid, parent_thread_id=mk_session(owner))
            attention.raise_attention(eid)
        seed("lr-raised", "o1")                                         # stays raised
        seed("lr-deferred", "o2")
        clear_attention("lr-deferred", outcome="defer",
                        condition={"type": "deadline", "value": "2026-06-27T09:00:00Z"})
        seed("lr-cleared", "o3")
        clear_attention("lr-cleared", outcome="answer")
        ids = {a["external_id"] for a in attention.list_raised()}
        assert ids == {"lr-raised"}                                     # not deferred/cleared

    def test_thread_scoping(self, isolated_db):
        _, t = _raised_artifact("lr-scope")
        scoped = {a["external_id"] for a in attention.list_raised(thread_id=t["sub"])}
        assert "lr-scope" in scoped                                     # subscriber sees it
        other = {a["external_id"] for a in attention.list_raised(thread_id="not-a-subscriber")}
        assert "lr-scope" not in other                                 # non-subscriber does not

    def test_listing_is_read_only(self, isolated_db):
        _raised_artifact("lr-ro")
        attention.list_raised()
        attention.list_raised(thread_id="x")
        assert attention.get_attention_state("lr-ro") == "raised"       # reads never clear


class TestAttentionIsNotADeliveryClass:
    def test_attention_states_are_not_delivery_dispositions(self):
        for state in (attention.ATTENTION_RAISED, attention.ATTENTION_DEFERRED,
                      attention.ATTENTION_CLEARED):
            assert state not in DELIVERY_DISPOSITIONS

    def test_unknown_outcome_rejected(self, isolated_db):
        _raised_artifact("ur-8")
        with pytest.raises(AttentionError):
            clear_attention("ur-8", outcome="frobnicate")
