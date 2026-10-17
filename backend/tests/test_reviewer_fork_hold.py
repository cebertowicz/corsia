"""The reviewer holds a fork head until a maintainer approves that exact head."""

from __future__ import annotations

from types import SimpleNamespace
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

from events.connectors import github_reviewer_fork_hold as fork_hold
from reviewer.reviewer_categories import LABEL_REVIEW, STATUS_PRESENCE_CONTEXT

FORK_INFO = {**_DEFAULT_PR_INFO, "headRepoFullName": "contributor/repo"}
PR_KEY = "owner/repo#42"


def _payload(
    action: str, sha: str, head_repo: object = "contributor/repo", sender: str = "alice",
    sender_type: str = "User",
) -> dict:
    head = {"sha": sha, "repo": {"full_name": head_repo} if head_repo else None}
    return {
        "action": action,
        "repository": {"full_name": "owner/repo"},
        "pull_request": {
            "number": 42, "title": "A change", "head": head,
            "base": {"ref": "dev", "repo": {"full_name": "owner/repo"}}, "labels": [],
        },
        "label": {"name": LABEL_REVIEW},
        "sender": {"login": sender, "type": sender_type},
    }


def _gate(gate_mod, pr_info, *, green=True, conclusion="success", sha=None):
    """One check-suite event through the gate; returns the review run and status mocks."""
    with patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info", return_value=pr_info), \
         patch.object(gate_mod.github_reviewer_gate, "checks_green", return_value=green), \
         patch.object(gate_mod, "emit_product_event"), \
         patch("reviewer.reviewer_status.set_status") as status, \
         patch.object(gate_mod, "_run_reviewer") as review:
        _call_gate(gate_mod, head_sha=sha or pr_info["headRefOid"], conclusion=conclusion)
    return SimpleNamespace(review=review, status=status)


def _label(gate_mod, payload, pr_info, *, can_approve=True, green=True, label_removed=True):
    """One review-label event through the router; returns what it ran and wrote."""
    from events.reviewer_route import check_gate_on_trigger

    with patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info", return_value=pr_info), \
         patch.object(gate_mod.github_reviewer_gate, "checks_green", return_value=green), \
         patch.object(gate_mod, "emit_product_event"), \
         patch("reviewer.reviewer_status.set_status") as status, \
         patch("reviewer.gh_fork_hold.sender_can_approve", return_value=can_approve) as permission, \
         patch("reviewer.gh_fork_hold.remove_review_label", return_value=label_removed) as remove, \
         patch("events.connectors.github_reviewer_fork_hold.emit_product_event") as emitted, \
         patch.object(gate_mod, "_run_reviewer") as review:
        handled = check_gate_on_trigger("webhook.github.pr_labeled", payload)
    return SimpleNamespace(
        handled=handled, review=review, remove=remove, status=status, emitted=emitted,
        permission=permission,
    )


def _texts(status) -> list[str]:
    return [call.args[3] for call in status.call_args_list]


def _approved(gate_mod):
    return gate_mod.reviewer_gate_state.get_approved_sha(PR_KEY)


def test_a_fork_head_with_green_checks_waits_instead_of_firing(_reload_gate):
    out = _gate(_reload_gate, FORK_INFO)

    out.review.assert_not_called()
    out.status.assert_called_once_with(
        "owner/repo", "abc123def", "pending", fork_hold.WAITING_TEXT, context=STATUS_PRESENCE_CONTEXT,
    )


@pytest.mark.parametrize("head_repo", [None, "OWNER/Other"])
def test_a_deleted_or_different_fork_is_held(_reload_gate, head_repo):
    out = _gate(_reload_gate, {**_DEFAULT_PR_INFO, "headRepoFullName": head_repo})

    out.review.assert_not_called()
    out.status.assert_called_once()


