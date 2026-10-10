"""Close issues named with a closing keyword in a merged PR body (issue #2047).

GitHub's own auto-close only fires when a pull request merges into the
repository's default branch. This repo's fixes land on ``dev`` while the
default branch is ``main``, so ``Closes/Fixes/Resolves #N`` in PR bodies never
fire and issues rot open. The ``close-on-dev-merge`` workflow runs this module
when a PR merges into ``dev``; it reproduces GitHub's keyword semantics:

- ``Closes/Fixes/Resolves`` (any tense/case, optional colon) close the issues
  they name, including comma / ``and`` / ``&`` lists.
- ``Refs #N`` never closes.
- Cross-repo references (``other/repo#N``) never close a local issue.
- A reference to a pull-request number is skipped (shared numbering).

Writes are loud: any referenced issue that cannot be resolved exits non-zero so
the workflow run shows red instead of silently missing a close.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request

API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"

# A short ``#N`` must not be preceded by a word char, path separator or another
# ref fragment, otherwise ``other/repo#19`` would look like a local reference.
_SHORT_REF = r"(?<![\w/.-])#(\d+)"
_GH_REF = r"(?:https?://github\.com/)?{repo}#(\d+)"
_ISSUE_URL_REF = r"(?:https?://github\.com/)?{repo}/issues/(\d+)"
_SEP = r"(?:\s*[,;&]\s*|\s+and\s+)"


def _ref_pattern(repo: str) -> re.Pattern[str]:
    repo_esc = re.escape(repo)
    # Order matters: the more specific repo/URL forms first, then a bare ``#N``
    # guarded against being part of a ``other/repo#N`` cross-repo reference.
    ref = f"(?:{_ISSUE_URL_REF.format(repo=repo_esc)}|{_GH_REF.format(repo=repo_esc)}|{_SHORT_REF})"
    return re.compile(ref)


def _closing_pattern(repo: str) -> re.Pattern[str]:
    ref = _ref_pattern(repo).pattern
    return re.compile(
        r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s*"
        + ref
        + f"(?:{_SEP}{ref})*",
        re.IGNORECASE,
    )


def extract_closing_issues(body: str | None, repo: str) -> list[int]:
    """Return issue numbers named by closing keywords, in first-seen order.

    Mirrors GitHub's closing-keyword grammar: a keyword, optional colon, then
    a local reference or a comma / ``and`` / ``&`` list of them (``Closes #1,
    #2 and #3``). Each issue otherwise needs its own keyword: a bare reference
    after whitespace is not chained. Plain ``Refs`` never closes. A reference
    is *local* only when it is a bare ``#N``, a same-repo ``owner/repo#N``, or
    an issue URL for this repo; a cross-repo reference or a pull-request-style
    ``owner/repo/pull/N`` is not a valid anchor, so a list that starts with one
    does not close. Under-closing is preferred to mis-closing: a stale open
    issue is fixed by a human, a wrongly-closed issue hides unfinished work.

    All references in each keyword-governed span are collected by re-scanning
    the span, because a capture group inside a repeated group would keep only
    the last iteration and silently drop middle list items.
    """
    if not body:
        return []
    ref_re = _ref_pattern(repo)
    found: list[int] = []
    seen: set[int] = set()
    for match in _closing_pattern(repo).finditer(body):
        for ref_match in ref_re.finditer(match.group(0)):
            number = next(int(g) for g in ref_match.groups() if g is not None)
            if number not in seen:
                seen.add(number)
                found.append(number)
    return found


class GitHub:
    """Minimal GitHub REST client for the three calls this module needs."""

    def __init__(self, token: str, repo: str) -> None:
        self._token = token
        self.repo = repo

    def _request(
        self, method: str, path: str, payload: dict | None = None
    ) -> tuple[int, dict | list | None]:
        req = urllib.request.Request(
            f"{API_ROOT}/repos/{self.repo}{path}",
            method=method,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "corsia-close-on-dev-merge",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            return exc.code, None

    def issue(self, number: int) -> tuple[int, dict | None]:
        status, data = self._request("GET", f"/issues/{number}")
        return status, (data if isinstance(data, dict) else None)

    def comment(self, number: int, text: str) -> int:
        status, _ = self._request("POST", f"/issues/{number}/comments", {"body": text})
        return status

    def close(self, number: int) -> int:
        status, _ = self._request(
            "PATCH", f"/issues/{number}", {"state": "closed", "state_reason": "completed"}
        )
        return status


def resolve(gh: GitHub, pr_number: int, merge_sha: str, run_url: str, numbers: list[int]) -> list[str]:
    """Close each still-open referenced issue with a provenance comment.

    Returns a list of human-readable failures (empty on success). Pull-request
    numbers and already-closed issues are skipped without failing.
    """
    failures: list[str] = []
    short_sha = merge_sha[:7] if merge_sha else "unknown SHA"
    for number in numbers:
        if number == pr_number:
            print(f"#{number}: the merged PR itself; skipping")
            continue
        status, issue = gh.issue(number)
        if status != 200 or issue is None:
            failures.append(f"#{number}: lookup failed with HTTP {status}")
            continue
        if "pull_request" in issue:
            print(f"#{number}: is a pull request; skipping")
            continue
        if issue.get("state") != "open":
            print(f"#{number}: already {issue.get('state')}; skipping")
            continue
        text = (
            f"Closed by PR #{pr_number} merging into `dev` as {short_sha} "
            f"(close-on-merge automation, [workflow run]({run_url}))."
        )
        comment_status = gh.comment(number, text)
        if comment_status not in (200, 201):
            failures.append(f"#{number}: comment failed with HTTP {comment_status}")
            continue
        close_status = gh.close(number)
        if close_status != 200:
            failures.append(f"#{number}: close failed with HTTP {close_status}")
            continue
        print(f"#{number}: closed ({short_sha})")
    return failures


def main() -> int:
    env = os.environ
    repo = env.get("REPO", "")
    body = env.get("PR_BODY", "")
    pr_number = int(env.get("PR_NUMBER", "0") or "0")
    merge_sha = env.get("MERGE_SHA", "")
    run_url = env.get("RUN_URL", "")
    token = env.get("GH_TOKEN", "")

    if not repo or not pr_number:
        print("REPO and PR_NUMBER are required", file=sys.stderr)
        return 2

    numbers = extract_closing_issues(body, repo)
    if not numbers:
        print("no closing keywords in the PR body; nothing to do")
        return 0
    print(f"closing keywords reference: {', '.join(f'#{n}' for n in numbers)}")

    if env.get("DRY_RUN") == "1":
        print(f"DRY_RUN: would resolve {numbers} against {repo}")
        return 0
    if not token:
        print("GH_TOKEN is required unless DRY_RUN=1", file=sys.stderr)
        return 2

    failures = resolve(GitHub(token, repo), pr_number, merge_sha, run_url, numbers)
    for failure in failures:
        print(f"FAIL {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
