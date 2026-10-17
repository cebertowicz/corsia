"""Publication contract: the reviewer never loses a finding it made (#946).

Before this contract a finding with no ``file`` anchor was counted as failed
without any post being attempted, and every shortfall — a lost nit, a lost
block, a GitHub rejection — surfaced as one aggregate number.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reviewer.review_publication import (  # noqa: E402
    CHANNEL_FILE,
    CHANNEL_LINE,
    CHANNEL_NONE,
    CHANNEL_PR_LEVEL,
    CHANNEL_THREAD,
    FindingDelivery,
    ReviewPublication,
    ReviewPublicationError,
    publish_findings,
    publish_review,
    reconcile_publication,
)
from reviewer.reviewer_threads import THREAD_ACTION_KEY, ReviewThreadAction  # noqa: E402
from reviewer_threads_fixtures import _finding  # noqa: E402


class _Poster:
    """Record every publication call and answer from a scripted result list."""

    def __init__(self, results: list[bool] | bool = True):
        self.calls: list[tuple[str, dict]] = []
        self._results = results

    def __call__(self, api_path: str, payload: dict) -> bool:
        self.calls.append((api_path, payload))
        if isinstance(self._results, bool):
            return self._results
        return self._results[len(self.calls) - 1]

    def paths(self) -> list[str]:
        return [path for path, _ in self.calls]


def _publish(findings, poster):
    with patch("reviewer.review_publication.gh_api_post_json", poster):
        return publish_findings(42, "owner/repo", "sha123", findings)


# -- Delivery: a finding is never dropped ---------------------------------


@pytest.mark.parametrize("missing", ["", None], ids=["empty-file", "null-file"])
def test_finding_without_file_anchor_is_published_at_pr_level(missing):
    """The #946 defect: this finding used to be discarded with no post at all."""
    poster = _Poster()
    publication = _publish([_finding(file=missing, severity="block")], poster)

    assert poster.paths() == ["repos/owner/repo/issues/42/comments"]
    delivery = publication.deliveries[0]
    assert delivery.delivered is True
    assert delivery.channel == CHANNEL_PR_LEVEL
    assert publication.undelivered == []
    assert publication.lost_blocks == []


def test_anchored_rejection_falls_back_to_pr_level_comment():
    """A path/line GitHub will not accept must not cost the finding."""
    poster = _Poster([False, True])
    publication = _publish([_finding(file="not-in-diff.py", line=9)], poster)

    assert poster.paths() == [
        "repos/owner/repo/pulls/42/comments",
        "repos/owner/repo/issues/42/comments",
    ]
    assert publication.deliveries[0].channel == CHANNEL_PR_LEVEL
    assert publication.pr_level == publication.deliveries


def test_finding_is_lost_only_when_every_channel_fails(caplog):
    poster = _Poster(False)
    with caplog.at_level(logging.ERROR):
        publication = _publish([_finding(description="Architectural drift.")], poster)

    assert poster.paths() == [
        "repos/owner/repo/pulls/42/comments",
        "repos/owner/repo/issues/42/comments",
    ]
    assert publication.deliveries[0].channel == CHANNEL_NONE
    assert publication.undelivered == publication.deliveries
    # Findings are persisted nowhere else, so the log is the last copy.
    assert "Architectural drift." in caplog.text


def test_anchored_findings_use_line_and_file_channels():
    poster = _Poster()
    publication = _publish(
        [_finding(file="a.py", line=10), _finding(file="b.py", line=0)], poster,
    )

    assert poster.paths() == ["repos/owner/repo/pulls/42/comments"] * 2
    line_payload, file_payload = (payload for _, payload in poster.calls)
    assert line_payload["line"] == 10
    assert line_payload["side"] == "RIGHT"
    assert "subject_type" not in line_payload
    assert file_payload["subject_type"] == "file"
    assert "line" not in file_payload
    assert [d.channel for d in publication.deliveries] == [
        CHANNEL_LINE, CHANNEL_FILE,
    ]


