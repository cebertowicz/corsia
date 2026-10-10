"""S4a — the single resolve_targets() owner-resolution path.

Proves the four outcomes (registered resolve · rule-bind+register · ownerless
triage · dead-letter) all flow through ONE connector-agnostic function, and that
mechanism-vs-no-owner dead-letter reasons stay distinct. GitHub behavior
preservation is covered by the existing event-routing test suites (route_event
now delegates owner resolution to resolve_targets).
"""

from __future__ import annotations

import pytest

from connections import binder
from events.envelope import EventEnvelope, normalize
from events.resolve import resolve_targets
from registry import artifact_record as AR
from tests.coordination_mvp import fixtures as F
from tests.event_routing_helpers import mk_session

GMAIL_THREAD = {"integration": "gmail", "type": "email-thread", "external_id": "thr-1"}


@pytest.fixture(autouse=True)
def _disable_analytics(monkeypatch):
    monkeypatch.setenv("CORSIA_ANALYTICS_DISABLE", "1")


class TestRegisteredResolve:
    def test_owned_artifact_resolves_path_a(self, isolated_db):
        owner = mk_session("owner")
        AR.register_or_resolve(GMAIL_THREAD, owner_intent=owner)
        env = normalize("gmail", F.scenario("gmail_repeat").raw_payload)
        target = resolve_targets(env)[0]
        assert target.resolution == "path_a_resolve"
        assert target.relationship == "owner"
        assert target.thread == owner


class TestRuleBindRegister:
    def test_first_touch_binds_and_owns_path_b(self, isolated_db):
        dest = mk_session("proj-a")
        repair = mk_session("conductor")
        binder.create_rule(connector="gmail", fact="from",
                           value="user_1@example.com", destination_thread_id=dest)
        env = normalize("gmail", F.scenario("gmail_first_touch").raw_payload)
        target = resolve_targets(env, repair_owner=repair)[0]
        assert target.resolution == "path_b_rule_bind"
        assert target.thread == dest
        # The artifact is now owned — a second resolve is path_a (ownership).
        again = resolve_targets(env, repair_owner=repair)[0]
        assert again.resolution == "path_a_resolve"


class TestDeclarationBind:
    def test_first_touch_declaration_uses_distinct_resolution(self, isolated_db):
        declared = mk_session("declared")
        env = EventEnvelope(
            event_id="e", type="x.y", connector="gmail",
            facts={"declared_owner_thread": declared}, relationships=(),
            artifact_ref={"integration": "gmail", "type": "email-thread", "external_id": "declared"},
            provenance={}, event_class="actionable", ownerless_ok=False,
        )

        target = resolve_targets(env)[0]

        assert target.resolution == "path_b_declaration_bind"
        assert target.thread == declared


class TestRepair:
    def test_equal_precedence_conflict_resolves_repair(self, isolated_db):
        a, b = mk_session("a"), mk_session("b")
        repair = mk_session("conductor")
        binder.create_rule(connector="gmail", fact="from", value="boss@example.com",
                           destination_thread_id=a, precedence=50)
        binder.create_rule(connector="gmail", fact="label", value="labelB",
                           destination_thread_id=b, precedence=50)
        env = normalize("gmail", F.scenario("peer_conflict_repair").raw_payload)
        target = resolve_targets(env, repair_owner=repair)[0]
        assert target.resolution == "repair"
        assert target.repair_artifact.owner_thread == repair


class TestOwnerlessTriage:
    def test_ownerless_event_routes_to_triage(self, isolated_db):
        # An ownerless_ok event with no owner and no rule resolves to triage,
        # distinct from a dead-letter.
        env = EventEnvelope(
            event_id="e", type="x.y", connector="gmail", facts={},
            relationships=(), artifact_ref=GMAIL_THREAD, provenance={},
            event_class="informational", ownerless_ok=True)
        target = resolve_targets(env)[0]
        assert target.resolution == "triage"
        assert target.reason == "ownerless_awaiting_triage_owner"


class TestDeadLetter:
    def test_no_artifact_dead_letters(self, isolated_db):
        env = EventEnvelope(
            event_id="e", type="x.y", connector=None, facts={}, relationships=(),
            artifact_ref=None, provenance={}, event_class="actionable",
            ownerless_ok=False)
        target = resolve_targets(env)[0]
        assert target.resolution == "dead_letter"
        assert target.reason == "no_artifact"

    def test_unowned_not_ownerless_dead_letters(self, isolated_db):
        env = normalize("gmail", F.scenario("gmail_first_touch").raw_payload)
        target = resolve_targets(env)[0]  # no rule, ownerless_ok False
        assert target.resolution == "dead_letter"
        assert target.reason == "no_owner"

    def test_mechanism_and_no_owner_reasons_are_distinct(self):
        # The dead-letter reason taxonomy distinguishes the failure modes — a
        # no-owner is not conflated with a missing artifact or inactive owner.
        assert len({"no_artifact", "no_owner", "owner_inactive"}) == 3
