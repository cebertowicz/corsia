"""The review label maps and reaches the gate through normal GitHub routing."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

from reviewer.reviewer_categories import LABEL_REVIEW


def _labeled(name: str) -> dict:
    return {
        "action": "labeled",
        "label": {"name": name},
        "repository": {"full_name": "o/r"},
        "pull_request": {"number": 7, "head": {"sha": "abc123"}},
    }



def test_review_label_maps_to_gate_event_and_manifest() -> None:
    from events.connectors.github import MANIFEST
    from events.github_mapper import map_github_event
    from events.identity import resolve_identity
    from events.reviewer_route import GATE_TRIGGER_EVENTS

    payload = _labeled(LABEL_REVIEW)
    assert map_github_event("pull_request", payload) == "webhook.github.pr_labeled"
    assert MANIFEST.classify(payload) == "pr.labeled"
    assert "webhook.github.pr_labeled" in GATE_TRIGGER_EVENTS
    assert resolve_identity("webhook.github.pr_labeled", payload).external_id == "o/r#7"


def test_non_review_labels_and_unlabel_events_remain_unmapped() -> None:
    from events.github_mapper import map_github_event

    assert map_github_event("pull_request", _labeled("bug")) is None
    assert map_github_event("pull_request", {**_labeled(LABEL_REVIEW), "action": "unlabeled"}) is None


def test_ingress_offloads_label_gate_evaluation() -> None:
    from events.ingress import handle_event

    async def fake_route(internal_event, payload, delivery_id=None):
        return None

    with patch("events.ingress.route_leg", fake_route), \
         patch("events.ingress.gate_trigger_leg", return_value=True) as gate:
        result = asyncio.run(handle_event("webhook.github.pr_labeled", _labeled(LABEL_REVIEW)))

    assert result["reviewer_gate"] == "evaluated"
    gate.assert_called_once()



def test_action_required_runs_use_documented_list_and_approve_endpoints() -> None:
    from reviewer.gh_helpers import approve_action_required_workflow_runs

    with patch(
        "reviewer.gh_helpers._run_gh", side_effect=["11\n22\n", "", ""],
    ) as gh:
        assert approve_action_required_workflow_runs("o/r", "abc123")

    assert [call.args[0] for call in gh.call_args_list] == [
        [
            "api", "--method", "GET", "repos/o/r/actions/runs",
            "-f", "head_sha=abc123", "-f", "status=action_required",
            "--paginate", "--jq", ".workflow_runs[].id",
        ],
        ["api", "--method", "POST", "repos/o/r/actions/runs/11/approve"],
        ["api", "--method", "POST", "repos/o/r/actions/runs/22/approve"],
    ]


def test_deleted_fork_repository_identity_survives_pr_info_mapping() -> None:
    from reviewer.gh_helpers import get_pull_request_info

    rest = {
        "number": 7, "mergeable": True, "draft": False,
        "base": {"ref": "dev", "sha": "b" * 40, "repo": {"full_name": "o/r"}},
        "head": {"sha": "a" * 40, "repo": None},
    }
    with patch("reviewer.gh_helpers._run_gh", return_value=json.dumps(rest)):
        info = get_pull_request_info("o/r", 7)

    assert (info["baseRepoFullName"], info["headRepoFullName"]) == ("o/r", None)
