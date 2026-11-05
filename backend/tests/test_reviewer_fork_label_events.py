"""The review label reaches the gate through the mapper, the manifest and ingress."""

from __future__ import annotations

import asyncio
import subprocess
from unittest.mock import patch

from reviewer.reviewer_categories import LABEL_REVIEW


def _labeled(name: str) -> dict:
    return {
        "action": "labeled",
        "label": {"name": name},
        "repository": {"full_name": "o/r"},
        "pull_request": {"number": 7, "head": {"sha": "abc123"}},
    }


def test_the_review_label_maps_to_a_gate_event() -> None:
    from events.github_mapper import map_github_event

    assert map_github_event("pull_request", _labeled(LABEL_REVIEW)) == "webhook.github.pr_labeled"


def test_removing_the_label_is_no_gate_trigger() -> None:
    from events.github_mapper import map_github_event
    from events.reviewer_route import GATE_TRIGGER_EVENTS

    event = map_github_event("pull_request", {**_labeled(LABEL_REVIEW), "action": "unlabeled"})
    assert event == "webhook.github.pr_unlabeled" and event not in GATE_TRIGGER_EVENTS


def test_any_other_label_approves_no_fork_head() -> None:
    from events.github_mapper import map_github_event
    from events.ingress import handle_event

    assert map_github_event("pull_request", {"action": "labeled", "label": None}) is None
    event = map_github_event("pull_request", _labeled("bug"))
    assert event == "webhook.github.pr_labeled"

    async def fake_route(internal_event, payload, delivery_id=None):
        return None

    with patch("events.ingress.route_leg", fake_route), \
         patch("events.connectors.github_reviewer_fork_hold.approve_labeled_head") as approve:
        asyncio.run(handle_event(event, _labeled("bug")))
    approve.assert_not_called()


def test_the_label_event_is_a_gate_trigger_that_does_not_wake_the_owner() -> None:
    from events.connectors.github import MANIFEST
    from events.disposition import disposition
    from events.identity import resolve_identity
    from events.reviewer_route import GATE_TRIGGER_EVENTS

    payload = _labeled(LABEL_REVIEW)
    event_type = MANIFEST.classify(payload)

    assert event_type == "pr.labeled"
    assert disposition("owner", MANIFEST.decl(event_type).event_class) == "digest"
    assert "webhook.github.pr_labeled" in GATE_TRIGGER_EVENTS
    assert resolve_identity("webhook.github.pr_labeled", payload).external_id == "o/r#7"


def test_ingress_runs_the_label_gate_leg_off_the_loop() -> None:
    from events.ingress import handle_event

    async def fake_route(internal_event, payload, delivery_id=None):
        return None

    with patch("events.ingress.route_leg", fake_route), \
         patch("events.connectors.github_reviewer_fork_hold.approve_labeled_head") as approve:
        result = asyncio.run(handle_event("webhook.github.pr_labeled", _labeled(LABEL_REVIEW)))

    assert result["reviewer_gate"] == "evaluated"
    assert approve.call_args.args[:3] == ("o/r", 7, "abc123")


def _gh_result(stdout: str = "", returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_only_a_login_with_write_access_can_approve() -> None:
    from reviewer import gh_fork_hold

    for permission, allowed in (("admin", True), ("write", True), ("read", False), ("none", False)):
        with patch("reviewer.gh_fork_hold.run_gh", return_value=_gh_result(permission + "\n")) as run:
            assert gh_fork_hold.sender_can_approve("o/r", "alice") is allowed
        assert run.call_args.args[0][1] == "repos/o/r/collaborators/alice/permission"

    with patch("reviewer.gh_fork_hold.run_gh", return_value=_gh_result(returncode=1, stderr="HTTP 404")):
        assert gh_fork_hold.sender_can_approve("o/r", "alice") is False
    with patch("reviewer.gh_fork_hold.run_gh") as run:
        assert gh_fork_hold.sender_can_approve("o/r", None) is False
        assert gh_fork_hold.sender_can_approve("o/r", "../x") is False
    run.assert_not_called()


def test_consuming_an_absent_label_counts_as_removed() -> None:
    from reviewer import gh_fork_hold

    with patch("reviewer.gh_fork_hold.run_gh", return_value=_gh_result(returncode=1, stderr="HTTP 404")) as run:
        assert gh_fork_hold.remove_review_label("o/r", 7) is True
    assert run.call_args.args[0] == ["api", "-X", "DELETE", f"repos/o/r/issues/7/labels/{LABEL_REVIEW}"]

    with patch("reviewer.gh_fork_hold.run_gh", return_value=_gh_result(returncode=1, stderr="HTTP 500")):
        assert gh_fork_hold.remove_review_label("o/r", 7) is False


def test_pr_info_names_both_repositories_and_none_for_a_deleted_fork() -> None:
    import json

    from reviewer.gh_helpers import get_pull_request_info

    rest = {
        "number": 7, "mergeable": True, "draft": False,
        "base": {"ref": "dev", "sha": "b" * 40, "repo": {"full_name": "o/r"}},
        "head": {"sha": "a" * 40, "repo": None},
    }
    with patch("reviewer.gh_helpers._run_gh", return_value=json.dumps(rest)):
        info = get_pull_request_info("o/r", 7)

    assert (info["baseRepoFullName"], info["headRepoFullName"]) == ("o/r", None)
