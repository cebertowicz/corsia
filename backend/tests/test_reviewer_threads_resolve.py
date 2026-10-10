from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reviewer.review_comment_fetch import (
    ReviewThreadState,
    fetch_existing_review_threads,
)
from reviewer.reviewer_resolution import (
    cited_line_report,
    set_review_thread_resolved,
)
from reviewer.reviewer_threads import (
    THREAD_ACTION_KEY,
    ReviewThreadAction,
    build_resolve_reply_body,
    classify_findings,
    classify_review_findings,
    fingerprint_finding,
    post_resolve_reply,
)
from reviewer_threads_fixtures import (
    _existing,
    _existing_pr_level,
    _finding,
    _mock_run,
    _pr_level_resolution,
)


def test_resolved_only_fingerprint_is_published_again_and_then_resolved():
    finding = _finding()
    fingerprint = fingerprint_finding(finding)
    resolved_parent = _existing(fingerprint, 1)
    resolved_reply = {
        "id": 2,
        "body": "<!-- corsia-review-resolved -->",
        "in_reply_to_id": 1,
    }

    new, resolves, unchanged = classify_findings(
        [finding], [resolved_parent, resolved_reply],
    )

    assert new == [finding]
    assert resolves == []
    assert unchanged == 0

    reposted = _existing(fingerprint, 3)
    _, resolves, _ = classify_findings(
        [], [resolved_parent, resolved_reply, reposted],
    )
    assert [comment["id"] for comment in resolves] == [3]


def test_missing_thread_snapshot_preserves_pr_level_absence_replies():
    fingerprint = fingerprint_finding(_finding(file=""))
    prior = _existing_pr_level(fingerprint, 8)

    new, resolutions, unchanged = classify_review_findings(
        [], [], [prior], 42, "owner/repo", "a" * 40,
    )

    assert new == []
    assert resolutions == [prior]
    assert unchanged == 0


def test_resolved_pr_level_finding_is_republished_and_later_resolved():
    finding = _finding(file="")
    fingerprint = fingerprint_finding(finding)
    resolved = _pr_level_resolution(fingerprint, 8)
    prior = _existing_pr_level(fingerprint, 7)

    new, resolves, unchanged = classify_findings([finding], [prior, resolved])

    assert new == [finding]
    assert resolves == []
    assert unchanged == 0

    reposted = _existing_pr_level(fingerprint, 9)
    _, resolves, _ = classify_findings([], [prior, resolved, reposted])
    assert [comment["id"] for comment in resolves] == [9]


def test_classification_uses_newest_unresolved_thread_independent_of_payload_order():
    fingerprint = "deadbeefdeadbeef"
    older = _existing(fingerprint, 1)
    older["created_at"] = "2026-09-12T00:00:00Z"
    newer = _existing(fingerprint, 2)
    newer["created_at"] = "2026-09-12T01:00:00Z"

    _, resolves, unchanged = classify_findings([], [newer, older])

    assert unchanged == 0
    assert [comment["id"] for comment in resolves] == [2]


def test_resolution_reply_reports_unchanged_cited_lines_without_claiming_a_fix():
    body = build_resolve_reply_body(
        "Cited lines unchanged since finding commit "
        "(`12345678` → `abcdef12`); not verified fixed.",
    )

    assert "Not raised this run." in body
    assert "Cited lines unchanged" in body
    assert "not verified fixed" in body
    assert "no longer present" not in body


def test_resolution_reply_requires_an_explicit_evidence_report_without_a_head_sha():
    body = build_resolve_reply_body("Cited lines unchanged; not verified fixed.")

    assert "Cited lines unchanged; not verified fixed." in body
    assert "abcdef" not in body


@patch("reviewer.reviewer_resolution.run_gh")
def test_cited_line_report_ignores_unchanged_context_inside_a_changed_hunk(mock_run):
    source = "a" * 40
    head = "b" * 40
    mock_run.return_value = _mock_run(stdout=json.dumps({
        "status": "ahead",
        "files": [{
            "filename": "backend/foo.py",
            "patch": "@@ -40,3 +40,3 @@\n unchanged\n-old\n+new\n unchanged too",
        }],
    }))
    comment = {
        "path": "backend/foo.py",
        "original_line": 40,
        "original_commit_id": source,
    }

    report = cited_line_report(comment, "owner/repo", head)

    assert "Cited lines unchanged" in report


