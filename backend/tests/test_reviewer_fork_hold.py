"""Fork pull requests wait for explicit, exact-head maintainer approval."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from reviewer_gate_fixtures import (  # noqa: F401
    _DEFAULT_PR_INFO,
    _call_gate,
    _reload_gate,
)

from reviewer.reviewer_categories import LABEL_REVIEW, STATUS_PRESENCE_CONTEXT

FORK_INFO = {**_DEFAULT_PR_INFO, "headRepoFullName": "contributor/repo"}
PR_KEY = "owner/repo#42"


def _payload(action: str, sha: str, head_repo: object = "contributor/repo", sender: str = "maintainer") -> dict:
    return {
        "action": action,
        "repository": {"full_name": "owner/repo"},
        "pull_request": {
            "number": 42,
            "head": {"sha": sha, "repo": {"full_name": head_repo} if head_repo else None},
            "base": {"ref": "dev", "repo": {"full_name": "owner/repo"}},
        },
        "label": {"name": LABEL_REVIEW},
        "sender": {"login": sender},
    }


def _run_green_gate(gate_mod, pr_info: dict, sha: str = "abc123def"):
    with patch.object(gate_mod.github_reviewer_gate, "checks_green", return_value=True), \
         patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info_awaiting_mergeability", return_value=pr_info), \
         patch.object(gate_mod, "emit_product_event"), \
         patch("reviewer.reviewer_status.set_status") as status, \
         patch.object(gate_mod, "_run_reviewer") as review:
        _call_gate(gate_mod, head_sha=sha)
    return review, status


def _label(gate_mod, payload: dict, pr_info: dict, *, workflow_approved: bool = True, green: bool = True):
    from events.reviewer_route import check_gate_on_trigger

    with patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info", return_value=pr_info) as fetch, \
         patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info_awaiting_mergeability") as second_fetch, \
         patch.object(gate_mod.github_reviewer_gate, "checks_green", return_value=green), \
         patch("reviewer.gh_helpers.approve_action_required_workflow_runs", return_value=workflow_approved) as approve_runs, \
         patch("reviewer.reviewer_status.set_status") as status, \
         patch.object(gate_mod, "emit_product_event"), \
         patch.object(gate_mod, "_run_reviewer") as review:
        handled = check_gate_on_trigger("webhook.github.pr_labeled", payload)
    return SimpleNamespace(
        handled=handled, fetch=fetch, second_fetch=second_fetch,
        approve_runs=approve_runs, status=status, review=review,
    )


def test_green_fork_head_waits_without_invoking_the_reviewer(_reload_gate):
    review, status = _run_green_gate(_reload_gate, FORK_INFO)

    review.assert_not_called()
    status.assert_called_once_with(
        "owner/repo", "abc123def", "pending", "fork head awaiting maintainer review label",
        context=STATUS_PRESENCE_CONTEXT,
    )


@pytest.mark.parametrize("head_repo", [None, "other/repo"])
def test_missing_or_different_head_repository_is_a_fork(_reload_gate, head_repo):
    review, status = _run_green_gate(
        _reload_gate, {**_DEFAULT_PR_INFO, "headRepoFullName": head_repo},
    )

    review.assert_not_called()
    status.assert_called_once()


def test_same_repo_head_keeps_existing_reviewer_behavior(_reload_gate):
    review, status = _run_green_gate(
        _reload_gate, {**_DEFAULT_PR_INFO, "headRepoFullName": "OWNER/REPO"},
    )

    review.assert_called_once()
    status.assert_not_called()


def test_action_required_fork_run_publishes_the_hold(_reload_gate):
    gate_mod = _reload_gate
    with patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info", return_value=FORK_INFO), \
         patch("reviewer.reviewer_status.set_status") as status:
        _call_gate(gate_mod, conclusion="action_required")

    status.assert_called_once_with(
        "owner/repo", "abc123def", "pending", "fork head awaiting maintainer review label",
        context=STATUS_PRESENCE_CONTEXT,
    )


def test_review_label_approves_workflows_and_the_exact_payload_head(_reload_gate):
    out = _label(_reload_gate, _payload("labeled", "abc123def"), FORK_INFO)

    assert out.handled
    out.fetch.assert_called_once_with("owner/repo", 42)
    out.second_fetch.assert_not_called()

    out.approve_runs.assert_called_once_with("owner/repo", "abc123def")
    out.review.assert_called_once()
    assert _reload_gate.reviewer_gate_state.get_approved_sha(PR_KEY) == "abc123def"


def test_labeled_old_head_cannot_release_the_current_fork_head(_reload_gate):
    current = {**FORK_INFO, "headRefOid": "newhead222"}
    out = _label(_reload_gate, _payload("labeled", "abc123def"), current)

    out.review.assert_not_called()
    assert _reload_gate.reviewer_gate_state.get_approved_sha(PR_KEY) == "abc123def"
    held_review, held_status = _run_green_gate(_reload_gate, current, "newhead222")
    held_review.assert_not_called()
    held_status.assert_called_once()


def test_workflow_approval_failure_leaves_head_unapproved(_reload_gate):
    out = _label(
        _reload_gate, _payload("labeled", "abc123def"), FORK_INFO, workflow_approved=False,
    )

    out.review.assert_not_called()
    assert _reload_gate.reviewer_gate_state.get_approved_sha(PR_KEY) is None
    assert out.status.call_args.args[3] == "could not approve fork workflow runs; add review again"



def test_fork_synchronize_removes_review_label_even_for_a_bot(_reload_gate):
    from events.reviewer_route import run_review

    bot_payload = _payload("synchronize", "newhead222")
    bot_payload["sender"] = {"login": "autofix[bot]", "type": "Bot"}
    with patch("reviewer.gh_helpers.remove_review_label", return_value=True) as remove:
        run_review("webhook.github.pr_synchronize", bot_payload)

    remove.assert_called_once_with("owner/repo", 42)


def test_same_repo_synchronize_does_not_remove_review_label(_reload_gate):
    from events.reviewer_route import run_review

    with patch("reviewer.gh_helpers.remove_review_label") as remove:
        run_review("webhook.github.pr_synchronize", _payload("synchronize", "newhead222", "owner/repo"))

    remove.assert_not_called()
