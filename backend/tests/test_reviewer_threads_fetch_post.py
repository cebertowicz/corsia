from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infra.transient_read import TRANSIENT_READ_ATTEMPTS
from reviewer.review_comment_fetch import (
    fetch_existing_issue_comments,
    fetch_existing_review_comments,
)
from reviewer.reviewer_threads import post_review_summary
from reviewer_threads_fixtures import _mock_run


@pytest.fixture(autouse=True)
def _no_retry_sleep():
    """The unreadable-read retry must never really sleep 4 s in these tests."""
    with patch("infra.transient_read.time.sleep") as sleep:
        yield sleep


@patch("reviewer.gh_api.subprocess.run")
def test_fetch_uses_slurp_flag(mock_run):
    mock_run.return_value = _mock_run(stdout='[[{"id": 1, "body": "x"}]]')
    out = fetch_existing_review_comments(42, "owner/repo")
    assert out == [{"id": 1, "body": "x"}]
    args = mock_run.call_args[0][0]
    assert "--paginate" in args
    assert "--slurp" in args


@patch("reviewer.gh_api.subprocess.run")
def test_fetch_flattens_multipage_slurp(mock_run):
    mock_run.return_value = _mock_run(
        stdout=(
            '[[{"id": 1, "body": "a"}, {"id": 2, "body": "b"}],'
            ' [{"id": 3, "body": "c"}]]'
        )
    )
    out = fetch_existing_review_comments(42, "owner/repo")
    assert [c["id"] for c in out] == [1, 2, 3]


@patch("reviewer.gh_api.subprocess.run")
def test_fetch_handles_empty_page_in_slurp(mock_run):
    mock_run.return_value = _mock_run(stdout='[[], [{"id": 1, "body": "only"}], []]')
    out = fetch_existing_review_comments(42, "owner/repo")
    assert len(out) == 1
    assert out[0]["id"] == 1


@patch("reviewer.gh_api.subprocess.run")
def test_fetch_handles_already_flat_response(mock_run):
    mock_run.return_value = _mock_run(stdout='[{"id": 1, "body": "flat"}]')
    out = fetch_existing_review_comments(42, "owner/repo")
    assert len(out) == 1
    assert out[0]["id"] == 1


@patch("reviewer.gh_api.subprocess.run")
def test_fetch_falls_back_to_manual_pagination_on_unknown_flag(mock_run):
    mock_run.side_effect = [
        _mock_run(returncode=1, stderr="unknown flag: --slurp"),
        _mock_run(stdout='[{"id": 1}, {"id": 2}]'),
    ]
    out = fetch_existing_review_comments(42, "owner/repo")
    assert len(out) == 2
    second_call_args = mock_run.call_args_list[1][0][0]
    assert "--slurp" not in second_call_args
    assert any("per_page=100" in a for a in second_call_args)


@patch("reviewer.gh_api.subprocess.run")
def test_fetch_retries_unreadable_read_then_returns_comments(mock_run, _no_retry_sleep):
    """One transient gh failure re-polls once and returns the readable result."""
    mock_run.side_effect = [
        _mock_run(returncode=2, stderr="API rate limit exceeded"),
        _mock_run(stdout='[[{"id": 1, "body": "x"}]]'),
    ]
    out = fetch_existing_review_comments(42, "owner/repo")
    assert out == [{"id": 1, "body": "x"}]
    assert mock_run.call_count == 2
    _no_retry_sleep.assert_called_once()


@patch("reviewer.gh_api.subprocess.run")
def test_fetch_returns_none_only_after_the_full_retry_budget(mock_run, _no_retry_sleep):
    """Persistent unreadability uses the whole bounded budget, then returns
    None so the caller's 'history is unreadable' path is unchanged."""
    mock_run.side_effect = itertools.repeat(
        _mock_run(returncode=2, stderr="API rate limit exceeded")
    )
    out = fetch_existing_issue_comments(42, "owner/repo")
    assert out is None
    assert mock_run.call_count == TRANSIENT_READ_ATTEMPTS
    assert _no_retry_sleep.call_count == TRANSIENT_READ_ATTEMPTS - 1


