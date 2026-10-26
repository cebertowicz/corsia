"""Mark issues fixed on ``dev`` and close them once the fix reaches ``main``.

GitHub's own auto-close only fires when a pull request merges into the
repository's default branch. This repo's fixes land on ``dev`` while the
default branch is ``main``, so ``Closes/Fixes/Resolves #N`` in PR bodies never
fire. The ``fixed-in-dev`` workflow runs this module in two modes:

- ``pull_request`` (merged into ``dev``): each issue named by a closing keyword
  gets the ``fixed-in-dev`` label and a comment with the merge SHA, PR number
  and a hidden marker. The issue stays open.
- ``push`` (to ``main``): each open ``fixed-in-dev`` issue is closed once every
  SHA in its markers is an ancestor of the pushed main commit, and the label is
  removed. A person who reopens it therefore keeps it open; a new fix merged to
  ``dev`` labels it again.

Keyword semantics follow GitHub's:

- ``Closes/Fixes/Resolves`` (any tense/case, optional colon) name the issues
  they apply to, including comma / ``and`` / ``&`` lists.
- ``Refs #N`` never labels or closes.
- Cross-repo references (``other/repo#N``) never touch a local issue.
- A reference to a pull-request number is skipped (shared numbering).

Writes are loud: any issue that cannot be resolved exits non-zero so the
workflow run shows red instead of silently missing one.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"

LABEL = "fixed-in-dev"
LABEL_COLOR = "0e8a16"
LABEL_DESCRIPTION = "Fix merged to dev; closes when it reaches main"
BOT_LOGIN = "github-actions[bot]"
_SHA = re.compile(r"[0-9a-f]{7,40}")
_MARKER = re.compile(r"<!--\s*fixed-in-dev sha=([0-9a-f]{7,40}) pr=(\d+)\s*-->")

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


def marker(sha: str, pr_number: int) -> str:
    return f"<!-- fixed-in-dev sha={sha} pr={pr_number} -->"


def marker_shas(comments: list[dict]) -> list[str]:
    """SHAs from markers in comments posted by the workflow's own bot."""
    shas: list[str] = []
    for comment in comments:
        if (comment.get("user") or {}).get("login") != BOT_LOGIN:
            continue
        for found in _MARKER.finditer(comment.get("body") or ""):
            if found.group(1) not in shas:
                shas.append(found.group(1))
    return shas