def test_a_pr_info_without_repo_identity_is_held(_reload_gate):
    info = {k: v for k, v in _DEFAULT_PR_INFO.items() if not k.endswith("RepoFullName")}

    _gate(_reload_gate, info).review.assert_not_called()


def test_a_same_repo_head_fires_as_before(_reload_gate):
    out = _gate(_reload_gate, {**_DEFAULT_PR_INFO, "headRepoFullName": "Owner/Repo"})

    out.review.assert_called_once()
    out.status.assert_not_called()
    assert _approved(_reload_gate) is None


def test_workflow_runs_held_for_approval_say_what_the_head_waits_for(_reload_gate):
    out = _gate(_reload_gate, FORK_INFO, green=False, conclusion="action_required")

    out.review.assert_not_called()
    assert _texts(out.status) == [fork_hold.WAITING_TEXT]


def test_held_workflow_runs_on_a_same_repo_head_write_nothing(_reload_gate):
    out = _gate(_reload_gate, _DEFAULT_PR_INFO, green=False, conclusion="action_required")

    out.status.assert_not_called()


def test_an_unreadable_pr_with_held_runs_is_reported_not_guessed(_reload_gate):
    gate_mod = _reload_gate
    with patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info", return_value=None), \
         patch("reviewer.reviewer_status.set_status") as status, \
         patch("events.connectors.github_reviewer_fork_hold.emit_product_event") as emitted:
        _call_gate(gate_mod, conclusion="action_required")

    status.assert_not_called()
    assert emitted.call_args.args[:2] == ("reviewer_gate", "fork_hold_unreadable")


def test_other_failed_suites_do_not_look_up_the_pr(_reload_gate):
    gate_mod = _reload_gate
    with patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info") as fetch:
        _call_gate(gate_mod, conclusion="failure")

    fetch.assert_not_called()


def test_the_review_label_on_green_checks_runs_the_review_at_once(_reload_gate):
    gate_mod = _reload_gate
    out = _label(gate_mod, _payload("labeled", "abc123def", sender="maintainer"), FORK_INFO)

    assert out.handled
    out.review.assert_called_once()
    assert _approved(gate_mod) == "abc123def"
    out.permission.assert_called_once_with("owner/repo", "maintainer")
    out.remove.assert_called_once_with("owner/repo", 42)
    assert out.emitted.call_args.args[:2] == ("reviewer_gate", "review_label_approved")


def test_the_label_before_green_checks_lets_the_green_event_run_the_review(_reload_gate):
    gate_mod = _reload_gate
    out = _label(gate_mod, _payload("labeled", "abc123def", sender="maintainer"), FORK_INFO, green=False)

    out.review.assert_not_called()
    out.status.assert_not_called()
    assert _approved(gate_mod) == "abc123def"

    later = _gate(gate_mod, FORK_INFO)

    later.review.assert_called_once()
    later.status.assert_not_called()


def test_the_label_never_approves_workflow_runs(_reload_gate):
    from events.reviewer_route import check_gate_on_trigger

    gate_mod = _reload_gate

    def gh(args, timeout):
        stdout = "write\n" if args[-2:] == ["--jq", ".permission"] else ""
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    with patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info", return_value=FORK_INFO), \
         patch.object(gate_mod.github_reviewer_gate, "checks_green", return_value=False), \
         patch.object(gate_mod, "emit_product_event"), \
         patch("reviewer.reviewer_status.set_status"), \
         patch("events.connectors.github_reviewer_fork_hold.emit_product_event"), \
         patch("reviewer.gh_fork_hold.run_gh", side_effect=gh) as run:
        check_gate_on_trigger("webhook.github.pr_labeled", _payload("labeled", "abc123def", sender="maintainer"))

    calls = [call.args[0] for call in run.call_args_list]
    assert calls == [
        ["api", "repos/owner/repo/collaborators/maintainer/permission", "--jq", ".permission"],
        ["api", "-X", "DELETE", f"repos/owner/repo/issues/42/labels/{LABEL_REVIEW}"],
    ]
    assert _approved(gate_mod) == "abc123def"


