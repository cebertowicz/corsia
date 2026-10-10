from __future__ import annotations


class TestCORSIASelfCommentSuppression:

    def test_summary_marker_suppressed(self):
        from events.ingress import _is_corsia_self_comment
        payload = {
            "action": "created",
            "comment": {"body": "Review done.\n\n<!-- corsia-review-summary -->"},
        }
        assert _is_corsia_self_comment("webhook.github.pr_comment", payload) is True

    def test_resolved_marker_suppressed(self):
        from events.ingress import _is_corsia_self_comment
        payload = {
            "action": "created",
            "comment": {
                "body": (
                    "Resolved — finding no longer present in HEAD (`abc12345`).\n\n"
                    "<!-- corsia-review-resolved -->"
                ),
            },
        }
        assert _is_corsia_self_comment("webhook.github.pr_comment", payload) is True

    def test_fp_marker_suppressed(self):
        from events.ingress import _is_corsia_self_comment
        payload = {
            "action": "created",
            "comment": {
                "body": ":no_entry_sign: **BLOCK**\n\nBad code.\n\n<!-- corsia-review-fp: deadbeef12345678 -->",
            },
        }
        assert _is_corsia_self_comment("webhook.github.issue_comment", payload) is True

    def test_normal_comment_not_suppressed(self):
        from events.ingress import _is_corsia_self_comment
        payload = {
            "action": "created",
            "comment": {"body": "LGTM! Nice work."},
        }
        assert _is_corsia_self_comment("webhook.github.pr_comment", payload) is False

    def test_marker_mid_paragraph_not_suppressed(self):
        from events.ingress import _is_corsia_self_comment
        payload = {
            "action": "created",
            "comment": {
                "body": (
                    "Is this marker intentional? "
                    "<!-- corsia-review-summary --> I think it was a copy-paste."
                ),
            },
        }
        assert _is_corsia_self_comment("webhook.github.pr_comment", payload) is False

    def test_fp_marker_mid_line_not_suppressed(self):
        from events.ingress import _is_corsia_self_comment
        payload = {
            "action": "created",
            "comment": {
                "body": "See this finding: <!-- corsia-review-fp: deadbeef12 --> agree?",
            },
        }
        assert _is_corsia_self_comment("webhook.github.pr_comment", payload) is False

    def test_resolved_marker_in_quote_not_suppressed(self):
        from events.ingress import _is_corsia_self_comment
        payload = {
            "action": "created",
            "comment": {
                "body": (
                    "Replying to the reviewer:\n"
                    "> <!-- corsia-review-resolved -->\n"
                    "Is this marker still needed?"
                ),
            },
        }
        assert _is_corsia_self_comment("webhook.github.pr_comment", payload) is False

    def test_non_comment_event_not_suppressed(self):
        from events.ingress import _is_corsia_self_comment
        payload = {
            "action": "opened",
            "pull_request": {"number": 1},
        }
        assert _is_corsia_self_comment("webhook.github.pr_opened", payload) is False

    async def test_summary_marker_route_leg_not_called(self):
        from unittest.mock import AsyncMock, patch
        from events.ingress import route_leg

        payload = {
            "action": "created",
            "comment": {"body": "Summary here\n\n<!-- corsia-review-summary -->"},
        }

        mock_route_event = AsyncMock()
        with patch("events.ingress.route_event", mock_route_event):
            result = await route_leg("webhook.github.pr_comment", payload)

        assert result is None
        mock_route_event.assert_not_called()

    async def test_resolved_marker_route_leg_not_called(self):
        from unittest.mock import AsyncMock, patch
        from events.ingress import route_leg

        payload = {
            "action": "created",
            "comment": {"body": "Done.\n\n<!-- corsia-review-resolved -->"},
        }

        mock_route_event = AsyncMock()
        with patch("events.ingress.route_event", mock_route_event):
            result = await route_leg("webhook.github.pr_comment", payload)

        assert result is None
        mock_route_event.assert_not_called()

    async def test_fp_marker_route_leg_not_called(self):
        from unittest.mock import AsyncMock, patch
        from events.ingress import route_leg

        payload = {
            "action": "created",
            "comment": {"body": "Finding.\n\n<!-- corsia-review-fp: abcd1234efgh5678 -->"},
        }

        mock_route_event = AsyncMock()
        with patch("events.ingress.route_event", mock_route_event):
            result = await route_leg("webhook.github.issue_comment", payload)

        assert result is None
        mock_route_event.assert_not_called()

    async def test_normal_comment_route_leg_calls_route_event(self):
        from unittest.mock import AsyncMock, patch
        from events.ingress import route_leg

        payload = {
            "action": "created",
            "comment": {"body": "Please fix this."},
        }

        mock_result = object()
        mock_route_event = AsyncMock(return_value=mock_result)
        with patch("events.ingress.route_event", mock_route_event):
            result = await route_leg("webhook.github.pr_comment", payload)

        assert result is mock_result
        mock_route_event.assert_called_once()

    def test_pr_review_comment_with_marker_suppressed_via_dispatcher(self):
        from events.github_mapper import map_github_event
        from events.ingress import _is_corsia_self_comment

        gh_payload = {
            "action": "created",
            "comment": {
                "body": "Finding here.\n\n<!-- corsia-review-fp: abcdef1234567890 -->",
            },
            "sender": {"login": "human-reviewer", "type": "User"},
        }
        internal_event = map_github_event("pull_request_review_comment", gh_payload)
        assert internal_event is not None, (
            "pull_request_review_comment must map to a non-None internal event "
            "(bot-sender filter only drops when sender is a bot with a noise marker)"
        )
        assert _is_corsia_self_comment(internal_event, gh_payload) is True, (
            f"internal event '{internal_event}' produced by pull_request_review_comment "
            "must be suppressed by _is_corsia_self_comment — "
            "check that it appears in _COMMENT_EVENT_TYPES"
        )

    def test_issue_comment_on_pr_with_marker_suppressed_via_dispatcher(self):
        from events.github_mapper import map_github_event
        from events.ingress import _is_corsia_self_comment

        gh_payload = {
            "action": "created",
            "issue": {
                "number": 42,
                "pull_request": {"url": "https://api.github.com/repos/o/r/pulls/42"},
            },
            "comment": {
                "body": "## Review\n\n3 findings.\n\n<!-- corsia-review-summary -->",
            },
            "sender": {"login": "human-reviewer", "type": "User"},
            "repository": {"full_name": "owner/repo"},
        }
        internal_event = map_github_event("issue_comment", gh_payload)
        assert internal_event is not None
        assert _is_corsia_self_comment(internal_event, gh_payload) is True

    async def test_handle_event_suppressed_shape(self):
        from events.ingress import handle_event

        payload = {
            "action": "created",
            "comment": {"body": "Summary.\n\n<!-- corsia-review-summary -->"},
        }
        result = await handle_event(
            "webhook.github.pr_comment", payload, delivery_id="test-del-1"
        )
        assert result["internal_event"] == "webhook.github.pr_comment"
        assert result["delivery_id"] == "test-del-1"
        assert result["route_outcome"] == "suppressed_self_comment"
        assert result["thread_id"] is None
        assert "reviewer" not in result

    def test_reviewer_composed_fp_comment_suppressed(self):
        from reviewer.reviewer_threads import format_threaded_comment_body
        from events.ingress import _is_corsia_self_comment

        finding = {
            "severity": "FLAG",
            "category": "test/integration",
            "description": "A sample finding for integration test.",
            "path": "backend/foo.py",
            "line": 42,
        }
        body = format_threaded_comment_body(finding)
        payload = {"action": "created", "comment": {"body": body}}
        assert _is_corsia_self_comment("webhook.github.pr_comment", payload) is True, (
            "Reviewer-composed FP comment must be suppressed. "
            "This means format_finding_comment placed the marker on its own line "
            "(preceded by \\n) as required by the standalone-line anchor."
        )

    def test_reviewer_composed_summary_comment_suppressed(self):
        from events.markers import REVIEW_SUMMARY_MARKER
        from events.ingress import _is_corsia_self_comment

        body_text = "## Review\n\n2 open findings."
        summary_body = f"{body_text}\n\n{REVIEW_SUMMARY_MARKER}"
        payload = {"action": "created", "comment": {"body": summary_body}}
        assert _is_corsia_self_comment("webhook.github.pr_comment", payload) is True

    def test_reviewer_composed_resolved_reply_suppressed(self):
        from events.markers import REVIEW_RESOLVED_MARKER
        from events.ingress import _is_corsia_self_comment

        head_sha = "abcdef1234567890"
        resolved_body = (
            f"Resolved — finding no longer present in HEAD (`{head_sha[:8]}`).\n\n"
            f"{REVIEW_RESOLVED_MARKER}"
        )
        payload = {"action": "created", "comment": {"body": resolved_body}}
        assert _is_corsia_self_comment("webhook.github.pr_comment", payload) is True, (
            "Reviewer-composed resolved-reply body must be suppressed. "
            "Check that post_resolve_reply still places the marker on its own trailing line."
        )

    def test_post_review_summary_places_marker_on_standalone_line(self):
        from reviewer.reviewer_threads import build_summary_body
        from events.markers import REVIEW_SUMMARY_MARKER

        body_text = "## Review\n\n2 open findings."
        marker_body = build_summary_body(body_text)

        lines = marker_body.splitlines()
        assert REVIEW_SUMMARY_MARKER in lines, (
            "REVIEW_SUMMARY_MARKER must appear as a standalone line in the "
            "build_summary_body output — the ingress detector requires it. "
            "If the body format changed, update both the composer and the spec."
        )

    def test_post_resolve_reply_places_marker_on_standalone_line(self):
        from reviewer.reviewer_threads import build_resolve_reply_body
        from events.markers import REVIEW_RESOLVED_MARKER

        evidence_report = "No cited lines; not verified fixed."
        body = build_resolve_reply_body(evidence_report)

        lines = body.splitlines()
        assert REVIEW_RESOLVED_MARKER in lines, (
            "REVIEW_RESOLVED_MARKER must appear as a standalone line in the "
            "build_resolve_reply_body output — the ingress detector requires it."
        )

    def test_format_threaded_comment_body_places_fp_marker_on_standalone_line(self):
        from reviewer.reviewer_threads import format_threaded_comment_body
        from events.markers import REVIEW_FP_MARKER_PREFIX

        finding = {
            "severity": "FLAG",
            "category": "test/marker-placement",
            "description": "Marker placement assertion.",
            "path": "backend/foo.py",
            "line": 1,
        }
        body = format_threaded_comment_body(finding)
        lines = body.splitlines()
        marker_lines = [ln for ln in lines if ln.startswith(REVIEW_FP_MARKER_PREFIX)]
        assert len(marker_lines) == 1, (
            f"Expected exactly one FP marker line in format_threaded_comment_body output. "
            f"Got {len(marker_lines)}. Body:\n{body}"
        )