def test_one_rejection_does_not_stop_the_remaining_findings():
    poster = _Poster([True, False, True, True])
    publication = _publish(
        [_finding(file=f"f{i}.py", line=10) for i in range(3)], poster,
    )

    assert [d.channel for d in publication.deliveries] == [
        CHANNEL_LINE, CHANNEL_PR_LEVEL, CHANNEL_LINE,
    ]
    assert publication.undelivered == []


def test_no_findings_posts_nothing():
    poster = _Poster()
    publication = _publish([], poster)

    assert poster.calls == []
    assert publication.deliveries == []


def test_approach_reset_groups_evidence_into_one_pull_comment():
    """A pulls/comments-only reader must see STOP, never a local patch queue."""
    from reviewer import review_publication

    approach = _finding(
        severity="block",
        file="",
        line=0,
        category="review-process/approach-churn",
        description="Several naming errors share one transformation defect.",
    )
    local = _finding(
        severity="block",
        file="backend/example.py",
        line=19,
        category="standards/eng-arch",
        cited_section="EAS5",
        description="A provider session is mislabeled as a thread.",
    )

    assert hasattr(review_publication, "prepare_findings_for_publication")
    grouped = review_publication.prepare_findings_for_publication(
        [approach, local]
    )
    poster = _Poster()
    publication = _publish(grouped, poster)

    assert len(grouped) == 1
    assert "A provider session is mislabeled as a thread." in (
        grouped[0]["description"]
    )
    assert poster.paths() == ["repos/owner/repo/pulls/42/comments"]
    assert poster.calls[0][1]["body"].startswith(
        ":no_entry_sign: **STOP — APPROACH RESET REQUIRED**"
    )
    assert publication.deliveries[0].channel == CHANNEL_LINE


def test_approach_reset_uses_reviewed_diff_file_when_it_has_no_local_anchor():
    """An aggregate-only result must still reach pulls/comments readers."""
    import inspect

    from reviewer import review_publication

    approach = _finding(
        severity="block",
        file="",
        line=0,
        category="review-process/approach-churn",
    )

    assert "fallback_file" in inspect.signature(
        review_publication.prepare_findings_for_publication
    ).parameters
    grouped = review_publication.prepare_findings_for_publication(
        [approach], fallback_file="backend/example.py",
    )
    poster = _Poster()
    publication = _publish(grouped, poster)

    assert poster.paths() == ["repos/owner/repo/pulls/42/comments"]
    assert poster.calls[0][1]["path"] == "backend/example.py"
    assert poster.calls[0][1]["subject_type"] == "file"
    assert publication.deliveries[0].channel == CHANNEL_FILE


# -- Reporting: a shortfall names the findings it lost --------------------


def _delivery(severity="flag", **kw) -> FindingDelivery:
    return FindingDelivery(
        _finding(severity=severity, **kw), CHANNEL_NONE, "no file anchor",
    )


def test_lost_block_reports_severity_rule_and_location():
    lost = _delivery(
        severity="block", category="correctness/race-condition",
        file="backend/x.py", line=12,
    )
    errors = reconcile_publication(
        publication=ReviewPublication([lost]),
        declared_new=1, declared_resolved=0,
        resolutions_posted=0, summary_posted=True,
    )

    assert len(errors) == 1
    assert "BLOCK" in errors[0]
    assert "[correctness/race-condition]" in errors[0]
    assert "backend/x.py:12" in errors[0]
    assert "no file anchor" in errors[0]


def test_lost_block_is_named_before_other_shortfalls():
    """The GitHub status is truncated; a lost block has to survive that."""
    errors = reconcile_publication(
        publication=ReviewPublication([
            _delivery(severity="nit", file="a.py", line=1),
            _delivery(severity="block", file="b.py", line=2),
        ]),
        declared_new=2, declared_resolved=1,
        resolutions_posted=0, summary_posted=False,
    )

    assert errors[0].startswith("BLOCK ")
    assert "NIT " in errors[1]
    assert any("summary" in e for e in errors)
    assert any("resolution" in e for e in errors)


