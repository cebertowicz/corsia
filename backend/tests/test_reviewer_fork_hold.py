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

WAITING = "waiting for a maintainer to add the review label"
FORK_INFO = {**_DEFAULT_PR_INFO, "headRepoFullName": "contributor/repo"}


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


def _label(gate_mod, payload, pr_info, *, can_approve=True, runs=(2, 0), label_removed=True):
    from events.reviewer_route import check_gate_on_trigger

    fetch, checks, emit = _green(gate_mod, pr_info)
    with fetch, checks, emit, patch("reviewer.reviewer_status.set_status") as status, \
         patch("reviewer.gh_fork_hold.sender_can_approve", return_value=can_approve), \
         patch("reviewer.gh_fork_hold.approve_waiting_runs", return_value=runs) as approve, \
         patch("reviewer.gh_fork_hold.remove_review_label", return_value=label_removed) as remove, \
         patch("events.connectors.github_reviewer_fork_hold.emit_product_event") as emitted, \
         patch.object(gate_mod, "_run_reviewer") as review:
        handled = check_gate_on_trigger("webhook.github.pr_labeled", payload)
    return SimpleNamespace(
        handled=handled, review=review, approve=approve, remove=remove, status=status, emitted=emitted,
    )


def _texts(status) -> list[str]:
    return [call.args[3] for call in status.call_args_list]


def _push(event: str, payload: dict, current: str | None = None, label_removed: bool = True):
    from events.reviewer_route import run_review

    head = payload["pull_request"]["head"]["sha"]
    info = {**FORK_INFO, "headRefOid": current or head}
    with patch("events.connectors.github_reviewer_gate.fetch_pr_info", return_value=info), \
         patch("reviewer.reviewer_status.set_status") as status, \
         patch("reviewer.gh_fork_hold.remove_review_label", return_value=label_removed) as remove:
        run_review(event, payload)
    return status, remove


def test_the_review_label_on_the_current_head_runs_the_review(_reload_gate):
    gate_mod = _reload_gate
    out = _label(gate_mod, _payload("labeled", "abc123def", sender="maintainer"), FORK_INFO)

    assert out.handled
    out.review.assert_called_once()
    assert gate_mod.reviewer_gate_state.get_approved_sha("owner/repo#42") == "abc123def"
    out.approve.assert_called_once_with("owner/repo", "abc123def")
    assert fork_hold.APPROVED_TEXT in _texts(out.status)
    props = out.emitted.call_args.kwargs["properties"]
    assert (props["sender"], props["approved_runs"], props["failed_runs"]) == ("maintainer", 2, 0)


def test_a_label_for_an_old_head_does_not_approve_the_current_head(_reload_gate):
    gate_mod = _reload_gate
    out = _label(gate_mod, _payload("labeled", "oldhead111"), FORK_INFO)

    out.review.assert_not_called()
    out.approve.assert_not_called()
    assert gate_mod.reviewer_gate_state.get_approved_sha("owner/repo#42") is None


def test_a_label_from_someone_without_write_access_is_removed_and_ignored(_reload_gate):
    gate_mod = _reload_gate
    out = _label(gate_mod, _payload("labeled", "abc123def", sender="triager"), FORK_INFO, can_approve=False)

    out.review.assert_not_called()
    out.approve.assert_not_called()
    out.remove.assert_called_once_with("owner/repo", 42)
    assert gate_mod.reviewer_gate_state.get_approved_sha("owner/repo#42") is None
    assert _texts(out.status) == [fork_hold.WAITING_TEXT]


def test_a_denied_label_never_downgrades_an_approved_head(_reload_gate):
    gate_mod = _reload_gate
    gate_mod.reviewer_gate_state.record_approval("owner/repo#42", "abc123def")
    out = _label(gate_mod, _payload("labeled", "abc123def", sender="triager"), FORK_INFO, can_approve=False)

    assert gate_mod.reviewer_gate_state.get_approved_sha("owner/repo#42") == "abc123def"
    out.status.assert_not_called()


@pytest.mark.parametrize("runs", [None, (1, 1)])
def test_runs_that_could_not_be_released_are_named_in_the_status(_reload_gate, runs):
    gate_mod = _reload_gate
    out = _label(gate_mod, _payload("labeled", "abc123def", sender="maintainer"), FORK_INFO, runs=runs)

    assert gate_mod.reviewer_gate_state.get_approved_sha("owner/repo#42") == "abc123def"
    assert fork_hold.RUNS_HELD_TEXT in _texts(out.status)
    assert fork_hold.APPROVED_TEXT not in _texts(out.status)


