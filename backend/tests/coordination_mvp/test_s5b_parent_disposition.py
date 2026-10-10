"""S5b — parent disposition API + enforcement.

Proves: the seven outcomes; the parent (not the profile) decides; a blocking
profile failure halts approve_to_user unless an authorized override is recorded;
reject/answer/revision route back to the child owner and never look user-visible;
defer_until needs a concrete S7-armable condition; reroute_owner is explicit +
audited (and cannot seize a parent_chain thread); supersede closes the request
without archiving the artifact.
"""

from __future__ import annotations

import pytest

from parent_review import disposition as D
from parent_review.disposition import BlockingProfileError, DispositionError, dispose
from registry import artifact_record as AR
from registry import models
from registry.identity_bridge import ParentChainOwnershipError
from tests.event_routing_helpers import mk_session

_GOOD = {"requested_action": "approve budget", "evidence": "Q3", "confidence": "high",
         "urgency": "high"}
_BAD = {"requested_action": "approve budget"}  # missing evidence/confidence


@pytest.fixture(autouse=True)
def _disable_analytics(monkeypatch):
    monkeypatch.setenv("CORSIA_ANALYTICS_DISABLE", "1")


def _user_raise(external_id: str, snapshot: dict, owner: str | None = None) -> str:
    owner = owner or mk_session("child-owner")
    models.register_artifact(integration="local", type="user-raise",
                             external_id=external_id, parent_thread_id=owner)
    models.update_metadata("local", "user-raise", external_id, snapshot)
    return external_id


def _identity(external_id: str) -> dict:
    return {"integration": "local", "type": "user-raise", "external_id": external_id}


class TestApproveToUserGate:
    def test_clean_artifact_approves_to_user(self, isolated_db):
        _user_raise("ur-ok", _GOOD)
        result = dispose("ur-ok", D.APPROVE_TO_USER)
        assert result.user_visible is True
        assert result.attention_state == "raised"

    def test_blocking_failure_halts_approve(self, isolated_db):
        _user_raise("ur-bad", _BAD)
        with pytest.raises(BlockingProfileError):
            dispose("ur-bad", D.APPROVE_TO_USER)

    def test_authorized_conductor_override_allows_approve_with_audit(self, isolated_db):
        from infra import conductor
        cond = mk_session("workspace-conductor")
        conductor.set_conductor(conductor.WORKSPACE_SCOPE, cond)
        _user_raise("ur-bad", _BAD)
        result = dispose("ur-bad", D.APPROVE_TO_USER, override_blocking=True,
                         authority="conductor", reason="time-critical exception",
                         actor_thread=cond)
        assert result.user_visible is True
        assert result.audit["override_actor"] == cond
        assert result.audit["override_reason"]

    def test_self_asserted_override_authority_is_rejected(self, isolated_db):
        # #595 flag 1: a non-conductor caller cannot self-assert override authority
        # just by passing truthy authority/reason strings.
        attacker = mk_session("attacker")
        _user_raise("ur-bad", _BAD)
        with pytest.raises(BlockingProfileError):
            dispose("ur-bad", D.APPROVE_TO_USER, override_blocking=True,
                    authority="i-said-so", reason="trust me", actor_thread=attacker)

    def test_override_without_reason_or_actor_is_rejected(self, isolated_db):
        _user_raise("ur-bad", _BAD)
        with pytest.raises(BlockingProfileError):
            dispose("ur-bad", D.APPROVE_TO_USER, override_blocking=True,
                    authority="conductor")  # no reason, no actor_thread


class TestRouteBackNotUserVisible:
    @pytest.mark.parametrize("outcome,state", [
        (D.REJECT_WITH_REASON, "resolved"),
        (D.ANSWER_INTERNALLY, "resolved"),
        (D.REQUEST_REVISION, "revision_requested"),
    ])
    def test_route_back_to_child_owner(self, isolated_db, outcome, state):
        owner = mk_session("child-owner")
        _user_raise("ur-rb", _GOOD, owner=owner)
        result = dispose("ur-rb", outcome, reason="needs more detail")
        assert result.user_visible is False
        assert result.routed_to == owner
        assert result.route_disposition == "owner_wake"
        assert result.attention_state == state


