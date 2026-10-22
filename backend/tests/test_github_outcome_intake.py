"""GitHub receipt mapping: the pure payload-to-draft mapper (spine §3.2).

Writing the drafts is proven in ``test_github_raw_facts_at_admission.py``.

Fixtures were recorded from ``gh api`` reads of ``cebertowicz/maestro`` and
trimmed to the fields the mapper reads, with logins replaced; the recording
script is not kept in this repository. The only shaped parts are webhook
envelopes (``action``/``changes``) around real objects, noted where a
webhook-only field cannot be recorded from REST.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from events.connectors.github_outcomes import (
    closes_issue_refs,
    introduced_by_numbers,
    map_github_outcomes,
    pr_subject,
)

FIXTURES = Path(__file__).parent / "fixtures/github_outcomes"
REPO = "cebertowicz/maestro"


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text())  # type: ignore[no-any-return]


def drafts_by_name(drafts) -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = {}
    for draft in drafts:
        out.setdefault(draft.event_name, []).append(draft)
    return out


# ---------------------------------------------------------------- mapping


def test_pr_opened_emits_one_deterministic_draft():
    fx = fixture("pr_opened")
    drafts = map_github_outcomes(fx["event"], fx["payload"])
    assert [d.event_name for d in drafts] == ["github.pr.opened"]
    draft = drafts[0]
    pr = fx["payload"]["pull_request"]
    assert draft.dedupe_key == f"github:pr:{REPO}#{pr['number']}:opened"
    assert draft.subject_key == pr_subject(REPO, pr["number"])
    assert draft.ts == pr["created_at"]
    assert draft.attributes["head_ref"] == pr["head"]["ref"]
    assert draft.attributes["draft"] == ("yes" if pr["draft"] else "no")
    assert not {"title", "body"} & set(draft.attributes), "no PR prose is kept"


def test_synchronize_reopen_close_carry_head_stable_keys():
    sync = fixture("pr_synchronize")
    (pushed,) = map_github_outcomes("pull_request", sync["payload"])
    assert pushed.event_name == "github.pr.pushed"
    assert pushed.dedupe_key.endswith(f":head:{sync['payload']['after']}")
    assert pushed.attributes["before_sha"] == sync["payload"]["before"]

    reopened = fixture("pr_reopened")["payload"]
    (draft,) = map_github_outcomes("pull_request", reopened)
    assert (draft.event_name, draft.dedupe_key) == (
        "github.pr.reopened",
        f"github:pr:{REPO}#3315:reopened:{reopened['pull_request']['updated_at']}",
    )

    closed = fixture("pr_closed_unmerged")["payload"]
    (draft,) = map_github_outcomes("pull_request", closed)
    assert draft.event_name == "github.pr.closed"
    assert not any(d.event_name == "github.pr.merged" for d in map_github_outcomes("pull_request", closed))


def test_merged_pr_emits_merged_introduced_by_and_closes():
    merged = fixture("pr_introduced_by_merged")["payload"]
    names = drafts_by_name(map_github_outcomes("pull_request", merged))
    assert set(names) == {"github.pr.merged", "github.pr.introduced_by",
                          "github.pr.closes_issue"}
    introducer = names["github.pr.introduced_by"][0]
    assert introducer.attributes["confidence"] == "stated"
    assert introducer.dedupe_key == (
        f"github:pr:{REPO}#2803:introduced_by:{introducer.attributes['introduced_by_pr']}"
    )
    # the Fixes line is real on #2803, not reconstructed
    (closed_issue,) = names["github.pr.closes_issue"]
    assert closed_issue.attributes["issue"] == "2795"

    closes = fixture("pr_closes_merged")["payload"]
    names = drafts_by_name(map_github_outcomes("pull_request", closes))
    assert set(names) == {"github.pr.merged", "github.pr.closes_issue"}
    ref = names["github.pr.closes_issue"][0]
    assert ref.attributes["issue_repo"] == REPO.lower()
    assert ref.attributes["issue"] == "1627"
    assert ref.attributes["confidence"] == "stated"
    assert ref.dedupe_key == f"github:pr:{REPO}#3191:closes:{REPO.lower()}#1627"


def test_body_parsers_are_order_stable_deduped_and_repo_qualified():
    body = (
        "Fixes #12 and closes #12; resolves: cebertowicz/OtherRepo#7.\n"
        "Closes #3191. Not related: #99 mentioned alone.\n"
    )
    assert closes_issue_refs(body, REPO) == [
        (REPO.lower(), 12), ("cebertowicz/otherrepo", 7), (REPO.lower(), 3191),
    ]
    assert introduced_by_numbers("Introduced by #5\n> introduced by #6\nIntroduced by #5") == [5, 6]


def test_merged_pr_measurement_attributes_are_optional_and_exact():
    payload = copy.deepcopy(fixture("pr_introduced_by_merged")["payload"])
    pr = payload["pull_request"]
    pr["created_at"] = "2026-09-28T10:00:00Z"
    pr["merged_at"] = "2026-09-28T12:34:00Z"
    primary = drafts_by_name(map_github_outcomes("pull_request", payload))["github.pr.merged"][0]
    assert primary.attributes["minutes_since_opened"] == 154

    del pr["created_at"]
    primary = drafts_by_name(map_github_outcomes("pull_request", payload))["github.pr.merged"][0]
    assert "minutes_since_opened" not in primary.attributes

    pr["created_at"] = "2026-09-28T13:00:00Z"
    primary = drafts_by_name(map_github_outcomes("pull_request", payload))["github.pr.merged"][0]
    assert "minutes_since_opened" not in primary.attributes


_REVERT_SHA = "a" * 40


@pytest.mark.parametrize(("title", "body", "expected"), [
    ('Revert "change"', f"This reverts commit {_REVERT_SHA}.", _REVERT_SHA),
    ('Revert "change"', "This reverts commit a change.", None),
    ('Revert "change"', f"This reverts commit {'a' * 39}.", None),
    ("Normal change", f"This reverts commit {_REVERT_SHA}.", None),
])
def test_merged_pr_revert_sha_requires_local_git_revert_template(title, body, expected):
    payload = copy.deepcopy(fixture("pr_introduced_by_merged")["payload"])
    payload["pull_request"].update(title=title, body=body)
    primary = drafts_by_name(map_github_outcomes("pull_request", payload))["github.pr.merged"][0]
    assert primary.attributes.get("reverts") == expected


@pytest.mark.parametrize(("title", "body", "expected"), [
    ('Revert "change"', f"Reverts {REPO.upper()}#42", "42"),
    ('Revert "change"', "Reverts other/repo#42", None),
    ("Normal change", f"Reverts {REPO}#42", None),
])
def test_merged_pr_revert_button_attribute_requires_same_repo(title, body, expected):
    payload = copy.deepcopy(fixture("pr_introduced_by_merged")["payload"])
    payload["pull_request"].update(title=title, body=body)
    primary = drafts_by_name(map_github_outcomes("pull_request", payload))["github.pr.merged"][0]
    assert primary.attributes.get("reverts_pr") == expected


def test_workflow_run_key_is_run_id_plus_attempt():
    first = fixture("workflow_run_success")
    (draft,) = map_github_outcomes(first["event"], first["payload"])
    run = first["payload"]["workflow_run"]
    assert draft.dedupe_key == f"github:workflow_run:{REPO}:{run['id']}:attempt:1"
    assert draft.subject_key == f"github:commit:{REPO}@{run['head_sha']}"
    assert draft.attributes["conclusion"] == "success"
    assert draft.attributes["attempt"] == 1

    attempt2 = fixture("workflow_run_attempt2")
    (retry,) = map_github_outcomes(attempt2["event"], attempt2["payload"])
    run2 = attempt2["payload"]["workflow_run"]
    assert retry.dedupe_key == f"github:workflow_run:{REPO}:{run2['id']}:attempt:2"
    assert retry.attributes["conclusion"] == "success"

    failure = fixture("workflow_run_failure")["payload"]
    (red,) = map_github_outcomes("workflow_run", failure)
    assert red.attributes["conclusion"] == "failure"
    assert red.attributes["event"] == "workflow_dispatch"



_MALFORMED_ATTEMPTS: list[Any] = [
    None, 0, -1, "", "  ", "1", "one", 1.5, True, False, [], {},
]


@pytest.mark.parametrize("bad", _MALFORMED_ATTEMPTS)
def test_a_present_invalid_attempt_is_refused_rather_than_defaulted(bad):
    """``attempt`` is half of the run's dedupe key, so a made-up one is a made-up fact.

    GitHub's Actions schema types ``run_attempt`` as an integer and does not list it as
    required (read 2026-10-02 from ``rest/actions/workflow-runs``): a payload that omits it
    is a run that was never re-attempted, while a payload carrying null, zero, a negative,
    a float, a bool, a string or a container states something no run does. The first maps;
    the rest are refused through the band the mapper already documents for a wrong-shaped
    field. Defaulting any of them to 1 would file that run's real first attempt as
    ``duplicate_ignored`` against an identity it never had.
    """
    payload = copy.deepcopy(fixture("workflow_run_attempt2")["payload"])
    payload["workflow_run"]["run_attempt"] = bad
    from events.connectors.github_outcome_input import PayloadRefusal

    with pytest.raises(PayloadRefusal):
        map_github_outcomes("workflow_run", payload)


def test_an_omitted_attempt_is_the_first_attempt():
    """Omission retains compatibility default 1 without proving historical attempts."""
    payload = copy.deepcopy(fixture("workflow_run_attempt2")["payload"])
    del payload["workflow_run"]["run_attempt"]
    (draft,) = map_github_outcomes("workflow_run", payload)
    run = payload["workflow_run"]
    assert draft.dedupe_key == f"github:workflow_run:{REPO}:{run['id']}:attempt:1"
    assert draft.attributes["attempt"] == 1


@pytest.mark.parametrize("valid", [1, 2, 3, 10])
def test_every_valid_attempt_keys_the_run_itself(valid):
    """No re-numbering of identity: the key carries the attempt the payload reported."""
    payload = copy.deepcopy(fixture("workflow_run_attempt2")["payload"])
    payload["workflow_run"]["run_attempt"] = valid
    (draft,) = map_github_outcomes("workflow_run", payload)
    run = payload["workflow_run"]
    assert draft.dedupe_key == f"github:workflow_run:{REPO}:{run['id']}:attempt:{valid}"
    assert draft.attributes["attempt"] == valid


def test_check_suite_still_maps_as_supplemental():
    fx = fixture("check_suite_success")
    suite = fx["payload"]["check_suite"]
    (draft,) = map_github_outcomes("check_suite", fx["payload"])
    assert draft.dedupe_key == (
        f"github:check_suite:{REPO}:{suite['id']}:completed:{suite['updated_at']}"
    )
    assert draft.attributes["conclusion"] == suite["conclusion"]
    incomplete = map_github_outcomes("check_suite", {"action": "requested", "repository": {"full_name": REPO}, "check_suite": {}})
    assert incomplete == []


def test_issue_mapping_carries_scalar_label_facts():
    opened = fixture("issue_opened")["payload"]
    (draft,) = map_github_outcomes("issues", opened)
    assert draft.event_name == "github.issue.opened"
    assert draft.dedupe_key == f"github:issue:{REPO}#3328:opened"
    assert draft.attributes["label_count"] >= 1

    for name, reason in (("issue_closed_completed", "completed"),
                         ("issue_closed_not_planned", "not_planned")):
        payload = fixture(name)["payload"]
        (draft,) = map_github_outcomes("issues", payload)
        assert draft.attributes["state_reason"] == reason
    unlabeled = map_github_outcomes("issues", {
        "action": "labeled", "repository": {"full_name": REPO}, "issue": {"number": 1},
    })
    assert unlabeled == []


def test_retarget_only_fires_on_a_base_change():
    fx = fixture("pr_retargeted")
    retargeted = [d for d in map_github_outcomes(fx["event"], fx["payload"])
                  if d.event_name == "github.pr.retargeted"]
    (draft,) = retargeted
    pr = fx["payload"]["pull_request"]
    assert draft.attributes["base_from"] == "dev"
    assert draft.attributes["base_ref"] == pr["base"]["ref"]
    assert draft.dedupe_key == f"github:pr:{REPO}#2778:retargeted:{pr['updated_at']}"

    no_base = copy.deepcopy(fx["payload"])
    no_base["changes"] = {"title": {"from": "x"}}
    assert [d for d in map_github_outcomes("pull_request", no_base)
            if d.event_name == "github.pr.retargeted"] == []
