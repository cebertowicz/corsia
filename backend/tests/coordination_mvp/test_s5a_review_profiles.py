"""S5a — Artifact Review Profile execution.

Proves: profiles bind by registry artifact type/subtype (no parallel taxonomy);
execution is fresh-context only (no parent conversation); output is structured
findings + a recommendation (profile does NOT make the final disposition);
blocking vs advisory is data; and a profile that cannot run fails loud.
"""

from __future__ import annotations

import pytest

from parent_review import profiles
from parent_review.profiles import ProfileExecutionError, run_review_profile
from registry import models
from tests.coordination_mvp.fixtures import DELIVERY_DISPOSITIONS
from tests.event_routing_helpers import mk_session

# Parent-disposition outcomes (PRG5) — the recommendation vocabulary.
_PARENT_OUTCOMES = {"approve_to_user", "reject_with_reason", "answer_internally",
                    "request_revision", "reroute_owner", "defer_until", "supersede"}


@pytest.fixture(autouse=True)
def _disable_analytics(monkeypatch):
    monkeypatch.setenv("CORSIA_ANALYTICS_DISABLE", "1")


def _register_user_raise(external_id: str, snapshot: dict) -> str:
    owner = mk_session("owner")
    models.register_artifact(integration="local", type="user-raise",
                             external_id=external_id, parent_thread_id=owner)
    # Snapshot lives in the registry metadata (the stable content source).
    models.update_metadata("local", "user-raise", external_id, snapshot)
    return external_id


_GOOD_SNAPSHOT = {"requested_action": "approve budget", "evidence": "Q3 figures",
                  "confidence": "high", "urgency": "high"}


class TestFreshContext:
    def test_no_parent_conversation_context(self, isolated_db):
        _register_user_raise("art-good", _GOOD_SNAPSHOT)
        result = run_review_profile("art-good", profile_id="user-raise-default")
        assert result.parent_conversation_context_injected is False
        assert "parent_conversation" not in result.fresh_context
        assert "parent_context" not in result.fresh_context
        assert result.fresh_context["artifact_snapshot"] == _GOOD_SNAPSHOT
        assert "criteria" in result.fresh_context


class TestStructuredOutput:
    def test_complete_user_raise_recommends_approve(self, isolated_db):
        _register_user_raise("art-good", _GOOD_SNAPSHOT)
        result = run_review_profile("art-good")
        assert result.recommendation == "approve_to_user"
        assert result.blocking_failure is False
        assert result.may_approve_to_user is True

    def test_incomplete_user_raise_blocks_with_findings(self, isolated_db):
        _register_user_raise("art-bad", {"requested_action": "do X"})  # no evidence/confidence
        result = run_review_profile("art-bad")
        assert result.blocking_failure is True
        assert result.may_approve_to_user is False
        assert result.recommendation == "request_revision"
        codes = {f.code for f in result.findings}
        assert "missing_evidence" in codes and "missing_confidence" in codes

    def test_recommendation_is_a_parent_outcome_not_a_delivery_disposition(self, isolated_db):
        _register_user_raise("art-good", _GOOD_SNAPSHOT)
        result = run_review_profile("art-good")
        assert result.recommendation in _PARENT_OUTCOMES
        assert result.recommendation not in DELIVERY_DISPOSITIONS

    def test_findings_render_as_artifact_comments(self, isolated_db):
        _register_user_raise("art-bad", {"requested_action": "do X"})
        result = run_review_profile("art-bad")
        comments = result.comments()
        assert comments and all("body" in c for c in comments)


class TestBindingFromRegistry:
    def test_profile_bound_to_type(self, isolated_db):
        _register_user_raise("art-good", _GOOD_SNAPSHOT)
        result = run_review_profile("art-good")
        assert result.artifact_type == "user-raise"
        assert result.profile_id == "user-raise-default"

    def test_profile_type_mismatch_fails_loud(self, isolated_db):
        owner = mk_session("owner")
        models.register_artifact(integration="github", type="pull-request",
                                 external_id="o/r#1", parent_thread_id=owner)
        models.update_metadata("github", "pull-request", "o/r#1", {"x": 1})
        with pytest.raises(ProfileExecutionError):
            run_review_profile("o/r#1", profile_id="user-raise-default")


class TestFailLoud:
    def test_unregistered_artifact_fails_loud(self, isolated_db):
        with pytest.raises(ProfileExecutionError):
            run_review_profile("nope", profile_id="user-raise-default")

    def test_unknown_profile_fails_loud(self, isolated_db):
        _register_user_raise("art-good", _GOOD_SNAPSHOT)
        with pytest.raises(ProfileExecutionError):
            run_review_profile("art-good", profile_id="does-not-exist")

    def test_no_default_profile_for_type_fails_loud(self, isolated_db):
        owner = mk_session("owner")
        models.register_artifact(integration="local", type="mystery-type",
                                 external_id="m-1", parent_thread_id=owner)
        models.update_metadata("local", "mystery-type", "m-1", {"x": 1})
        with pytest.raises(ProfileExecutionError):
            run_review_profile("m-1")

    def test_empty_snapshot_fails_loud(self, isolated_db):
        owner = mk_session("owner")
        models.register_artifact(integration="local", type="user-raise",
                                 external_id="art-empty", parent_thread_id=owner)
        with pytest.raises(ProfileExecutionError):
            run_review_profile("art-empty")
