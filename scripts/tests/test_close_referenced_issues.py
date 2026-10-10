"""Tests for scripts/close_referenced_issues.py (issue #2047)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from close_referenced_issues import extract_closing_issues, resolve  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / "close_referenced_issues.py"
REPO = "cebertowicz/corsia"


def test_single_keyword():
    assert extract_closing_issues("Closes #10", REPO) == [10]


def test_colon_and_tenses():
    assert extract_closing_issues("Fixes: #11", REPO) == [11]
    assert extract_closing_issues("closed #12", REPO) == [12]
    assert extract_closing_issues("RESOLVED #13", REPO) == [13]


def test_list_after_one_keyword():
    assert extract_closing_issues("Closes #14, #15 and #16", REPO) == [14, 15, 16]
    assert extract_closing_issues("Fixes #17 & #18", REPO) == [17, 18]


def test_refs_never_closes():
    assert extract_closing_issues("Refs #19", REPO) == []
    assert extract_closing_issues("Ref #20 only context", REPO) == []


def test_bare_reference_without_keyword():
    assert extract_closing_issues("also visible #21 here", REPO) == []


def test_sentence_after_ref_does_not_swallow_next_ref():
    assert extract_closing_issues("Fixed #22 by rewriting #23 handling", REPO) == [22]


def test_period_between_refs():
    assert extract_closing_issues("Fixes #24. Also see #25", REPO) == [24]


def test_gh_style_repo_ref():
    assert extract_closing_issues(f"closed {REPO}#26", REPO) == [26]


def test_issue_url_ref():
    assert extract_closing_issues(f"Fixes https://github.com/{REPO}/issues/27", REPO) == [27]


def test_cross_repo_never_closes():
    assert extract_closing_issues("Fixes other/repo#28", REPO) == []
    # The keyword's first governed token is a cross-repo ref, so the whole list
    # is not a valid local-closing anchor. Under-close over mis-close.
    assert extract_closing_issues("Fixes other/repo#29, #30", REPO) == []


def test_local_ref_after_cross_repo_ref_in_own_clause():
    # Each keyword anchors its own list; a separate local clause still closes.
    assert extract_closing_issues(
        f"Fixes other/repo#31. Closes {REPO}#32", REPO
    ) == [32]


def test_dedupe_first_seen_order():
    assert extract_closing_issues("Closes #31; fixes #32; closes #31", REPO) == [31, 32]


def test_empty_and_none_bodies():
    assert extract_closing_issues("", REPO) == []
    assert extract_closing_issues(None, REPO) == []


def test_template_placeholder_without_number():
    assert extract_closing_issues("Closes #", REPO) == []
    assert extract_closing_issues("Closes #(issue number)", REPO) == []


def test_keyword_word_in_prose_not_followed_by_ref():
    assert extract_closing_issues("this fix (#33) predates the policy", REPO) == []


def test_mixed_realistic_body():
    body = (
        "## Summary\nReworks the guard.\n\n"
        f"Refs {REPO}/pull/40\nCloses #34 and #35.\n"
        "Follow-up context in #36.\n"
    )
    assert extract_closing_issues(body, REPO) == [34, 35]


def test_bare_whitespace_does_not_chain():
    # GitHub grammar: each extra issue needs a comma/and/& or its own keyword.
    assert extract_closing_issues("Closes #37\n#38 is still broken", REPO) == [37]
    assert extract_closing_issues("Closes #39 #40", REPO) == [39]


class FakeGitHub:
    """Scriptable stand-in for the GitHub client."""

    def __init__(self, issues=None, comment_status=201, close_status=200):
        self.repo = REPO
        self.issues = issues or {}
        self.comment_status = comment_status
        self.close_status = close_status
        self.commented: list[int] = []
        self.closed: list[int] = []

    def issue(self, number):
        return 200 if number in self.issues else 404, self.issues.get(number)

    def comment(self, number, text):
        self.commented.append(number)
        return self.comment_status

    def close(self, number):
        self.closed.append(number)
        return self.close_status


def _open_issue():
    return {"state": "open"}


def test_resolve_closes_open_issue_with_comment():
    gh = FakeGitHub(issues={50: _open_issue()})
    failures = resolve(gh, pr_number=9, merge_sha="a" * 40, run_url="u", numbers=[50])
    assert failures == []
    assert gh.commented == [50] and gh.closed == [50]


def test_resolve_skips_pr_itself_and_prs_and_closed():
    gh = FakeGitHub(issues={9: _open_issue(), 51: {"state": "open", "pull_request": {}}, 52: {"state": "closed"}})
    failures = resolve(gh, pr_number=9, merge_sha="a" * 40, run_url="u", numbers=[9, 51, 52])
    assert failures == []
    assert gh.closed == []


def test_resolve_reports_missing_lookup_comment_and_close_failures():
    gh = FakeGitHub(issues={53: _open_issue()}, comment_status=403)
    failures = resolve(gh, 9, "a" * 40, "u", [53])
    assert failures and "53" in failures[0]
    gh2 = FakeGitHub(issues={}, comment_status=201)
    assert resolve(gh2, 9, "a" * 40, "u", [54]) != []
    gh3 = FakeGitHub(issues={55: _open_issue()}, close_status=401)
    assert resolve(gh3, 9, "a" * 40, "u", [55]) != []


def _run_script(**env_overrides):
    env = {**os.environ, "PATH": os.environ["PATH"]}
    env.pop("GH_TOKEN", None)
    env.update({"REPO": REPO, "PR_NUMBER": "9", "MERGE_SHA": "a" * 40, "RUN_URL": "u"})
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True
    )


def test_entrypoint_dry_run_exits_zero_and_names_refs():
    # Regression for the review block: running the file must execute main().
    proc = _run_script(PR_BODY="Closes #60", DRY_RUN="1")
    assert proc.returncode == 0, proc.stderr
    assert "#60" in proc.stdout


def test_entrypoint_no_keywords_is_a_clean_noop():
    proc = _run_script(PR_BODY="Refs #61 only")
    assert proc.returncode == 0
    assert "nothing to do" in proc.stdout


def test_entrypoint_requires_token_for_real_writes():
    proc = _run_script(PR_BODY="Closes #62")
    assert proc.returncode == 2
    assert "GH_TOKEN" in proc.stderr


def test_entrypoint_requires_repo_or_number():
    env_proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        env={**os.environ, "PR_BODY": "Closes #63", "DRY_RUN": "1", "PR_NUMBER": ""},
        capture_output=True,
        text=True,
    )
    assert env_proc.returncode == 2
