"""Real consumed-input refusals and incidental mapper faults stay separate."""
from __future__ import annotations

import logging

import pytest

from events.connectors import github_admission as admission, github_outcomes as outcomes
from test_github_raw_facts_at_admission import _counters, _deliveries, _facts, delivery


CASES = [
    ("pr_retargeted", ("changes",), "bad"),
    ("pr_retargeted", ("changes", "base"), "bad"),
    ("pr_retargeted", ("changes", "base"), ["bad"]),
    ("workflow_run_success", ("workflow_run",), "bad"),
    ("check_suite_success", ("check_suite",), "bad"),
    ("check_suite_success", ("check_suite", "app"), "bad"),
    ("check_suite_success", ("check_suite", "app"), ["bad"]),
    ("issue_opened", ("issue", "labels"), ["bad"]),
    ("issue_opened", ("issue", "labels"), [3]),
    ("issue_opened", ("issue", "labels"), "bad"),
    ("pr_introduced_by_merged", ("pull_request", "merged_at"), None),
    ("workflow_run_success", ("workflow_run", "pull_requests"), [{"number": None}]),
    ("pr_opened", ("pull_request", "user"), "bad"),
    ("pr_introduced_by_merged", ("pull_request", "body"), 123),
    ("pr_opened", ("pull_request", "draft"), "false"),
    ("pr_opened", ("pull_request", "number"), True),
    ("pr_opened", ("pull_request", "head", "sha"), None),
    ("pr_opened", ("pull_request", "created_at"), "yesterday"),
    ("pr_opened", ("repository",), "bad"),
    ("pr_opened", ("repository", "full_name"), None),
    ("workflow_run_success", ("workflow_run", "id"), True),
    ("workflow_run_success", ("workflow_run", "pull_requests"), [5]),
    ("workflow_run_success", ("workflow_run", "pull_requests"), {"number": 1}),
    ("check_suite_success", ("check_suite", "pull_requests"), ["number"]),
    ("check_suite_success", ("check_suite", "pull_requests"), [{}]),
    ("pr_retargeted", ("changes", "base", "ref", "from"), False),
    ("pr_retargeted", ("changes", "base", "ref", "from"), None),
    ("pr_retargeted", ("changes", "base", "ref"), []),
    ("issue_opened", ("issue", "labels"), [{"name": 3}]),
    ("issue_opened", ("issue", "labels"), None),
    ("pr_opened", ("pull_request", "draft"), None),
    ("pr_synchronize", ("after",), False),
    ("pr_opened", ("pull_request", "base"), []),
    ("pr_closed_unmerged", ("pull_request", "merged"), 0),
    ("pr_opened", ("pull_request", "created_at"), False),
    ("check_suite_success", ("check_suite", "updated_at"), None),
]


def malformed(fixture, path, value):
    event_type, payload = delivery(fixture)
    node = payload
    for part in path[:-1]:
        node = node[part]
    node[path[-1]] = value
    return event_type, payload


@pytest.mark.parametrize("fixture,path,value", CASES)
def test_real_malformed_input_has_receipt_one_count_and_no_facts(
    main_db, caplog, fixture, path, value,
):
    event_type, payload = malformed(fixture, path, value)
    # Through the metadata extraction that precedes mapping, then the real store.
    with caplog.at_level(logging.WARNING, logger=admission.log.name):
        result, _ = admission._record_phase(event_type, payload, "d-refused", "webhook")
    assert result is None
    assert _deliveries(main_db) == {"d-refused": "received"}
    assert _facts(main_db) == {}
    assert _counters(main_db) == {("mapping_failed", event_type): 1}
    result, _ = admission._record_phase(event_type, payload, "d-refused", "webhook")
    assert result.content["status"] == "duplicate"
    assert _counters(main_db) == {("mapping_failed", event_type): 1}
    assert _facts(main_db) == {}
    refused = [r.getMessage() for r in caplog.records if "mapping refused" in r.getMessage()]
    assert refused and all(line.endswith("PayloadRefusal") for line in refused)