class GitHub:
    """Minimal GitHub REST client for the calls this module needs."""

    def __init__(self, auth: str, repo: str) -> None:
        self._auth = auth
        self.repo = repo

    def _request(
        self, method: str, path: str, payload: dict | list | None = None
    ) -> tuple[int, dict | list | None]:
        req = urllib.request.Request(
            f"{API_ROOT}/repos/{self.repo}{path}",
            method=method,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {self._auth}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "corsia-fixed-in-dev",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            return exc.code, None

    def _pages(self, path: str) -> tuple[int, list[dict]]:
        items: list[dict] = []
        sep = "&" if "?" in path else "?"
        for page in range(1, 51):
            status, data = self._request("GET", f"{path}{sep}per_page=100&page={page}")
            if status != 200 or not isinstance(data, list):
                return status, items
            items.extend(data)
            if len(data) < 100:
                break
        return 200, items

    def issue(self, number: int) -> tuple[int, dict | None]:
        status, data = self._request("GET", f"/issues/{number}")
        return status, (data if isinstance(data, dict) else None)

    def comments(self, number: int) -> tuple[int, list[dict]]:
        return self._pages(f"/issues/{number}/comments")

    def comment(self, number: int, text: str) -> int:
        status, _ = self._request("POST", f"/issues/{number}/comments", {"body": text})
        return status

    def ensure_label(self) -> int:
        status, _ = self._request(
            "POST",
            "/labels",
            {"name": LABEL, "color": LABEL_COLOR, "description": LABEL_DESCRIPTION},
        )
        # 422 means the label already exists.
        return 201 if status == 422 else status

    def add_label(self, number: int) -> int:
        status, _ = self._request("POST", f"/issues/{number}/labels", {"labels": [LABEL]})
        return status

    def remove_label(self, number: int) -> int:
        status, _ = self._request("DELETE", f"/issues/{number}/labels/{LABEL}")
        # 404 means the label is already gone.
        return 200 if status == 404 else status

    def open_labeled_issues(self) -> tuple[int, list[dict]]:
        return self._pages(f"/issues?state=open&labels={LABEL}")

    def close(self, number: int) -> int:
        status, _ = self._request(
            "PATCH", f"/issues/{number}", {"state": "closed", "state_reason": "completed"}
        )
        return status


def resolve(gh: GitHub, pr_number: int, merge_sha: str, run_url: str, numbers: list[int]) -> list[str]:
    """Label each still-open referenced issue and comment the dev merge.

    Returns a list of human-readable failures (empty on success). Pull-request
    numbers and already-closed issues are skipped without failing. A re-run
    does not comment twice: an existing marker for this SHA is detected.
    """
    if not _SHA.fullmatch(merge_sha or ""):
        # A marker without a valid SHA never parses, so the issue would be
        # labeled but never closed.
        return [f"merge SHA {merge_sha!r} is not a commit SHA; nothing labeled"]
    failures: list[str] = []
    short_sha = merge_sha[:7]
    label_status = gh.ensure_label()
    if label_status not in (200, 201):
        return [f"label create failed with HTTP {label_status}"]
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
        label_status = gh.add_label(number)
        if label_status not in (200, 201):
            failures.append(f"#{number}: label failed with HTTP {label_status}")
            continue
        comments_status, comments = gh.comments(number)
        if comments_status != 200:
            failures.append(f"#{number}: comment lookup failed with HTTP {comments_status}")
            continue
        if merge_sha in marker_shas(comments):
            print(f"#{number}: already marked for {short_sha}; skipping comment")
            continue
        text = (
            f"Fixed in `dev` by PR #{pr_number}, merged as {short_sha} "
            f"([workflow run]({run_url})). This issue closes when that commit "
            f"reaches `main`.\n\n{marker(merge_sha, pr_number)}"
        )
        comment_status = gh.comment(number, text)
        if comment_status not in (200, 201):
            failures.append(f"#{number}: comment failed with HTTP {comment_status}")
            continue
        print(f"#{number}: labeled {LABEL} ({short_sha})")
    return failures


def is_ancestor(sha: str, ref: str, repo_dir: str) -> bool:
    """True when ``sha`` is reachable from ``ref`` in the checkout at repo_dir.

    Raises ``LookupError`` when git cannot answer (exit other than 0 or 1),
    e.g. a marked SHA lost to a rewrite, so it is never read as "not yet".
    """
    proc = subprocess.run(
        ["git", "merge-base", "--is-ancestor", sha, ref],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )
    if proc.returncode not in (0, 1):
        raise LookupError(f"git exit {proc.returncode}: {proc.stderr.strip()}")
    return proc.returncode == 0


def close_reached_main(gh: GitHub, main_sha: str, run_url: str, repo_dir: str = ".") -> list[str]:
    """Close each open fixed-in-dev issue whose marked SHAs are all in main."""
    failures: list[str] = []
    status, issues = gh.open_labeled_issues()
    if status != 200:
        return [f"issue listing failed with HTTP {status}"]
    short_main = main_sha[:7]
    for issue in issues:
        number = issue["number"]
        if "pull_request" in issue:
            continue
        comments_status, comments = gh.comments(number)
        if comments_status != 200:
            failures.append(f"#{number}: comment lookup failed with HTTP {comments_status}")
            continue
        shas = marker_shas(comments)
        if not shas:
            print(f"#{number}: no fixed-in-dev marker; leaving open")
            continue
        try:
            missing = [s for s in shas if not is_ancestor(s, main_sha, repo_dir)]
        except LookupError as exc:
            failures.append(f"#{number}: a marked SHA is not a commit in this checkout ({exc})")
            continue
        if missing:
            print(f"#{number}: not yet in main ({', '.join(s[:7] for s in missing)}); leaving open")
            continue
        close_status = gh.close(number)
        if close_status != 200:
            failures.append(f"#{number}: close failed with HTTP {close_status}")
            continue
        unlabel_status = gh.remove_label(number)
        if unlabel_status != 200:
            failures.append(f"#{number}: closed but label removal failed with HTTP {unlabel_status}")
        text = (
            f"Closed: the fix ({', '.join(s[:7] for s in shas)}) is in `main` as "
            f"{short_main} ([workflow run]({run_url}))."
        )
        comment_status = gh.comment(number, text)
        if comment_status not in (200, 201):
            failures.append(f"#{number}: closed but comment failed with HTTP {comment_status}")
            continue
        print(f"#{number}: closed ({short_main})")
    return failures


def main() -> int:
    env = os.environ
    repo = env.get("REPO", "")
    event = env.get("EVENT_NAME", "pull_request")
    run_url = env.get("RUN_URL", "")
    auth = env.get("GH_TOKEN", "")

    if not repo:
        print("REPO is required", file=sys.stderr)
        return 2

    if event == "push":
        main_sha = env.get("MAIN_SHA", "")
        if not main_sha:
            print("MAIN_SHA is required for push events", file=sys.stderr)
            return 2
        if env.get("DRY_RUN") == "1":
            print(f"DRY_RUN: would close fixed-in-dev issues now in {main_sha[:7]}")
            return 0
        if not auth:
            print("GH_TOKEN is required unless DRY_RUN=1", file=sys.stderr)
            return 2
        failures = close_reached_main(GitHub(auth, repo), main_sha, run_url)
    else:
        body = env.get("PR_BODY", "")
        pr_number = int(env.get("PR_NUMBER", "0") or "0")
        merge_sha = env.get("MERGE_SHA", "")
        if not pr_number:
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
        if not auth:
            print("GH_TOKEN is required unless DRY_RUN=1", file=sys.stderr)
            return 2
        failures = resolve(GitHub(auth, repo), pr_number, merge_sha, run_url, numbers)

    for failure in failures:
        print(f"FAIL {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