def test_an_unreadable_current_head_approves_nothing_and_says_how_to_retry(_reload_gate):
    gate_mod = _reload_gate
    fetch = patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info", return_value=None)
    from events.reviewer_route import check_gate_on_trigger

    with fetch, patch("reviewer.reviewer_status.set_status") as status, \
         patch("reviewer.gh_fork_hold.approve_waiting_runs") as approve:
        check_gate_on_trigger("webhook.github.pr_labeled", _payload("labeled", "abc123def"))

    approve.assert_not_called()
    assert _texts(status) == [fork_hold.STALE_LABEL_TEXT]
    assert gate_mod.reviewer_gate_state.get_approved_sha("owner/repo#42") is None


def test_a_label_on_a_same_repo_pr_is_not_a_fork_approval(_reload_gate):
    out = _label(_reload_gate, _payload("labeled", "abc123def", head_repo="owner/repo"), _DEFAULT_PR_INFO)

    out.approve.assert_not_called()
    out.status.assert_not_called()


def test_the_label_alone_without_an_event_is_not_approval(_reload_gate):
    info = {**FORK_INFO, "labels": [{"name": LABEL_REVIEW}]}
    with patch("reviewer.reviewer_status.set_status") as status:
        review = _evaluate(_reload_gate, info, status)

    review.assert_not_called()
    assert _texts(status) == [fork_hold.STALE_LABEL_TEXT]


def test_a_label_event_without_a_head_is_reported_not_approved(_reload_gate):
    from events.reviewer_route import check_gate_on_trigger

    payload = _payload("labeled", "")
    with patch("events.reviewer_route.emit_product_event") as emitted, \
         patch("events.connectors.github_reviewer_fork_hold.reconcile_label") as approve:
        assert check_gate_on_trigger("webhook.github.pr_labeled", payload) is False

    approve.assert_not_called()
    assert emitted.call_args.kwargs["properties"]["missing_fields"] == ["pull_request.head.sha"]


def test_a_new_fork_push_drops_the_approval_and_the_label_and_waits(_reload_gate):
    gate_mod = _reload_gate
    gate_mod.reviewer_gate_state.record_approval("owner/repo#42", "abc123def")

    status, remove = _push("webhook.github.pr_synchronize", _payload("synchronize", "newhead222"))

    assert gate_mod.reviewer_gate_state.get_approved_sha("owner/repo#42") is None
    remove.assert_called_once_with("owner/repo", 42)
    status.assert_called_once_with(
        "owner/repo", "newhead222", "pending", WAITING, context=STATUS_PRESENCE_CONTEXT,
    )


def test_a_failed_label_removal_is_named_in_the_status_and_a_relabel_recovers(_reload_gate):
    gate_mod = _reload_gate
    status, _ = _push(
        "webhook.github.pr_synchronize", _payload("synchronize", "newhead222"), label_removed=False,
    )
    assert _texts(status) == [fork_hold.STALE_LABEL_TEXT]

    out = _label(gate_mod, _payload("labeled", "newhead222", sender="maintainer"), {**FORK_INFO, "headRefOid": "newhead222"})
    out.review.assert_called_once()
    assert gate_mod.reviewer_gate_state.get_approved_sha("owner/repo#42") == "newhead222"


def test_a_label_handled_before_its_own_push_keeps_the_approval(_reload_gate):
    gate_mod = _reload_gate
    info_b = {**FORK_INFO, "headRefOid": "newhead222"}
    _label(gate_mod, _payload("labeled", "newhead222", sender="maintainer"), info_b)

    status, remove = _push("webhook.github.pr_synchronize", _payload("synchronize", "newhead222"))

    assert gate_mod.reviewer_gate_state.get_approved_sha("owner/repo#42") == "newhead222"
    remove.assert_not_called()
    status.assert_not_called()


def test_a_late_push_event_for_an_older_head_changes_nothing(_reload_gate):
    gate_mod = _reload_gate
    gate_mod.reviewer_gate_state.record_approval("owner/repo#42", "newhead222")

    status, remove = _push(
        "webhook.github.pr_synchronize", _payload("synchronize", "oldhead111"), current="newhead222",
    )

    assert gate_mod.reviewer_gate_state.get_approved_sha("owner/repo#42") == "newhead222"
    remove.assert_not_called()
    status.assert_not_called()


def test_a_bot_push_to_a_fork_still_drops_the_label_and_waits(_reload_gate):
    from events import guard

    payload = _payload("synchronize", "newhead222", sender="autofix[bot]", sender_type="Bot")
    assert guard.is_bot_or_self_authored(payload)

    status, remove = _push("webhook.github.pr_synchronize", payload)

    remove.assert_called_once_with("owner/repo", 42)
    assert _texts(status) == [fork_hold.WAITING_TEXT]


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


def test_every_status_text_fits_githubs_description_limit():
    for text in (fork_hold.WAITING_TEXT, fork_hold.STALE_LABEL_TEXT, fork_hold.APPROVED_TEXT, fork_hold.RUNS_HELD_TEXT):
        assert len(text) <= 140
