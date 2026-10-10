from __future__ import annotations

def _bot_sender(login: str = "corsia-review[bot]") -> dict:
    return {"login": login, "type": "Bot"}


def _human_sender(login: str = "alice") -> dict:
    return {"login": login, "type": "User"}


_SUMMARY_MARKER = "<!-- corsia-review-summary -->"
_FP_MARKER = "<!-- corsia-review-fp: bug-001 -->"
_RESOLVED_MARKER = "<!-- corsia-review-resolved -->"


class TestCommentNoiseFilter:

    def test_bot_per_line_flag_is_noise(self):
        from events.github_mapper import map_github_event
        payload = {
            "action": "created",
            "issue": {"number": 5, "pull_request": {"url": "..."}},
            "comment": {"body": f"**Issue** {_FP_MARKER}"},
            "sender": _bot_sender(),
            "repository": {"full_name": "o/r"},
        }
        assert map_github_event("issue_comment", payload) is None

    def test_bot_resolved_reply_is_noise(self):
        from events.github_mapper import map_github_event
        payload = {
            "action": "created",
            "issue": {"number": 6, "pull_request": {"url": "..."}},
            "comment": {"body": f"Resolved. {_RESOLVED_MARKER}"},
            "sender": _bot_sender(),
            "repository": {"full_name": "o/r"},
        }
        assert map_github_event("issue_comment", payload) is None

    def test_unknown_bot_comment_not_dropped(self):
        from events.github_mapper import map_github_event
        payload = {
            "action": "created",
            "issue": {"number": 7, "pull_request": {"url": "..."}},
            "comment": {"body": "Automated check passed."},
            "sender": _bot_sender(login="other-bot[bot]"),
            "repository": {"full_name": "o/r"},
        }
        assert map_github_event("issue_comment", payload) == "webhook.github.pr_comment"

    def test_bot_review_summary_is_kept(self):
        from events.github_mapper import map_github_event
        payload = {
            "action": "created",
            "issue": {"number": 8, "pull_request": {"url": "..."}},
            "comment": {"body": f"{_SUMMARY_MARKER}\n## Review\n3 findings."},
            "sender": _bot_sender(),
            "repository": {"full_name": "o/r"},
        }
        assert map_github_event("issue_comment", payload) == "webhook.github.pr_comment"

    def test_human_comment_always_kept(self):
        from events.github_mapper import map_github_event
        payload = {
            "action": "created",
            "issue": {"number": 9, "pull_request": {"url": "..."}},
            "comment": {"body": "Looks good to me!"},
            "sender": _human_sender(),
            "repository": {"full_name": "o/r"},
        }
        assert map_github_event("issue_comment", payload) == "webhook.github.pr_comment"

    def test_human_comment_with_fp_marker_is_kept(self):
        from events.github_mapper import map_github_event
        payload = {
            "action": "created",
            "issue": {"number": 10, "pull_request": {"url": "..."}},
            "comment": {"body": f"Is this marker intentional? {_FP_MARKER}"},
            "sender": _human_sender(),
            "repository": {"full_name": "o/r"},
        }
        assert map_github_event("issue_comment", payload) == "webhook.github.pr_comment"

    def test_bot_review_comment_is_noise(self):
        from events.github_mapper import map_github_event
        payload = {
            "action": "created",
            "comment": {"body": f"NIT: rename this. {_FP_MARKER}"},
            "sender": _bot_sender(),
            "pull_request": {"number": 11},
            "repository": {"full_name": "o/r"},
        }
        assert map_github_event("pull_request_review_comment", payload) is None

    def test_human_review_comment_is_kept(self):
        from events.github_mapper import map_github_event
        payload = {
            "action": "created",
            "comment": {"body": "Should this be async?"},
            "sender": _human_sender(),
            "pull_request": {"number": 12},
            "repository": {"full_name": "o/r"},
        }
        assert map_github_event("pull_request_review_comment", payload) == "webhook.github.pr_comment"

    def test_bot_login_suffix_without_noise_marker_passes_through(self):
        from events.github_mapper import map_github_event
        payload = {
            "action": "created",
            "issue": {"number": 13, "pull_request": {"url": "..."}},
            "comment": {"body": "automated nit"},
            "sender": {"login": "some-tool[bot]", "type": "User"},
            "repository": {"full_name": "o/r"},
        }
        assert map_github_event("issue_comment", payload) == "webhook.github.pr_comment"