@patch("reviewer.reviewer_resolution.run_gh")
def test_cited_line_report_checks_the_full_original_citation_range(mock_run):
    source = "a" * 40
    head = "b" * 40
    mock_run.return_value = _mock_run(stdout=json.dumps({
        "status": "ahead",
        "files": [{
            "filename": "backend/foo.py",
            "patch": "@@ -40 +40 @@\n-old\n+new",
        }],
    }))
    comment = {
        "path": "backend/foo.py",
        "original_start_line": 40,
        "original_line": 42,
        "original_commit_id": source,
    }

    report = cited_line_report(comment, "owner/repo", head)

    assert "Cited lines changed" in report


@patch("reviewer.reviewer_resolution.run_gh")
def test_cited_line_report_counts_an_insertion_strictly_inside_a_cited_range(mock_run):
    source = "a" * 40
    head = "b" * 40
    mock_run.return_value = _mock_run(stdout=json.dumps({
        "status": "ahead",
        "files": [{
            "filename": "backend/foo.py",
            "patch": "@@ -40,3 +40,4 @@\n unchanged\n+inserted\n unchanged\n unchanged",
        }],
    }))
    comment = {
        "path": "backend/foo.py",
        "original_start_line": 40,
        "original_line": 42,
        "original_commit_id": source,
    }

    report = cited_line_report(comment, "owner/repo", head)

    assert "Cited lines changed" in report


@patch("reviewer.reviewer_resolution.run_gh")
def test_cited_line_report_ignores_an_insertion_just_above_a_cited_range(mock_run):
    source = "a" * 40
    head = "b" * 40
    mock_run.return_value = _mock_run(stdout=json.dumps({
        "status": "ahead",
        "files": [{
            "filename": "backend/foo.py",
            "patch": "@@ -39,3 +39,4 @@\n+inserted\n unchanged\n unchanged\n unchanged",
        }],
    }))
    comment = {
        "path": "backend/foo.py",
        "original_start_line": 40,
        "original_line": 42,
        "original_commit_id": source,
    }

    report = cited_line_report(comment, "owner/repo", head)

    assert "Cited lines unchanged" in report


@patch("reviewer.reviewer_resolution.run_gh")
def test_cited_line_report_rejects_a_diverged_compare_base(mock_run):
    source = "a" * 40
    head = "b" * 40
    mock_run.return_value = _mock_run(stdout=json.dumps({
        "status": "diverged",
        "files": [{
            "filename": "backend/foo.py",
            "patch": "@@ -42 +42 @@\n-old\n+new",
        }],
    }))
    comment = {
        "path": "backend/foo.py",
        "original_line": 42,
        "original_commit_id": source,
    }

    report = cited_line_report(comment, "owner/repo", head)

    assert "finding commit does not precede reviewed head" in report
    assert "not verified fixed" in report


@patch("reviewer.reviewer_resolution.run_gh")
def test_cited_line_report_compares_the_finding_commit_at_the_cited_line(mock_run):
    source = "a" * 40
    head = "b" * 40
    mock_run.return_value = _mock_run(stdout=json.dumps({
        "status": "ahead",
        "files": [{
            "filename": "backend/foo.py",
            "patch": "@@ -42 +42 @@\n-old\n+new",
        }],
    }))
    comment = {
        "path": "backend/foo.py",
        "original_line": 42,
        "original_commit_id": source,
    }

    report = cited_line_report(comment, "owner/repo", head)

    assert "Cited lines changed" in report
    assert source[:8] in report and head[:8] in report
    assert "not verified fixed" in report
    assert mock_run.call_args.args[0][-1] == f"repos/owner/repo/compare/{source}...{head}"


