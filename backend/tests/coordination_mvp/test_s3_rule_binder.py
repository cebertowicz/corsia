"""S3 — Connections rule binder.

Proves: first-touch rule-binds+owns via the S2 path; existing artifacts route by
ownership (rules not re-run); GitHub/doc use the same artifact mechanics; visible
precedence with first-match-wins; equal-precedence ties create a binder-repair
artifact (no silent tiebreak); matched-rule provenance recorded; stale filter
fields fail loud.
"""

from __future__ import annotations

import uuid

import pytest

from connections import binder
from connections.binder import StaleRuleFieldError
from events.envelope import normalize
from registry import artifact_record as AR
from tests.coordination_mvp import fixtures as F
from tests.event_routing_helpers import mk_session


@pytest.fixture(autouse=True)
def _disable_analytics(monkeypatch):
    monkeypatch.setenv("CORSIA_ANALYTICS_DISABLE", "1")


def _identity_metadata(identity: dict) -> dict:
    from infra.database import get_db
    db = get_db()
    try:
        import json
        row = db.execute(
            "SELECT metadata FROM artifact_identities "
            "WHERE integration=? AND type=? AND external_id=?",
            (identity["integration"], identity["type"], identity["external_id"]),
        ).fetchone()
        return json.loads(row["metadata"]) if row and row["metadata"] else {}
    finally:
        db.close()


class TestFirstTouchRuleBind:
    def test_gmail_first_touch_registers_and_owns(self, isolated_db):
        owner = mk_session("proj-a")
        repair = mk_session("conductor")
        binder.create_rule(connector="gmail", fact="from",
                           value="user_1@example.com", destination_thread_id=owner)
        env = normalize("gmail", F.scenario("gmail_first_touch").raw_payload)
        result = binder.bind_owner(env, repair_owner=repair)
        assert result.via == "rule"
        assert result.record.owner_thread == owner
        assert result.matched_rule_id is not None

    def test_matched_rule_provenance_recorded(self, isolated_db):
        owner = mk_session("proj-a")
        repair = mk_session("conductor")
        rule = binder.create_rule(connector="gmail", fact="from",
                                  value="user_1@example.com",
                                  destination_thread_id=owner)
        env = normalize("gmail", F.scenario("gmail_first_touch").raw_payload)
        binder.bind_owner(env, repair_owner=repair)
        meta = _identity_metadata(env.artifact_ref)
        assert meta.get("bound_by_rule") == rule.id
        assert meta.get("matched_fact") == "from"


class TestExistingRoutesByOwnership:
    def test_gmail_repeat_routes_by_ownership_not_rule(self, isolated_db):
        owner = mk_session("proj-a")
        repair = mk_session("conductor")
        rule = binder.create_rule(connector="gmail", fact="from",
                                  value="user_1@example.com",
                                  destination_thread_id=owner)
        first = normalize("gmail", F.scenario("gmail_first_touch").raw_payload)
        binder.bind_owner(first, repair_owner=repair)
        # Disable the rule — a repeat must still resolve by ownership alone.
        binder.disable_rule(rule.id)
        repeat = normalize("gmail", F.scenario("gmail_repeat").raw_payload)
        result = binder.bind_owner(repeat, repair_owner=repair)
        assert result.via == "ownership"
        assert result.record.owner_thread == owner
        assert result.matched_rule_id is None

    @pytest.mark.parametrize("scenario_name,identity", [
        ("github_check_completed",
         {"integration": "github", "type": "pull-request", "external_id": "o/r#7"}),
        ("document_comment",
         {"integration": "local", "type": "document", "external_id": "doc-9"}),
    ])
    def test_preregistered_artifact_routes_by_ownership(self, isolated_db,
                                                        scenario_name, identity):
        owner = mk_session("worker")
        repair = mk_session("conductor")
        AR.register_or_resolve(identity, owner_intent=owner)
        scn = F.scenario(scenario_name)
        env = normalize(scn.connector, scn.raw_payload)
        result = binder.bind_owner(env, repair_owner=repair)
        assert result.via == "ownership"
        assert result.record.owner_thread == owner