def test_a_label_names_the_head_it_was_added_on_even_if_a_push_landed_since(_reload_gate):
    gate_mod = _reload_gate
    current = {**FORK_INFO, "headRefOid": "newhead222"}
    out = _label(gate_mod, _payload("labeled", "abc123def", sender="maintainer"), current)

    out.review.assert_not_called()
    assert _approved(gate_mod) == "abc123def"

    held = _gate(gate_mod, current)

    held.review.assert_not_called()
    assert _texts(held.status) == [fork_hold.WAITING_TEXT]


def test_a_new_fork_head_stays_held_after_the_old_head_was_approved(_reload_gate):
    gate_mod = _reload_gate
    gate_mod.reviewer_gate_state.record_approval(PR_KEY, "abc123def")

    held = _gate(gate_mod, {**FORK_INFO, "headRefOid": "newhead222"})

    held.review.assert_not_called()
    assert _texts(held.status) == [fork_hold.WAITING_TEXT]
    assert _approved(gate_mod) == "abc123def"


def test_a_fork_push_writes_nothing(_reload_gate):
    from events.reviewer_route import run_review

    gate_mod = _reload_gate
    gate_mod.reviewer_gate_state.record_approval(PR_KEY, "abc123def")
    with patch("reviewer.reviewer_status.set_status") as status, \
         patch("reviewer.gh_fork_hold.remove_review_label") as remove:
        run_review("webhook.github.pr_synchronize", _payload("synchronize", "newhead222"))
        run_review(
            "webhook.github.pr_synchronize",
            _payload("synchronize", "newhead333", sender="autofix[bot]", sender_type="Bot"),
        )

    status.assert_not_called()
    remove.assert_not_called()
    assert _approved(gate_mod) == "abc123def"


def test_a_label_from_someone_without_write_access_is_consumed_and_approves_nothing(_reload_gate):
    gate_mod = _reload_gate
    out = _label(gate_mod, _payload("labeled", "abc123def", sender="triager"), FORK_INFO, can_approve=False)

    out.review.assert_not_called()
    out.status.assert_not_called()
    out.remove.assert_called_once_with("owner/repo", 42)
    assert _approved(gate_mod) is None
    assert out.emitted.call_args.args[:2] == ("reviewer_gate", "review_label_denied")


def test_a_label_that_could_not_be_removed_is_reported(_reload_gate):
    out = _label(
        _reload_gate, _payload("labeled", "abc123def", sender="maintainer"), FORK_INFO, label_removed=False,
    )

    assert out.emitted.call_args.kwargs["properties"]["label_removed"] is False
    out.review.assert_called_once()


def test_a_label_on_a_same_repo_pr_is_not_a_fork_approval(_reload_gate):
    gate_mod = _reload_gate
    out = _label(gate_mod, _payload("labeled", "abc123def", head_repo="owner/repo"), _DEFAULT_PR_INFO)

    out.permission.assert_not_called()
    out.remove.assert_not_called()
    out.review.assert_not_called()
    assert _approved(gate_mod) is None


def test_a_label_event_without_a_head_is_reported_as_a_label_event(_reload_gate):
    from events.reviewer_route import check_gate_on_trigger

    with patch("events.reviewer_route.emit_product_event") as emitted, \
         patch("events.connectors.github_reviewer_fork_hold.approve_labeled_head") as approve:
        assert check_gate_on_trigger("webhook.github.pr_labeled", _payload("labeled", "")) is False

    approve.assert_not_called()
    assert emitted.call_args.args[:2] == ("reviewer_gate", "label_event_invalid")
    assert emitted.call_args.kwargs["properties"]["missing_fields"] == ["pull_request.head.sha"]


def test_the_held_status_fits_githubs_description_limit():
    assert len(fork_hold.WAITING_TEXT) <= 140