class TestReroute:
    def _conductor(self) -> str:
        from infra import conductor
        cond = mk_session("workspace-conductor")
        conductor.set_conductor(conductor.WORKSPACE_SCOPE, cond)
        return cond

    def test_authorized_conductor_reroute_changes_owner(self, isolated_db):
        cond = self._conductor()
        old, new = mk_session("old-owner"), mk_session("new-owner")
        _user_raise("ur-rr", _GOOD, owner=old)
        result = dispose("ur-rr", D.REROUTE_OWNER, new_owner=new,
                         authority="conductor", reason="wrong team", actor_thread=cond)
        assert result.routed_to == new
        assert AR.register_or_resolve(_identity("ur-rr")).owner_thread == new

    def test_unauthorized_caller_cannot_reroute(self, isolated_db):
        # #595: a non-conductor actor cannot transfer ownership with strings.
        self._conductor()
        old, new = mk_session("old-owner"), mk_session("new-owner")
        attacker = mk_session("attacker")
        _user_raise("ur-rrx", _GOOD, owner=old)
        with pytest.raises(DispositionError):
            dispose("ur-rrx", D.REROUTE_OWNER, new_owner=new,
                    authority="i-said-so", reason="trust me", actor_thread=attacker)
        assert AR.register_or_resolve(_identity("ur-rrx")).owner_thread == old  # unchanged

    def test_reroute_requires_reason_and_actor(self, isolated_db):
        new = mk_session("new-owner")
        _user_raise("ur-rr2", _GOOD)
        with pytest.raises(DispositionError):
            dispose("ur-rr2", D.REROUTE_OWNER, new_owner=new)  # no reason/actor

    def test_reroute_cannot_seize_parent_chain(self, isolated_db):
        cond = self._conductor()
        old = mk_session("old-owner")
        pc = mk_session("parent-chain")
        _user_raise("ur-rr3", _GOOD, owner=old)
        AR.subscribe_parent_chain(_identity("ur-rr3"), pc)
        # Authorized actor, but the parent_chain ownership guard still holds.
        with pytest.raises(ParentChainOwnershipError):
            dispose("ur-rr3", D.REROUTE_OWNER, new_owner=pc, authority="conductor",
                    reason="x", actor_thread=cond)


class TestDefer:
    def test_deadline_defer_records_armable_condition(self, isolated_db):
        _user_raise("ur-def", _GOOD)
        cond = {"type": "deadline", "value": "2026-06-27T09:00:00Z"}
        result = dispose("ur-def", D.DEFER_UNTIL, condition=cond)
        assert result.attention_state == "deferred"
        # Deadline normalized to canonical UTC ISO.
        assert result.audit["condition"]["value"] == "2026-06-27T09:00:00+00:00"

    def test_naive_deadline_defer_rejected(self, isolated_db):
        _user_raise("ur-def-naive", _GOOD)
        with pytest.raises(DispositionError):
            dispose("ur-def-naive", D.DEFER_UNTIL,
                    condition={"type": "deadline", "value": "2026-07-01T00:00"})

    def test_unconditioned_defer_rejected(self, isolated_db):
        _user_raise("ur-def2", _GOOD)
        with pytest.raises(DispositionError):
            dispose("ur-def2", D.DEFER_UNTIL)  # no condition

    def test_deadline_without_value_rejected(self, isolated_db):
        _user_raise("ur-def3", _GOOD)
        with pytest.raises(DispositionError):
            dispose("ur-def3", D.DEFER_UNTIL, condition={"type": "deadline"})


class TestSupersede:
    def test_supersede_closes_without_archiving(self, isolated_db):
        _user_raise("ur-sup", _GOOD)
        result = dispose("ur-sup", D.SUPERSEDE, replacement_id="ur-new",
                         reason="replaced by consolidated ask")
        assert result.attention_state == "superseded"
        assert result.audit["superseded_by"] == "ur-new"
        # Artifact itself is NOT archived.
        assert AR.register_or_resolve(_identity("ur-sup")).lifecycle == "active"

    def test_supersede_requires_replacement(self, isolated_db):
        _user_raise("ur-sup2", _GOOD)
        with pytest.raises(DispositionError):
            dispose("ur-sup2", D.SUPERSEDE)


class TestNoSilentDropAndNoArchive:
    def test_unknown_outcome_rejected(self, isolated_db):
        _user_raise("ur-x", _GOOD)
        with pytest.raises(DispositionError):
            dispose("ur-x", "frobnicate")

    def test_disposition_never_archives_artifact(self, isolated_db):
        _user_raise("ur-keep", _GOOD)
        dispose("ur-keep", D.REJECT_WITH_REASON, reason="no")
        assert AR.register_or_resolve(_identity("ur-keep")).lifecycle == "active"