@patch("reviewer.reviewer_resolution.run_gh")
def test_cited_line_report_names_unchanged_lines_as_not_verified_fixed(mock_run):
    mock_run.return_value = _mock_run(stdout=json.dumps({"status": "ahead", "files": []}))
    comment = {
        "path": "backend/foo.py",
        "line": 42,
        "commit_id": "a" * 40,
    }

    report = cited_line_report(comment, "owner/repo", "b" * 40)

    assert "Cited lines unchanged" in report
    assert "not verified fixed" in report


@patch("reviewer.reviewer_resolution.run_gh")
def test_cited_line_report_never_claims_a_fix_when_comparison_fails(mock_run):
    mock_run.return_value = _mock_run(returncode=1, stderr="compare unavailable")
    comment = {
        "path": "backend/foo.py",
        "line": 42,
        "commit_id": "a" * 40,
    }

    report = cited_line_report(comment, "owner/repo", "b" * 40)

    assert "Could not check cited lines: compare unavailable" in report
    assert "not verified fixed" in report


@patch("reviewer.gh_api.subprocess.run")
def test_post_resolve_reply_resolves_a_pr_level_comment_with_a_new_comment(mock_run):
    """A PR-level finding has no thread; resolution must never edit a comment,
    because a fingerprint can also appear in a human's quote of a finding."""
    mock_run.return_value = _mock_run(stdout='{"id": 8}')
    comment = _existing_pr_level("deadbeefdeadbeef", 7)

    ok = post_resolve_reply(comment, "owner/repo", "abc12345xyz")

    assert ok is True
    args = mock_run.call_args[0][0]
    assert "POST" in args
    assert "PATCH" not in args
    assert any("/issues/123/comments" in a for a in args)


def test_pr_level_resolution_body_binds_both_markers():
    from events.markers import REVIEW_FP_MARKER_PREFIX, REVIEW_RESOLVED_MARKER
    from reviewer.reviewer_threads import (
        build_pr_level_resolution_body,
        extract_fingerprint,
    )

    body = build_pr_level_resolution_body("deadbeefdeadbeef", "No cited lines; not verified fixed.")

    lines = body.splitlines()
    assert REVIEW_RESOLVED_MARKER in lines
    assert any(ln.startswith(REVIEW_FP_MARKER_PREFIX) for ln in lines)
    assert extract_fingerprint(body) == "deadbeefdeadbeef"


@patch("reviewer.gh_api.subprocess.run")
def test_post_resolve_reply_rejects_a_pr_level_comment_without_a_fingerprint(mock_run):
    comment = {
        "id": 7,
        "body": "no marker here",
        "issue_url": "https://api.github.com/repos/x/y/issues/123",
    }

    assert post_resolve_reply(comment, "owner/repo", "abc12345xyz") is False
    mock_run.assert_not_called()


@patch("reviewer.gh_api.subprocess.run")
def test_post_resolve_reply_only_posts_evidence_without_changing_thread_state(mock_run):
    mock_run.return_value = _mock_run(stdout='{"id": 999}')
    parent = {"id": 123, "pull_request_url": "https://api.github.com/repos/o/r/pulls/42"}
    ok = post_resolve_reply(parent, "owner/repo", "abc12345xyz")

    assert ok is True
    assert not any(
        "resolveReviewThread" in str(call.args[0])
        for call in mock_run.call_args_list
    )
    args = mock_run.call_args.args[0]
    assert any("/pulls/42/comments" in arg for arg in args)


def test_post_resolve_reply_returns_false_when_comment_id_missing():
    assert post_resolve_reply({}, "owner/repo", "abc") is False
    assert post_resolve_reply({"id": 1}, "owner/repo", "abc") is False


@patch("reviewer.gh_api.subprocess.run")
def test_post_resolve_reply_false_on_malformed_repo(mock_run):
    parent = {"id": 123, "pull_request_url": "https://api.github.com/repos/o/r/pulls/42"}
    ok = post_resolve_reply(parent, "no-slash-repo", "abc12345xyz")
    assert ok is False