@pytest.mark.parametrize(
    "response",
    [
        _mock_run(returncode=2, stderr="API rate limit exceeded"),
        _mock_run(stdout=""),
    ],
    ids=["gh-failure", "blank-stdout"],
)
@patch("reviewer.gh_api.subprocess.run")
def test_fetch_returns_unreadable_on_failed_or_blank_response(mock_run, response):
    mock_run.return_value = response
    out = fetch_existing_review_comments(42, "owner/repo")
    assert out is None


@patch("reviewer.gh_api.subprocess.run")
def test_fetch_handles_invalid_json_via_manual_fallback(mock_run):
    mock_run.side_effect = [
        _mock_run(stdout="not-json"),
        _mock_run(stdout="[]"),
    ]
    out = fetch_existing_review_comments(42, "owner/repo")
    assert out == []
    assert mock_run.call_count == 2


@patch("reviewer.gh_api.subprocess.run")
def test_fetch_discards_partial_manual_pagination(mock_run):
    first_page = json.dumps([{"id": comment_id} for comment_id in range(100)])
    mock_run.side_effect = itertools.cycle([
        _mock_run(returncode=1, stderr="unknown flag: --slurp"),
        _mock_run(stdout=first_page),
        _mock_run(returncode=2, stderr="API rate limit exceeded"),
    ])

    out = fetch_existing_review_comments(42, "owner/repo")

    assert out is None


@pytest.mark.parametrize("blank_page", [1, 2])
@patch("reviewer.gh_api.subprocess.run")
def test_fetch_discards_blank_manual_pagination_page(mock_run, blank_page):
    first_page = _mock_run(
        stdout=json.dumps([{"id": comment_id} for comment_id in range(100)])
    )
    manual_pages = [_mock_run(stdout="")]
    if blank_page == 2:
        manual_pages.insert(0, first_page)
    mock_run.side_effect = itertools.cycle([
        _mock_run(returncode=1, stderr="unknown flag: --slurp"),
        *manual_pages,
    ])

    assert fetch_existing_review_comments(42, "owner/repo") is None


@patch("reviewer.gh_api.subprocess.run")
def test_fetch_issue_comments_uses_the_issues_endpoint(mock_run):
    mock_run.return_value = _mock_run(stdout='[[{"id": 7, "body": "x"}]]')
    out = fetch_existing_issue_comments(42, "owner/repo")
    assert out == [{"id": 7, "body": "x"}]
    args = mock_run.call_args[0][0]
    assert any("repos/owner/repo/issues/42/comments" in a for a in args)


@pytest.mark.parametrize(
    "response",
    [
        _mock_run(returncode=2, stderr="API rate limit exceeded"),
        _mock_run(stdout=""),
    ],
    ids=["gh-failure", "blank-stdout"],
)
@patch("reviewer.gh_api.subprocess.run")
def test_fetch_issue_comments_returns_unreadable_on_failure(mock_run, response):
    mock_run.return_value = response
    assert fetch_existing_issue_comments(42, "owner/repo") is None


@patch("reviewer.gh_api.subprocess.run")
def test_post_review_summary_creates_new_when_none_exists(mock_run):
    mock_run.return_value = _mock_run(stdout='{"id": 99}')
    ok = post_review_summary(42, "owner/repo", "summary body", [])
    assert ok is True
    last_args = mock_run.call_args_list[-1][0][0]
    assert "POST" in last_args
    assert any("/issues/42/comments" in a for a in last_args)


@patch("reviewer.gh_api.subprocess.run")
def test_post_review_summary_edits_existing_via_patch(mock_run):
    mock_run.return_value = _mock_run(stdout='{"id": 555}')
    comments = [
        {"id": 555, "body": "old summary\n\n<!-- corsia-review-summary -->"},
    ]
    ok = post_review_summary(42, "owner/repo", "fresh summary", comments)
    assert ok is True
    last_args = mock_run.call_args_list[-1][0][0]
    assert "PATCH" in last_args
    assert any("/issues/comments/555" in a for a in last_args)


@patch("reviewer.gh_api.subprocess.run")
def test_post_review_summary_does_not_duplicate_when_id_is_unusable(mock_run):
    """A sticky summary we cannot address must never become a second one."""
    comments = [{"body": "<!-- corsia-review-summary -->"}]

    ok = post_review_summary(42, "owner/repo", "summary body", comments)

    assert ok is False
    mock_run.assert_not_called()
