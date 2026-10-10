"""GitHub's supported retarget shape and returned scalar vocabularies reach the store."""
from __future__ import annotations

import pytest

from events.connectors import github_admission as admission, github_outcomes as outcomes
from test_github_raw_facts_at_admission import (
    _attributes, _counters, _deliveries, delivery,
)


def test_canonical_retarget_is_stored_with_its_original_identity(main_db):
    event_type, payload = delivery("pr_retargeted")
    (draft,) = outcomes.map_github_outcomes(event_type, payload)
    pr = payload["pull_request"]
    repo = payload["repository"]["full_name"].lower()
    assert draft.dedupe_key == f"github:pr:{repo}#{pr['number']}:retargeted:{pr['updated_at']}"
    admission._record_phase(event_type, payload, "d-retarget", "webhook")
    assert _attributes(main_db)[draft.dedupe_key]["base_from"] == "dev"
    assert _counters(main_db) == {}


@pytest.mark.parametrize("base_change", [{"from": "dev"}, {"from": {"ref": "dev"}}])
def test_unsupported_base_from_does_not_emit_or_consume_fact_fields(main_db, base_change):
    # GitHub's edited schema defines changes.base.ref.from, not changes.base.from.
    payload = {"action": "edited", "changes": {"base": base_change}, "repository": 5}
    assert outcomes.map_github_outcomes("pull_request", payload) == []
    admission._record_phase("pull_request", payload, "d-unsupported", "webhook")
    assert _deliveries(main_db) == {"d-unsupported": "received"}
    assert _attributes(main_db) == _counters(main_db) == {}


@pytest.mark.parametrize("ref_change", [{}, [], {"from": None}, {"from": False}])
def test_consumed_canonical_retarget_shape_remains_a_refusal(main_db, ref_change):
    event_type, payload = delivery("pr_retargeted")
    payload["changes"]["base"]["ref"] = ref_change
    with pytest.raises(admission.PayloadRefusal):
        outcomes.map_github_outcomes(event_type, payload)
    admission._record_phase(event_type, payload, "d-refused-retarget", "webhook")
    assert _deliveries(main_db) == {"d-refused-retarget": "received"}
    assert _attributes(main_db) == {}
    assert _counters(main_db) == {("mapping_failed", "pull_request"): 1}


@pytest.mark.parametrize("fixture,object_key,event_name", [
    ("workflow_run_success", "workflow_run", "github.workflow_run.completed"),
    ("check_suite_success", "check_suite", "github.check_suite.completed"),
])
@pytest.mark.parametrize("conclusion", ["startup_failure", "action_required"])
def test_provider_conclusion_survives_real_mapping_and_store(
    main_db, fixture, object_key, event_name, conclusion,
):
    event_type, payload = delivery(fixture)
    payload[object_key]["conclusion"] = conclusion
    admission._record_phase(event_type, payload, "d-conclusion", "webhook")
    (stored,) = _attributes(main_db).values()
    assert stored["conclusion"] == conclusion
    assert _counters(main_db) == {}
    assert main_db.execute("SELECT event_name FROM outcome_events").fetchone()[0] == event_name


@pytest.mark.parametrize("fixture,object_key,event_name", [
    ("workflow_run_success", "workflow_run", "github.workflow_run.completed"),
    ("check_suite_success", "check_suite", "github.check_suite.completed"),
])
def test_misspelled_conclusion_is_dropped_and_counted(main_db, fixture, object_key, event_name):
    event_type, payload = delivery(fixture)
    payload[object_key]["conclusion"] = "actions_required"
    admission._record_phase(event_type, payload, "d-misspelling", "webhook")
    (stored,) = _attributes(main_db).values()
    assert "conclusion" not in stored
    assert _counters(main_db) == {("attribute_dropped", event_name): 1}
    assert _deliveries(main_db) == {"d-misspelling": "received"}


@pytest.mark.parametrize("reason", ["duplicate", "reopened"])
def test_issue_reason_survives_without_narrowing_existing_vocabulary(main_db, reason):
    event_type, payload = delivery("issue_closed_completed")
    payload["issue"]["state_reason"] = reason
    admission._record_phase(event_type, payload, "d-issue-reason", "webhook")
    (stored,) = _attributes(main_db).values()
    assert stored["state_reason"] == reason
    assert _counters(main_db) == {}