@patch("reviewer.review_comment_fetch.run_gh")
def test_fetch_existing_review_threads_maps_root_ids_and_exact_head(mock_run):
    head = "a" * 40
    mock_run.return_value = _mock_run(stdout=json.dumps({
        "data": {"repository": {"pullRequest": {
            "headRefOid": head,
            "reviewThreads": {
                "nodes": [{
                    "id": "PRRT_1", "isResolved": True,
                    "comments": {"nodes": [{"databaseId": 41}]},
                }],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            },
        }}}
    }))

    snapshot = fetch_existing_review_threads(42, "owner/repo")

    assert snapshot is not None
    assert snapshot.head_sha == head
    assert snapshot.by_root_comment_id == {41: ReviewThreadState("PRRT_1", True)}
    args = mock_run.call_args.args[0]
    assert "owner=owner" in args
    assert "name=repo" in args
    assert "number=42" in args


@patch("reviewer.review_comment_fetch.run_gh")
def test_fetch_existing_review_threads_paginates_all_threads(mock_run):
    head = "b" * 40
    first_page = {
        "data": {"repository": {"pullRequest": {
            "headRefOid": head,
            "reviewThreads": {
                "nodes": [{
                    "id": "PRRT_1", "isResolved": False,
                    "comments": {"nodes": [{"databaseId": 51}]},
                }],
                "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
            },
        }}}
    }
    second_page = {
        "data": {"repository": {"pullRequest": {
            "headRefOid": head,
            "reviewThreads": {
                "nodes": [{
                    "id": "PRRT_2", "isResolved": True,
                    "comments": {"nodes": [{"databaseId": 52}]},
                }],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            },
        }}}
    }
    mock_run.side_effect = [
        _mock_run(stdout=json.dumps(first_page)),
        _mock_run(stdout=json.dumps(second_page)),
    ]

    snapshot = fetch_existing_review_threads(42, "owner/repo")

    assert snapshot is not None
    assert set(snapshot.by_root_comment_id) == {51, 52}
    assert snapshot.by_root_comment_id[52].is_resolved is True
    assert mock_run.call_count == 2
    assert "after=cursor-1" in mock_run.call_args_list[1].args[0]


@patch("reviewer.review_comment_fetch.run_gh")
def test_fetch_existing_review_threads_rejects_a_head_change_while_paginating(mock_run):
    first = "c" * 40
    second = "d" * 40
    def page(head, cursor):
        return _mock_run(stdout=json.dumps({
            "data": {"repository": {"pullRequest": {
                "headRefOid": head,
                "reviewThreads": {
                    "nodes": [],
                    "pageInfo": {"hasNextPage": bool(cursor), "endCursor": cursor},
                },
            }}}
        }))
    mock_run.side_effect = [page(first, "cursor"), page(second, None)]

    assert fetch_existing_review_threads(42, "owner/repo") is None


@patch("reviewer.reviewer_resolution.run_gh")
def test_set_review_thread_resolved_verifies_resolve_and_unresolve(mock_run):
    thread_id = "PRRT_123"
    mock_run.side_effect = [
        _mock_run(stdout=json.dumps({"data": {
            "resolveReviewThread": {
                "thread": {"id": thread_id, "isResolved": True},
            },
        }})),
        _mock_run(stdout=json.dumps({"data": {
            "unresolveReviewThread": {
                "thread": {"id": thread_id, "isResolved": False},
            },
        }})),
    ]

    assert set_review_thread_resolved(thread_id, resolved=True) is True
    assert set_review_thread_resolved(thread_id, resolved=False) is True
    assert "resolveReviewThread" in mock_run.call_args_list[0].args[0][-3]
    assert "unresolveReviewThread" in mock_run.call_args_list[1].args[0][-3]


@patch("reviewer.reviewer_resolution.run_gh")
def test_set_review_thread_resolved_rejects_a_mismatched_mutation_response(mock_run):
    mock_run.return_value = _mock_run(stdout=json.dumps({"data": {
        "resolveReviewThread": {
            "thread": {"id": "PRRT_123", "isResolved": False},
        },
    }}))

    assert set_review_thread_resolved("PRRT_123", resolved=True) is False


