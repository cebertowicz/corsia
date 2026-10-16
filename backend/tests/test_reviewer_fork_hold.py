"""The reviewer holds a fork head until a maintainer approves that exact head."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from reviewer_gate_fixtures import (  # noqa: F401
    _DEFAULT_PR_INFO,
    _call_gate,
    _disable_analytics,
    _reload_gate,
    _stub_presence,
    _stub_presence_absent,
)

from reviewer.reviewer_categories import LABEL_REVIEW, STATUS_PRESENCE_CONTEXT

WAITING = "waiting for a maintainer to add the review label"
FORK_INFO = {**_DEFAULT_PR_INFO, "headRepoFullName": "contributor/repo"}


def _payload(action: str, sha: str, head_repo: object = "contributor/repo", sender: str = "alice") -> dict:
    head = {"sha": sha, "repo": {"full_name": head_repo} if head_repo else None}
    return {
        "action": action,
        "repository": {"full_name": "owner/repo"},
        "pull_request": {
            "number": 42, "title": "A change", "head": head,
            "base": {"ref": "dev", "repo": {"full_name": "owner/repo"}}, "labels": [],
        },
        "label": {"name": LABEL_REVIEW},
        "sender": {"login": sender, "type": "User"},
    }


def _green(gate_mod, pr_info):
    return (
        patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info", return_value=pr_info),
        patch.object(gate_mod.github_reviewer_gate, "checks_green", return_value=True),
        patch.object(gate_mod, "emit_product_event"),
    )


def _evaluate(gate_mod, pr_info, status, run=None):
    fetch, checks, emit = _green(gate_mod, pr_info)
    with fetch, checks, emit, patch("reviewer.reviewer_status.set_status", status), \
         patch.object(gate_mod, "_run_reviewer") as review:
        _call_gate(gate_mod, head_sha=pr_info["headRefOid"])
    return review


def test_a_fork_head_with_green_checks_waits_instead_of_firing(_reload_gate):
    with patch("reviewer.reviewer_status.set_status") as status:
        review = _evaluate(_reload_gate, FORK_INFO, status)

    review.assert_not_called()
    status.assert_called_once_with(
        "owner/repo", "abc123def", "pending", WAITING, context=STATUS_PRESENCE_CONTEXT,
    )


@pytest.mark.parametrize("head_repo", [None, "OWNER/Other"])
def test_a_deleted_or_different_fork_is_held(_reload_gate, head_repo):
    info = {**_DEFAULT_PR_INFO, "headRepoFullName": head_repo}
    with patch("reviewer.reviewer_status.set_status") as status:
        review = _evaluate(_reload_gate, info, status)

    review.assert_not_called()
    status.assert_called_once()


def test_a_pr_info_without_repo_identity_is_held(_reload_gate):
    info = {k: v for k, v in _DEFAULT_PR_INFO.items() if not k.endswith("RepoFullName")}
    with patch("reviewer.reviewer_status.set_status") as status:
        review = _evaluate(_reload_gate, info, status)

    review.assert_not_called()


def test_a_same_repo_head_fires_as_before(_reload_gate):
    with patch("reviewer.reviewer_status.set_status") as status:
        review = _evaluate(_reload_gate, {**_DEFAULT_PR_INFO, "headRepoFullName": "Owner/Repo"}, status)

    review.assert_called_once()
    status.assert_not_called()


def _label(gate_mod, payload, pr_info):
    from events.reviewer_route import check_gate_on_trigger

    fetch, checks, emit = _green(gate_mod, pr_info)
    with fetch, checks, emit as emitted, patch("reviewer.reviewer_status.set_status"), \
         patch("reviewer.gh_fork_hold.approve_waiting_runs", return_value=2) as approve, \
         patch("events.reviewer_fork_hold.emit_product_event") as fork_emitted, \
         patch.object(gate_mod, "_run_reviewer") as review:
        handled = check_gate_on_trigger("webhook.github.pr_labeled", payload)
    return handled, review, approve, fork_emitted


def test_the_review_label_on_the_current_head_runs_the_review(_reload_gate):
    gate_mod = _reload_gate
    handled, review, approve, fork_emitted = _label(
        gate_mod, _payload("labeled", "abc123def", sender="maintainer"), FORK_INFO,
    )

    assert handled
    review.assert_called_once()
    assert gate_mod.reviewer_gate_state.get_approved_sha("owner/repo#42") == "abc123def"
    approve.assert_called_once_with("owner/repo", "abc123def")
    props = fork_emitted.call_args.kwargs["properties"]
    assert (props["sender"], props["approved_runs"]) == ("maintainer", 2)


def test_a_label_for_an_old_head_does_not_approve_a_newer_head(_reload_gate):
    gate_mod = _reload_gate
    _, review, _, _ = _label(gate_mod, _payload("labeled", "oldhead111"), FORK_INFO)
    review.assert_not_called()

    newer = {**FORK_INFO, "headRefOid": "newhead222"}
    with patch("reviewer.reviewer_status.set_status") as status:
        review = _evaluate(gate_mod, newer, status)

    review.assert_not_called()
    status.assert_called_once()


def test_the_label_alone_without_an_event_is_not_approval(_reload_gate):
    info = {**FORK_INFO, "labels": [{"name": LABEL_REVIEW}]}
    with patch("reviewer.reviewer_status.set_status") as status:
        review = _evaluate(_reload_gate, info, status)

    review.assert_not_called()


def test_a_label_event_without_a_head_is_reported_not_approved(_reload_gate):
    from events.reviewer_route import check_gate_on_trigger

    payload = _payload("labeled", "")
    with patch("events.reviewer_route.emit_product_event") as emitted, \
         patch("events.reviewer_fork_hold.approve_head") as approve:
        assert check_gate_on_trigger("webhook.github.pr_labeled", payload) is False

    approve.assert_not_called()
    assert emitted.call_args.kwargs["properties"]["missing_fields"] == ["pull_request.head.sha"]


def _push(action_event: str, payload: dict):
    from events.reviewer_route import run_review

    with patch("reviewer.reviewer_status.set_status") as status, \
         patch("reviewer.gh_fork_hold.remove_review_label") as remove:
        run_review(action_event, payload)
    return status, remove


def test_a_new_fork_push_drops_the_approval_and_the_label_and_waits(_reload_gate):
    gate_mod = _reload_gate
    gate_mod.reviewer_gate_state.record_approval("owner/repo#42", "abc123def")

    status, remove = _push("webhook.github.pr_synchronize", _payload("synchronize", "newhead222"))

    assert gate_mod.reviewer_gate_state.get_approved_sha("owner/repo#42") is None
    remove.assert_called_once_with("owner/repo", 42)
    status.assert_called_once_with(
        "owner/repo", "newhead222", "pending", WAITING, context=STATUS_PRESENCE_CONTEXT,
    )


def test_an_opened_fork_waits_without_touching_labels(_reload_gate):
    status, remove = _push("webhook.github.pr_opened", _payload("opened", "abc123def"))

    status.assert_called_once()
    remove.assert_not_called()


def test_a_deleted_fork_push_is_held(_reload_gate):
    status, remove = _push("webhook.github.pr_synchronize", _payload("synchronize", "abc123def", head_repo=None))

    status.assert_called_once()
    remove.assert_called_once()


def test_a_same_repo_push_is_untouched(_reload_gate):
    gate_mod = _reload_gate
    gate_mod.reviewer_gate_state.record_approval("owner/repo#42", "abc123def")

    status, remove = _push(
        "webhook.github.pr_synchronize", _payload("synchronize", "newhead222", head_repo="owner/repo"),
    )

    status.assert_not_called()
    remove.assert_not_called()
    assert gate_mod.reviewer_gate_state.get_approved_sha("owner/repo#42") == "abc123def"