class TestPrecedence:
    def test_lower_precedence_wins(self, isolated_db):
        win = mk_session("high-prio")
        lose = mk_session("low-prio")
        repair = mk_session("conductor")
        binder.create_rule(connector="gmail", fact="label", value="INBOX",
                           destination_thread_id=lose, precedence=200)
        winner = binder.create_rule(connector="gmail", fact="from",
                                    value="user_1@example.com",
                                    destination_thread_id=win, precedence=10)
        env = normalize("gmail", F.scenario("gmail_first_touch").raw_payload)
        result = binder.bind_owner(env, repair_owner=repair)
        assert result.via == "rule"
        assert result.matched_rule_id == winner.id
        assert result.record.owner_thread == win


class TestEqualPrecedenceRepair:
    def test_equal_precedence_conflict_creates_repair_artifact(self, isolated_db):
        a = mk_session("proj-a")
        b = mk_session("proj-b")
        repair = mk_session("conductor")
        binder.create_rule(connector="gmail", fact="from",
                           value="user_1@example.com", destination_thread_id=a,
                           precedence=50)
        binder.create_rule(connector="gmail", fact="label", value="INBOX",
                           destination_thread_id=b, precedence=50)
        env = normalize("gmail", F.scenario("gmail_first_touch").raw_payload)
        result = binder.bind_owner(env, repair_owner=repair)
        assert result.via == "repair"
        # The email artifact is NOT owned by a guess.
        assert result.record.owner_thread is None
        # A real, owned repair artifact is created with conflict provenance.
        assert result.repair_artifact.owner_thread == repair
        assert set(result.conflict.destinations) == {a, b}
        assert len(result.conflict.rule_ids) == 2


class TestStaleFieldsFailLoud:
    def test_create_rule_rejects_undeclared_field(self, isolated_db):
        owner = mk_session("proj-a")
        with pytest.raises(StaleRuleFieldError):
            binder.create_rule(connector="gmail", fact="not_a_field",
                               value="x", destination_thread_id=owner)

    def test_stale_rules_detects_removed_field(self, isolated_db):
        owner = mk_session("proj-a")
        # Insert a rule directly (bypassing create-time validation) to simulate a
        # field the connector declared at create time but later removed.
        from datetime import datetime, timezone
        from infra.database import get_db
        db = get_db()
        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            '''INSERT INTO event_rules
               (id, scope, connector, "match", "to", "on", action,
                precedence, enabled, created_at, updated_at)
               VALUES (?,?,?,? ,?,'first_seen','deliver',?,1,?,?)''',
            (str(uuid.uuid4())[:12], "global", "gmail",
             '[{"field":"removed_field","op":"eq","value":"x"}]',
             'thread:' + owner, 100, now, now),
        )
        db.commit()
        db.close()
        stale = binder.stale_rules("gmail")
        assert len(stale) == 1 and stale[0].fact == "removed_field"
        with pytest.raises(StaleRuleFieldError):
            binder.validate_rule(stale[0])


class TestDeclarationBind:
    def test_declaration_beats_matching_rule_and_records_provenance(self, isolated_db):
        declared = mk_session("declared")
        rule_owner = mk_session("rule-owner")
        binder.create_rule(
            connector="gmail", fact="from", value="user_1@example.com",
            destination_thread_id=rule_owner,
        )
        identity = {"integration": "gmail", "type": "email-thread", "external_id": "declared"}
        result = binder.bind_owner_for(
            identity,
            {"declared_owner_thread": declared, "from": "user_1@example.com"},
            "gmail",
        )

        assert result.via == "declaration"
        assert result.record.owner_thread == declared
        assert _identity_metadata(identity)["ownership_provenance"] == "declared_in_pr_body"