def test_absent_open_review_thread_is_replied_to_and_marked_for_resolution():
    finding = _finding()
    fingerprint = fingerprint_finding(finding)
    parent = _existing(fingerprint, 101)

    new, resolutions, unchanged = classify_findings(
        [], [parent],
        review_thread_states={101: ReviewThreadState("PRRT_101", False)},
    )

    assert new == []
    assert unchanged == 0
    assert len(resolutions) == 1
    action = resolutions[0][THREAD_ACTION_KEY]
    assert action == ReviewThreadAction("PRRT_101", 101, False, True)


def test_open_thread_with_prior_absence_reply_retries_close_without_duplicate_reply():
    finding = _finding()
    fingerprint = fingerprint_finding(finding)
    parent = _existing(fingerprint, 102)
    reply = {
        "id": 103,
        "in_reply_to_id": 102,
        "body": "Not raised this run. not verified fixed.\n<!-- corsia-review-resolved -->",
    }

    _, resolutions, _ = classify_findings(
        [], [parent, reply],
        review_thread_states={102: ReviewThreadState("PRRT_102", False)},
    )

    assert len(resolutions) == 1
    assert resolutions[0][THREAD_ACTION_KEY].reply_needed is False


def test_reappeared_finding_reopens_the_closed_thread_and_republishes():
    finding = _finding()
    fingerprint = fingerprint_finding(finding)
    parent = _existing(fingerprint, 104)
    reply = {
        "id": 105,
        "in_reply_to_id": 104,
        "body": "Not raised this run. not verified fixed.\n<!-- corsia-review-resolved -->",
    }

    new, resolutions, unchanged = classify_findings(
        [finding], [parent, reply],
        review_thread_states={104: ReviewThreadState("PRRT_104", True)},
    )

    assert resolutions == []
    assert unchanged == 0
    assert len(new) == 1
    assert new[0][THREAD_ACTION_KEY] == ReviewThreadAction(
        "PRRT_104", 104, True, True,
    )


def test_reappeared_finding_on_open_thread_is_replied_to_without_reopening():
    finding = _finding()
    fingerprint = fingerprint_finding(finding)
    parent = _existing(fingerprint, 106)
    reply = {
        "id": 107,
        "in_reply_to_id": 106,
        "body": "Not raised this run. not verified fixed.\n<!-- corsia-review-resolved -->",
    }

    new, _, _ = classify_findings(
        [finding], [parent, reply],
        review_thread_states={106: ReviewThreadState("PRRT_106", False)},
    )

    assert len(new) == 1
    assert new[0][THREAD_ACTION_KEY] == ReviewThreadAction(
        "PRRT_106", 106, False, True,
    )


def test_human_resolved_thread_is_not_reopened_without_prior_reviewer_absence():
    finding = _finding()
    fingerprint = fingerprint_finding(finding)
    parent = _existing(fingerprint, 107)

    new, resolutions, unchanged = classify_findings(
        [finding], [parent],
        review_thread_states={107: ReviewThreadState("PRRT_107", True)},
    )

    assert new == []
    assert resolutions == []
    assert unchanged == 1


def test_current_open_finding_is_unchanged_and_closed_absent_thread_is_ignored():
    finding = _finding()
    fingerprint = fingerprint_finding(finding)
    parent = _existing(fingerprint, 108)

    new, resolutions, unchanged = classify_findings(
        [finding], [parent],
        review_thread_states={108: ReviewThreadState("PRRT_108", False)},
    )
    assert new == []
    assert resolutions == []
    assert unchanged == 1

    new, resolutions, unchanged = classify_findings(
        [], [parent],
        review_thread_states={108: ReviewThreadState("PRRT_108", True)},
    )
    assert new == []
    assert resolutions == []
    assert unchanged == 0


def test_pr_level_findings_remain_outside_review_thread_mutations():
    finding = _finding(file="")
    fingerprint = fingerprint_finding(finding)
    parent = _existing_pr_level(fingerprint, 109)

    _, resolutions, _ = classify_findings(
        [], [parent], review_thread_states={},
    )

    assert len(resolutions) == 1
    assert THREAD_ACTION_KEY not in resolutions[0]
