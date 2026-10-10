from __future__ import annotations

from unittest.mock import MagicMock


def _finding(**kw) -> dict:
    base = {
        "severity": "flag",
        "file": "backend/foo.py",
        "line": 42,
        "category": "correctness/logic-error",
        "description": "Some description text.",
        "cited_section": None,
    }
    base.update(kw)
    return base


def _existing(fp: str, comment_id: int, in_reply_to: int | None = None) -> dict:
    body = f"existing body <!-- corsia-review-fp: {fp} -->"
    return {
        "id": comment_id,
        "body": body,
        "in_reply_to_id": in_reply_to,
        "pull_request_url": "https://api.github.com/repos/x/y/pulls/123",
    }


def _existing_pr_level(fp: str, comment_id: int) -> dict:
    """A PR-level (issue) comment carrying a finding that had no file anchor."""
    return {
        "id": comment_id,
        "body": f"finding body\n\n<!-- corsia-review-fp: {fp} -->",
        "issue_url": "https://api.github.com/repos/x/y/issues/123",
    }


def _pr_level_resolution(fp: str, comment_id: int) -> dict:
    """The standalone comment that resolves a PR-level finding comment."""
    return {
        "id": comment_id,
        "body": (
            "Resolved — finding no longer present in HEAD (`abc12345`).\n\n"
            "<!-- corsia-review-resolved -->\n"
            f"<!-- corsia-review-fp: {fp} -->"
        ),
        "issue_url": "https://api.github.com/repos/x/y/issues/123",
    }


def _mock_run(returncode=0, stdout="", stderr=""):
    mock = MagicMock()
    mock.returncode = returncode
    mock.stdout = stdout
    mock.stderr = stderr
    return mock