def test_declared_new_count_must_match_the_delivery_record():
    """The summary declares N new; a record covering fewer is a silent drop."""
    errors = reconcile_publication(
        publication=ReviewPublication([]),
        declared_new=2, declared_resolved=0,
        resolutions_posted=0, summary_posted=True,
    )

    assert errors == ["delivery record covers 0 of 2 declared new finding(s)"]


def test_declared_resolutions_must_match_the_replies_posted():
    errors = reconcile_publication(
        publication=ReviewPublication([]),
        declared_new=0, declared_resolved=3,
        resolutions_posted=1, summary_posted=True,
    )

    assert errors == [
        "2 of 3 declared resolution reply/replies were not published"
    ]


def test_a_fully_published_review_reconciles_clean():
    errors = reconcile_publication(
        publication=ReviewPublication([
            FindingDelivery(_finding(), CHANNEL_LINE),
            FindingDelivery(_finding(file="b.py"), CHANNEL_PR_LEVEL, "no file anchor"),
        ]),
        declared_new=2, declared_resolved=1,
        resolutions_posted=1, summary_posted=True,
    )

    assert errors == []


# -- publish_review: reconcile before reporting success -------------------


def _publish_review(findings, *, poster, summary_ok=True, resolve_replies=()):
    with patch("reviewer.review_publication.gh_api_post_json", poster), \
         patch(
             "reviewer.review_publication.post_review_summary",
             return_value=summary_ok,
         ) as summary, \
         patch(
             "reviewer.review_thread_publication.post_resolve_reply", return_value=True,
         ):
        publication = publish_review(
            pr_number=42, repo="owner/repo", head_sha="sha123",
            findings=list(findings), new_threads=list(findings),
            resolve_replies=list(resolve_replies), unchanged_count=0,
            model_label="codex", existing_issue_comments=[],
        )
    return publication, summary


def test_publish_review_returns_the_delivery_record_when_all_findings_land():
    publication, summary = _publish_review([_finding()], poster=_Poster())

    assert publication.undelivered == []
    summary.assert_called_once()


def test_publish_review_hard_fails_distinctly_on_a_lost_block():
    with pytest.raises(ReviewPublicationError) as exc_info:
        _publish_review(
            [_finding(severity="block", file="backend/x.py", line=12)],
            poster=_Poster(False),
        )

    assert exc_info.value.lost_block is True
    message = str(exc_info.value)
    assert message.startswith("review LOST 1 BLOCK finding(s): BLOCK ")
    assert "backend/x.py:12" in message


def test_publish_review_failure_on_a_lost_nit_is_not_a_lost_block():
    with pytest.raises(ReviewPublicationError) as exc_info:
        _publish_review(
            [_finding(severity="nit", file="backend/x.py", line=3)],
            poster=_Poster(False),
        )

    assert exc_info.value.lost_block is False
    assert str(exc_info.value).startswith("review publication failed: NIT ")


def test_publish_review_fails_when_the_summary_does_not_land():
    with pytest.raises(ReviewPublicationError) as exc_info:
        _publish_review([_finding()], poster=_Poster(), summary_ok=False)

    assert "review summary comment was not published" in str(exc_info.value)


def test_summary_is_composed_after_publication_with_the_fallback_count():
    """The summary tells the reader where to look, so it must know."""
    poster = _Poster([False, True])
    with patch("reviewer.review_publication.gh_api_post_json", poster), \
         patch("reviewer.review_publication.post_review_summary", return_value=True), \
         patch(
             "reviewer.review_publication.format_summary_body", return_value="body",
         ) as format_summary:
        publish_review(
            pr_number=42, repo="owner/repo", head_sha="sha123",
            findings=[_finding()], new_threads=[_finding()],
            resolve_replies=[], unchanged_count=0,
            model_label="codex", existing_issue_comments=[],
        )

    assert format_summary.call_args.kwargs["pr_level_count"] == 1
    assert format_summary.call_args.kwargs["new_count"] == 1