@pytest.mark.parametrize("fault", [KeyError, TypeError, AttributeError])
def test_incidental_code_fault_propagates_then_same_id_reenters(main_db, monkeypatch, caplog, fault):
    event_type, payload = delivery("pr_opened")
    real = outcomes.map_github_outcomes

    def broken(*args):
        drafts = real(*args)
        assert drafts, "the real mapper accepted valid input before injected code failure"
        raise fault("private payload marker")

    with monkeypatch.context() as patch, caplog.at_level(logging.ERROR, logger=admission.log.name):
        patch.setattr(outcomes, "map_github_outcomes", broken)
        with pytest.raises(fault):
            admission._record_phase(event_type, payload, "d-fault", "webhook")
    assert _deliveries(main_db) == _facts(main_db) == _counters(main_db) == {}
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert fault.__name__ in logged and "private payload marker" not in logged
    admission._record_phase(event_type, payload, "d-fault", "webhook")
    assert _deliveries(main_db) == {"d-fault": "received"}
    assert len(_facts(main_db)) == 1
    assert _counters(main_db) == {}


@pytest.mark.parametrize("fixture,path", [
    ("pr_opened", ("pull_request", "number")),
    ("pr_opened", ("pull_request", "head", "sha")),
    ("pr_opened", ("pull_request", "base", "ref")),
    ("pr_opened", ("pull_request", "created_at")),
    ("pr_synchronize", ("pull_request", "updated_at")),
    ("pr_closed_unmerged", ("pull_request", "closed_at")),
    ("pr_introduced_by_merged", ("pull_request", "merge_commit_sha")),
    ("workflow_run_success", ("workflow_run", "id")),
    ("workflow_run_success", ("workflow_run", "workflow_id")),
    ("workflow_run_success", ("workflow_run", "updated_at")),
    ("check_suite_success", ("check_suite", "updated_at")),
    ("issue_opened", ("issue", "number")),
    ("issue_opened", ("issue", "created_at")),
])
def test_absent_required_field_is_explicit_refusal(main_db, fixture, path):
    event_type, payload = delivery(fixture)
    node = payload
    for part in path[:-1]:
        node = node[part]
    del node[path[-1]]
    admission._record_phase(event_type, payload, "d-missing", "webhook")
    assert _facts(main_db) == {}
    assert _deliveries(main_db) == {"d-missing": "received"}
    assert _counters(main_db) == {("mapping_failed", event_type): 1}


def test_optional_absent_and_null_values_are_not_fabricated():
    event_type, payload = delivery("pr_opened")
    pr = payload["pull_request"]
    pr["body"] = None
    pr["user"] = None
    del pr["draft"]
    (draft,) = outcomes.map_github_outcomes(event_type, payload)
    assert draft.attributes["declaration_count"] == 0
    assert "author_login" not in draft.attributes and "draft" not in draft.attributes
    event_type, payload = delivery("issue_opened")
    del payload["issue"]["labels"]
    (draft,) = outcomes.map_github_outcomes(event_type, payload)
    assert "label_count" not in draft.attributes


@pytest.mark.parametrize("event_type,action", [
    ("workflow_run", "requested"), ("check_suite", "requested"),
])
def test_non_fact_action_does_not_validate_unconsumed_payload(event_type, action):
    assert outcomes.map_github_outcomes(event_type, {"action": action, "repository": 5}) == []


def test_edit_without_base_change_does_not_consume_repository_or_pr(main_db):
    payload = {"action": "edited", "repository": 5, "changes": {"title": {"from": "x"}}}
    assert outcomes.map_github_outcomes("pull_request", payload) == []
    admission._record_phase("pull_request", payload, "d-title-edit", "webhook")
    assert _deliveries(main_db) == {"d-title-edit": "received"}
    assert _facts(main_db) == _counters(main_db) == {}


def test_label_selection_uses_the_allowlists_ordered_canonical_vocabularies():
    from infra.outcome_event_attributes import ATTRIBUTE_ALLOWLISTS, ISSUE_KIND_VALUES, PRIORITY_VALUES

    event_type, payload = delivery("issue_opened")
    payload["issue"]["labels"] = [{"name": value} for value in (*PRIORITY_VALUES, *ISSUE_KIND_VALUES)]
    (draft,) = outcomes.map_github_outcomes(event_type, payload)
    assert draft.attributes["priority"] == PRIORITY_VALUES[0]
    assert draft.attributes["kind"] == ISSUE_KIND_VALUES[0]
    specs = ATTRIBUTE_ALLOWLISTS[draft.event_name]
    assert specs["priority"].values == frozenset(PRIORITY_VALUES)
    assert specs["kind"].values == frozenset(ISSUE_KIND_VALUES)
