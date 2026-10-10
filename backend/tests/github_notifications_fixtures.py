"""Shared fixtures for the GitHub notifications poller tests.

The webhook payloads keep the fields CORSIA reads from real stored deliveries
on hook 632110825 (``15d36dc0-b7d9-11f1-9938-e7a6a98748df``: issue_comment on a
PR; ``d1dcc9d0-b7d9-11f1-96c4-4850322abbbf``: pull_request_review_comment).
The REST objects the poller reads are those same objects plus the REST-only
keys observed live (``closed_by`` on an issue; counts and mergeability on a
pull), so a polled payload is built from a superset of the webhook's.
"""

from __future__ import annotations

import copy
import json
import subprocess
from typing import Any

REPO = "o/r"
PR = 42
CREATED = "2026-09-24T05:30:00Z"
FLOOR = "2026-09-24T05:00:00Z"
SERVER_DATE = "Thu, 24 Sep 2026 05:31:00 GMT"
LAST_MODIFIED = "Thu, 24 Sep 2026 05:30:05 GMT"


def _user(login: str, user_type: str = "User") -> dict[str, Any]:
    return {"login": login, "id": 7, "type": user_type, "site_admin": False}


def issue_object(number: int = PR, *, is_pr: bool = True) -> dict[str, Any]:
    issue = {
        "number": number, "title": "Test PR", "state": "open",
        "body": "Owner thread: X\nOwner thread ID: own-1",
        "user": _user("pr-author"), "author_association": "OWNER",
        "html_url": f"https://github.com/{REPO}/pull/{number}",
    }
    if is_pr:
        issue["pull_request"] = {
            "url": f"https://api.github.com/repos/{REPO}/pulls/{number}",
            "html_url": f"https://github.com/{REPO}/pull/{number}",
            "diff_url": "d", "patch_url": "p", "merged_at": None,
        }
    return issue


def pull_object(number: int = PR) -> dict[str, Any]:
    return {
        "number": number, "title": "Test PR", "state": "open", "draft": True,
        "body": "Owner thread: X\nOwner thread ID: own-1", "user": _user("pr-author"),
        "head": {"sha": "abc123", "ref": "feature"}, "base": {"ref": "dev"},
        "html_url": f"https://github.com/{REPO}/pull/{number}",
        "author_association": "OWNER", "labels": [],
    }


def comment_object(comment_id: int, *, body: str = "Please stop.", login: str = "reviewer-human",
                   user_type: str = "User", created_at: str = CREATED) -> dict[str, Any]:
    return {
        "id": comment_id, "body": body, "user": _user(login, user_type),
        "author_association": "COLLABORATOR", "created_at": created_at,
        "updated_at": created_at,
        "html_url": f"https://github.com/{REPO}/pull/{PR}#issuecomment-{comment_id}",
    }


def webhook_issue_comment(comment: dict[str, Any], *, is_pr: bool = True) -> dict[str, Any]:
    """An ``issue_comment`` webhook payload as GitHub delivers it."""
    return {
        "action": "created", "issue": issue_object(is_pr=is_pr), "comment": comment,
        "repository": {"full_name": REPO, "id": 1}, "sender": comment["user"],
    }


def webhook_review_comment(comment: dict[str, Any]) -> dict[str, Any]:
    """A ``pull_request_review_comment`` webhook payload as GitHub delivers it."""
    return {
        "action": "created", "pull_request": pull_object(), "comment": comment,
        "repository": {"full_name": REPO, "id": 1}, "sender": comment["user"],
    }


def notification_thread(
    number: int = PR, *, kind: str = "pulls", reason: str = "author",
) -> dict[str, Any]:
    subject_type = "PullRequest" if kind == "pulls" else "Issue"
    return {
        "id": str(number), "reason": reason, "updated_at": CREATED,
        "subject": {"type": subject_type, "url": f"https://api.github.com/repos/{REPO}/{kind}/{number}"},
        "repository": {"full_name": REPO, "id": 1},
    }


def _http(status: int, body: Any = None, headers: dict[str, str] | None = None) -> str:
    reason = {
        200: "OK", 304: "Not Modified", 401: "Unauthorized", 403: "Forbidden",
        404: "Not Found", 410: "Gone", 500: "Server Error", 502: "Bad Gateway",
    }[status]
    lines = [f"HTTP/2.0 {status} {reason}"]
    lines += [f"{name}: {value}" for name, value in (headers or {}).items()]
    text = "\r\n".join(lines) + "\r\n\r\n"
    return text + (json.dumps(body) if body is not None else "")


class FakeGh:
    """A recording ``gh`` runner that serves GET paths from a routing table."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.auth_rc = 0
        self.auth_stderr = "You are not logged into any GitHub hosts."
        self.missing = False
        self.notifications_status = 200
        self.threads: list[dict[str, Any]] = [notification_thread()]
        self.issue_comments: list[dict[str, Any]] = []
        self.review_comments: list[dict[str, Any]] = []
        self.issue = {**issue_object(), "closed_by": None}
        self.pull = {**pull_object(), "additions": 1, "mergeable": True, "comments": 0}
        #: Extra GET routes: a body to serve, or an int HTTP status to fail with.
        self.routes: dict[str, Any] = {}
        #: Whether every notifications page links a next page.
        self.notifications_next = False

    def api_calls(self) -> list[list[str]]:
        return [call for call in self.calls if call[:1] == ["api"]]

    def _route(self, path: str) -> str:
        base = path.split("?", 1)[0]
        if base == "notifications":
            headers = {"X-Poll-Interval": "60", "Last-Modified": LAST_MODIFIED, "Date": SERVER_DATE}
            if self.notifications_next:
                headers["Link"] = '<https://api.github.com/notifications?page=2>; rel="next"'
            if self.notifications_status != 200:
                return _http(self.notifications_status, None, headers)
            return _http(200, self.threads, headers)
        table = {
            f"repos/{REPO}/issues/{PR}/comments": self.issue_comments,
            f"repos/{REPO}/pulls/{PR}/comments": self.review_comments,
            f"repos/{REPO}/issues/{PR}": self.issue,
            f"repos/{REPO}/pulls/{PR}": self.pull,
            **self.routes,
        }
        if isinstance(table.get(base), int):
            return _http(table[base])
        if base not in table:
            raise AssertionError(f"unexpected GitHub read: {path}")
        return _http(200, copy.deepcopy(table[base]))

    def __call__(self, args: list[str], *, timeout: float = 30.0, **_: Any):
        self.calls.append(list(args))
        if self.missing:
            raise FileNotFoundError("gh")
        if args[:2] == ["auth", "status"]:
            return subprocess.CompletedProcess(args, self.auth_rc, "", self.auth_stderr)
        stdout = self._route(args[-1])
        returncode = 0 if stdout.startswith("HTTP/2.0 2") else 1
        return subprocess.CompletedProcess(args, returncode, stdout, "")