def test_reappeared_finding_reopens_and_republishes_in_original_thread():
    finding = _finding()
    finding[THREAD_ACTION_KEY] = ReviewThreadAction("PRRT_1", 123, True)
    with patch(
        "reviewer.review_thread_publication.set_review_thread_resolved", return_value=True,
    ) as set_resolved, patch(
        "reviewer.review_thread_publication.post_review_thread_reply", return_value=True,
    ) as post_reply, patch(
        "reviewer.review_publication.post_review_summary", return_value=True,
    ), patch("reviewer.review_publication.format_summary_body", return_value="summary"):
        publication = publish_review(
            pr_number=42, repo="owner/repo", head_sha="a" * 40,
            findings=[finding], new_threads=[finding], resolve_replies=[],
            unchanged_count=0, model_label="claude", existing_issue_comments=[],
        )

    set_resolved.assert_called_once_with("PRRT_1", resolved=False)
    post_reply.assert_called_once()
    assert post_reply.call_args.args[:3] == (123, 42, "owner/repo")
    assert publication.deliveries[0].channel == CHANNEL_THREAD


def test_reappeared_finding_falls_back_to_new_publication_if_reopen_fails():
    finding = _finding()
    finding[THREAD_ACTION_KEY] = ReviewThreadAction("PRRT_1", 123, True)
    with patch(
        "reviewer.review_thread_publication.set_review_thread_resolved", return_value=False,
    ), patch(
        "reviewer.review_thread_publication.post_review_thread_reply",
    ) as post_reply, patch(
        "reviewer.review_publication.gh_api_post_json", return_value=True,
    ) as post_new, patch(
        "reviewer.review_publication.post_review_summary", return_value=True,
    ), patch("reviewer.review_publication.format_summary_body", return_value="summary"):
        publication = publish_review(
            pr_number=42, repo="owner/repo", head_sha="a" * 40,
            findings=[finding], new_threads=[finding], resolve_replies=[],
            unchanged_count=0, model_label="claude", existing_issue_comments=[],
        )

    post_reply.assert_not_called()
    assert post_new.called
    assert publication.deliveries[0].channel == CHANNEL_LINE


def _publish_resolution(*, reply_ok, resolve_ok, reply_needed=True):
    action = ReviewThreadAction("PRRT_1", 123, False, reply_needed=reply_needed)
    comment = {"id": 123, THREAD_ACTION_KEY: action}
    with patch(
        "reviewer.review_thread_publication.post_resolve_reply", return_value=reply_ok,
    ) as post_reply, patch(
        "reviewer.review_thread_publication.set_review_thread_resolved",
        return_value=resolve_ok,
    ) as set_resolved, patch(
        "reviewer.review_thread_publication._emit_lifecycle",
    ) as emit_event, patch(
        "reviewer.review_publication.post_review_summary", return_value=True,
    ), patch("reviewer.review_publication.format_summary_body", return_value="summary"):
        publish_review(
            pr_number=42, repo="owner/repo", head_sha="a" * 40,
            findings=[], new_threads=[], resolve_replies=[comment],
            unchanged_count=0, model_label="claude", existing_issue_comments=[],
        )
    return post_reply, set_resolved, emit_event


def test_failed_resolve_after_a_landed_reply_keeps_the_verdict_and_stays_visible():
    """The reply is the declared item; a failed resolve leaves the thread open."""
    post_reply, set_resolved, emit_event = _publish_resolution(
        reply_ok=True, resolve_ok=False,
    )

    post_reply.assert_called_once()
    set_resolved.assert_called_once_with("PRRT_1", resolved=True)
    emit_event.assert_called_once_with(42, "a" * 40, "PRRT_1", "resolve", False)


def test_retried_resolve_on_an_already_replied_thread_keeps_the_verdict():
    post_reply, set_resolved, emit_event = _publish_resolution(
        reply_ok=True, resolve_ok=False, reply_needed=False,
    )

    post_reply.assert_not_called()
    set_resolved.assert_called_once_with("PRRT_1", resolved=True)
    emit_event.assert_called_once_with(42, "a" * 40, "PRRT_1", "resolve", False)


def test_resolution_reply_that_did_not_land_is_still_counted_missing():
    with pytest.raises(ReviewPublicationError, match="1 of 1 declared resolution"):
        _publish_resolution(reply_ok=False, resolve_ok=True)
