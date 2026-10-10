"""Optional body mentions cannot erase a valid provider lifecycle fact."""
from __future__ import annotations

import json
import sys

import pytest

from events.connectors import github_admission as admission, github_outcomes as outcomes
from events.connectors import github_outcome_input as shape
from test_github_raw_facts_at_admission import _counters, _deliveries, _facts, delivery


def oversized_decimal() -> str:
    limit = sys.get_int_max_str_digits()
    assert limit > 0, "this proof requires Python's active decimal conversion limit"
    return "9" * (limit + 1)


def body_case(case: str) -> tuple[list[str], list[int]]:
    if case == "zero":
        return ["0"], []
    if case == "double-zero":
        return ["00"], []
    if case == "oversized":
        return [oversized_decimal()], []
    if case == "leading-zero":
        return ["0007"], [7]
    if case == "mixed":
        return ["0", "7", "00", "0007", oversized_decimal(), "8", "8"], [7, 8]
    raise AssertionError(case)


def merged_delivery(grammar: str, references: list[str]):
    event_type, payload = delivery("pr_introduced_by_merged")
    payload["pull_request"]["body"] = "Untrusted optional declaration text\n" + "\n".join(
        f"{grammar} #{number}" for number in references
    )
    return event_type, payload


def stored_rows(db):
    return [
        (row[0], row[1], row[2], row[3], json.loads(row[4]))
        for row in db.execute(
            "SELECT dedupe_key,event_name,ts,subject_key,attributes "
            "FROM outcome_events ORDER BY dedupe_key"
        )
    ]


@pytest.mark.parametrize("case", ["zero", "double-zero", "oversized", "leading-zero", "mixed"])
@pytest.mark.parametrize("grammar", ["Introduced by", "Fixes"])
def test_optional_mentions_preserve_primary_mapping(grammar, case):
    references, expected_numbers = body_case(case)
    event_type, payload = merged_delivery(grammar, references)
    drafts = outcomes.map_github_outcomes(event_type, payload)
    primary, *relations = drafts
    repo = payload["repository"]["full_name"].lower()
    pr = payload["pull_request"]
    assert primary.event_name == "github.pr.merged"
    assert primary.subject_key == f"github:pr:{repo}#{pr['number']}"
    assert primary.ts == pr["merged_at"]
    assert primary.attributes["merge_sha"] == pr["merge_commit_sha"]
    attribute = "introduced_by_pr" if grammar == "Introduced by" else "issue"
    event_name = "github.pr.introduced_by" if grammar == "Introduced by" else "github.pr.closes_issue"
    assert [row.attributes[attribute] for row in relations] == [str(n) for n in expected_numbers]
    assert all(row.event_name == event_name and row.attributes["confidence"] == "stated"
               for row in relations)
    assert all("body" not in row.attributes for row in drafts)


@pytest.mark.parametrize("case", ["zero", "double-zero", "oversized", "leading-zero", "mixed"])
@pytest.mark.parametrize("grammar", ["Introduced by", "Fixes"])
def test_optional_mentions_survive_real_admission_store_and_duplicates(main_db, grammar, case):
    references, expected_numbers = body_case(case)
    event_type, payload = merged_delivery(grammar, references)
    result, _ = admission._record_phase(event_type, payload, "d-optional", "webhook")
    assert result is None
    rows = stored_rows(main_db)
    primary = [row for row in rows if row[1] == "github.pr.merged"]
    assert len(primary) == 1, "unusable optional text must not erase the real merge fact"
    pr = payload["pull_request"]
    repo = payload["repository"]["full_name"].lower()
    subject = f"github:pr:{repo}#{pr['number']}"
    expected_ts = pr["merged_at"].replace("Z", "+00:00")
    assert primary[0][:4] == (f"{subject}:merged", "github.pr.merged", expected_ts, subject)
    assert primary[0][4]["merge_sha"] == pr["merge_commit_sha"]
    attribute = "introduced_by_pr" if grammar == "Introduced by" else "issue"
    relations = [row for row in rows if row[1] != "github.pr.merged"]
    assert len(rows) == 1 + len(expected_numbers)
    assert {row[4][attribute] for row in relations} == {str(n) for n in expected_numbers}
    assert all(row[4]["confidence"] == "stated" for row in relations)
    assert _deliveries(main_db) == {"d-optional": "received"}
    assert set(_facts(main_db).values()) == {(None, None, None)}
    assert _counters(main_db) == {}
    assert "Untrusted optional declaration text" not in json.dumps(rows)
    result, _ = admission._record_phase(event_type, payload, "d-optional", "webhook")
    assert result.content["status"] == "duplicate"
    assert stored_rows(main_db) == rows
    assert _deliveries(main_db) == {"d-optional": "received"}
    assert _counters(main_db) == {}


def test_cross_repository_mixed_references_keep_order_and_deduplicate():
    body = "Closes #0; Fixes ACME/OTHER#007; Resolves #8; Fixes acme/other#007; Fixes #00"
    assert outcomes.closes_issue_refs(body, "ACME/HOME") == [("acme/other", 7), ("acme/home", 8)]
    assert outcomes.introduced_by_numbers(
        "Introduced by #0\n> introduced by #007\nIntroduced by #8\nIntroduced by #007"
    ) == [7, 8]


@pytest.mark.parametrize("grammar", ["Introduced by", "Fixes"])
@pytest.mark.parametrize("fault", [KeyError, TypeError, ValueError])
def test_incidental_reference_parser_fault_aborts_and_same_id_recovers(
    main_db, monkeypatch, grammar, fault,
):
    event_type, payload = merged_delivery(grammar, ["7"])

    def broken(value):
        raise fault("incidental reference parser failure")

    with monkeypatch.context() as patch:
        patch.setattr(shape, "reference_number", broken)
        with pytest.raises(fault):
            admission._record_phase(event_type, payload, "d-parser", "webhook")
    assert _deliveries(main_db) == _facts(main_db) == _counters(main_db) == {}
    result, _ = admission._record_phase(event_type, payload, "d-parser", "webhook")
    assert result is None
    rows = stored_rows(main_db)
    assert len(rows) == 2
    assert {row[1] for row in rows} >= {"github.pr.merged"}
    assert _counters(main_db) == {}
    result, _ = admission._record_phase(event_type, payload, "d-parser", "webhook")
    assert result.content["status"] == "duplicate"
    assert stored_rows(main_db) == rows


@pytest.mark.parametrize("event_type", ["pull_request", "workflow_run", "check_suite", "issues"])
def test_unsupported_actions_do_not_consume_invalid_incidental_fields(event_type):
    assert outcomes.map_github_outcomes(event_type, {
        "action": "unmapped-action", "repository": None,
        "pull_request": None, "workflow_run": None, "check_suite": None, "issue": None,
    }) == []


@pytest.mark.parametrize("changes", [{}, {"title": {"from": "old"}}, {"base": {"sha": {"from": "old"}}}])
def test_edited_without_base_ref_change_does_not_consume_pr_or_repository(changes):
    assert outcomes.map_github_outcomes("pull_request", {
        "action": "edited", "changes": changes, "repository": None, "pull_request": None,
    }) == []
