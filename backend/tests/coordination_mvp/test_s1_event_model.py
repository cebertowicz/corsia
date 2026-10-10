"""S1 — Event Model envelope & identity.

Proves the declared-formula substrate satisfies the S0 contract fixtures and the
S1 acceptance focus: stable identity, redelivery dedupe, version facts, distinct
internal state-transition identities, manifest shape, and ONE GitHub identity
source (no parallel path).
"""

from __future__ import annotations

import pytest

from events.connectors import (
    UnregisteredConnectorError,
    UnregisteredEventTypeError,
    get_manifest,
    registered_connectors,
)
from events.envelope import normalize
from events.identity_formula import MalformedEventError, compute_event_id
from tests.coordination_mvp import fixtures as F

_NORMALIZABLE = {"gmail", "github", "document"}


def _normalizable_scenarios():
    return [s for s in F.all_scenarios() if s.connector in _NORMALIZABLE]


class TestEnvelopeMatchesFixtures:
    """Every S1-normalizable fixture normalizes to its declared contract."""

    @pytest.mark.parametrize("scn", _normalizable_scenarios(),
                             ids=lambda s: s.name)
    def test_envelope_matches_declared_contract(self, scn):
        env = normalize(scn.connector, scn.raw_payload)
        assert env.type == scn.internal_event
        assert env.event_class == scn.event_class
        assert env.ownerless_ok == scn.ownerless_ok
        for key, value in scn.expected_facts.items():
            assert env.facts.get(key) == value, f"{scn.name}: fact {key}"
        if scn.artifact_ref is not None:
            assert env.artifact_ref == scn.artifact_ref
        if scn.relationships:
            got = {(r["edge"], r["to_ref"]) for r in env.relationships}
            want = {(r["edge"], r["to_ref"]) for r in scn.relationships}
            assert want <= got, f"{scn.name}: relationships {want - got} missing"


class TestStableIdentity:
    """EM1/EM5/AC-1.1 — same occurrence → same id; different facts → different."""

    def test_gmail_first_touch_stable_id(self):
        scn = F.scenario("gmail_first_touch")
        a = normalize("gmail", dict(scn.raw_payload))
        b = normalize("gmail", dict(scn.raw_payload))
        assert a.event_id == b.event_id
        # Independent of arrival order / unrelated fields.
        jittered = dict(scn.raw_payload, label="WORK", subject="re: hello")
        assert normalize("gmail", jittered).event_id == a.event_id
        # A different message is a different event.
        other = dict(scn.raw_payload, message_id="msg-999")
        assert normalize("gmail", other).event_id != a.event_id

    def test_github_redelivery_same_id(self):
        scn = F.scenario("github_check_completed")
        first = normalize("github", dict(scn.raw_payload))
        # A webhook redelivery carries the same facts but a new delivery GUID.
        redelivered = dict(scn.raw_payload, delivery_guid="new-guid")
        assert normalize("github", redelivered).event_id == first.event_id

    def test_doc_comment_version_in_identity(self):
        scn = F.scenario("document_comment")
        v3 = normalize("document", dict(scn.raw_payload))
        assert v3.facts["version"] == 3
        v4 = normalize("document", dict(scn.raw_payload, version=4))
        assert v4.event_id != v3.event_id, "version must distinguish the event"

    def test_internal_plan_occurrences_have_durable_event_identities(self):
        def material(event_type: str, occurrence: str, revision: int):
            return normalize("plan", {
                "material_event": event_type, "plan_id": "p1", "node_id": "n1",
                "plan_event_id": occurrence, "plan_revision": revision,
            })
        blocked = material("plan.node_blocked", "event-1", 1)
        review = material("plan.review_requested", "event-2", 2)
        completed = material("plan.node_completed", "event-3", 3)
        assert len({blocked.event_id, review.event_id, completed.event_id}) == 3
        assert "task_id" not in completed.facts


class TestManifestShape:
    """AC-1.3 — a manifest declares its facts/identity/relationships, not behavior."""

    @pytest.mark.parametrize("connector", ["gmail", "github", "document", "plan"])
    def test_manifest_declares_required_fields(self, connector):
        m = get_manifest(connector)
        assert m.namespace and m.version and m.event_types
        for decl in m.event_types.values():
            assert decl.identity_facts, f"{connector}:{decl.event_type} no identity"
            assert decl.event_class in {"actionable", "informational"}

    def test_all_builtin_connectors_registered(self):
        assert _NORMALIZABLE | {"plan"} <= set(registered_connectors())


class TestSingleGitHubIdentitySource:
    """Replace-don't-preserve — the manifest and the legacy router resolve a PR
    to the SAME external id (no parallel identity derivation)."""

    def test_pr_artifact_ref_matches_resolve_identity(self):
        from events.identity import resolve_identity
        scn = F.scenario("github_pr_opened")
        env = normalize("github", scn.raw_payload)
        legacy = resolve_identity("webhook.github.pr_opened", scn.raw_payload)
        assert env.artifact_ref["external_id"] == legacy.external_id

    def test_check_artifact_ref_matches_resolve_identity(self):
        from events.identity import resolve_identity
        scn = F.scenario("github_check_completed")
        env = normalize("github", scn.raw_payload)
        legacy = resolve_identity("webhook.github.check_suite_completed",
                                  scn.raw_payload)
        assert env.artifact_ref["external_id"] == legacy.external_id


class TestLoudRejection:
    """EM4/EM7/EM11/EM19 — unknown source/type/identity reject loudly, no guess."""

    def test_unregistered_connector_rejects(self):
        with pytest.raises(UnregisteredConnectorError):
            normalize("slack", {"x": 1})

    def test_unregistered_event_type_rejects(self):
        with pytest.raises(UnregisteredEventTypeError):
            normalize("github", {"repository": {"full_name": "o/r"}}, event_type="pr.frobnicate")

    def test_missing_identity_fact_is_malformed(self):
        with pytest.raises(MalformedEventError):
            # email.received requires message_id.
            normalize("gmail", {"from": "x@example.com", "thread_id": "t"})

    def test_compute_event_id_rejects_missing_fact(self):
        with pytest.raises(MalformedEventError):
            compute_event_id("gmail", "email.received", {"from": "x"}, ("message_id",))
