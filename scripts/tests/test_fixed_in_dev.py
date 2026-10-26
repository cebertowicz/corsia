"""Tests for scripts/fixed_in_dev.py."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fixed_in_dev import (  # noqa: E402
    close_reached_main,
    extract_closing_issues,
    mark_fixed_in_dev,
    marker,
    marker_shas,
)

SCRIPT = Path(__file__).resolve().parents[1] / "fixed_in_dev.py"
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


BOT = {"login": "github-actions[bot]"}
SHA_A = "a" * 40
SHA_B = "b" * 40


class FakeGitHub:
    """Scriptable stand-in for the GitHub client."""

    def __init__(
        self, issues=None, comments=None, comment_status=201, label_status=201, add_label_status=201
    ):
        self.repo = REPO
        self.issues = issues or {}
        self.comment_log = comments or {}
        self.comment_status = comment_status
        self.label_status = label_status
        self.add_label_status = add_label_status
        self.writes: list[str] = []
        self.label_created = 0
        self.labeled: list[int] = []
        self.unlabeled: list[int] = []
        self.commented: list[tuple[int, str]] = []
        self.closed: list[int] = []

    def issue(self, number):
        return 200 if number in self.issues else 404, self.issues.get(number)

    def comments(self, number):
        return 200, self.comment_log.get(number, [])

    def comment(self, number, text):
        self.commented.append((number, text))
        if self.comment_status in (200, 201):
            self.writes.append("comment")
            self.comment_log.setdefault(number, []).append({"user": BOT, "body": text})
        return self.comment_status

    def ensure_label(self):
        self.label_created += 1
        return self.label_status

    def add_label(self, number):
        self.labeled.append(number)
        if self.add_label_status in (200, 201):
            self.writes.append("label")
            self.issues[number]["labeled"] = True
        return self.add_label_status

    def remove_label(self, number):
        self.unlabeled.append(number)
        self.issues[number]["labeled"] = False
        return 200

    def open_labeled_issues(self):
        return 200, [
            {"number": n, **i}
            for n, i in self.issues.items()
            if i.get("state") == "open" and i.get("labeled")
        ]

    def close(self, number):
        self.closed.append(number)
        self.issues[number]["state"] = "closed"
        return 200


def _open_issue(labeled=False):
    return {"state": "open", "labeled": labeled}


def _labeled_issue():
    return _open_issue(labeled=True)


def test_dev_path_labels_and_comments_with_marker_but_never_closes():
    gh = FakeGitHub(issues={50: _open_issue()})
    failures = mark_fixed_in_dev(gh, pr_number=9, merge_sha=SHA_A, run_url="u", numbers=[50])
    assert failures == []
    assert gh.labeled == [50] and gh.closed == []
    assert gh.label_created == 1
    (number, text), = gh.commented
    assert number == 50
    assert "PR #9" in text and SHA_A[:7] in text
    assert f"<!-- fixed-in-dev sha={SHA_A} pr=9 -->" in text
    # The marker is recorded before the label makes the issue visible to main.
    assert gh.writes == ["comment", "label"]


def test_dev_path_rerun_does_not_double_comment():
    gh = FakeGitHub(issues={50: _open_issue()})
    mark_fixed_in_dev(gh, 9, SHA_A, "u", [50])
    mark_fixed_in_dev(gh, 9, SHA_A, "u", [50])
    assert len(gh.commented) == 1
    assert gh.closed == []


def test_dev_path_rerun_after_label_failure_labels_without_recommenting():
    gh = FakeGitHub(issues={57: _open_issue()}, add_label_status=500)
    assert mark_fixed_in_dev(gh, 9, SHA_A, "u", [57]) != []
    gh.add_label_status = 201
    assert mark_fixed_in_dev(gh, 9, SHA_A, "u", [57]) == []
    assert len(gh.commented) == 1 and gh.issues[57]["labeled"]


def test_dev_path_comment_failure_leaves_reopened_issue_unlabeled(tmp_path):
    # A reopened issue still carries a marker for a SHA already in main. If the
    # new marker fails to post, the issue must not be labeled, or the next main
    # push would close it on the old fix.
    main_sha, in_main, _ = _promotion_repo(tmp_path)
    gh = FakeGitHub(issues={58: _open_issue()}, comments={58: _marked(in_main)}, comment_status=500)
    assert mark_fixed_in_dev(gh, 9, SHA_B, "u", [58]) != []
    assert not gh.issues[58]["labeled"]
    assert close_reached_main(gh, main_sha, "u", repo_dir=str(tmp_path)) == []
    assert gh.closed == []


def test_dev_path_skips_pr_itself_prs_and_closed():
    gh = FakeGitHub(issues={9: _open_issue(), 51: {"state": "open", "pull_request": {}}, 52: {"state": "closed"}})
    failures = mark_fixed_in_dev(gh, pr_number=9, merge_sha=SHA_A, run_url="u", numbers=[9, 51, 52])
    assert failures == []
    assert gh.labeled == [] and gh.commented == [] and gh.closed == []


def test_dev_path_empty_merge_sha_fails_before_writing():
    gh = FakeGitHub(issues={56: _open_issue()})
    failures = mark_fixed_in_dev(gh, 9, "", "u", [56])
    assert failures and "not a commit SHA" in failures[0]
    assert gh.labeled == [] and gh.commented == [] and gh.label_created == 0


def test_dev_path_reports_missing_issue_label_and_comment_failures():
    assert mark_fixed_in_dev(FakeGitHub(issues={}), 9, SHA_A, "u", [54]) != []
    assert mark_fixed_in_dev(FakeGitHub(issues={53: _open_issue()}, comment_status=403), 9, SHA_A, "u", [53]) != []
    assert mark_fixed_in_dev(FakeGitHub(issues={55: _open_issue()}, label_status=403), 9, SHA_A, "u", [55]) != []
    assert mark_fixed_in_dev(FakeGitHub(issues={59: _open_issue()}, add_label_status=403), 9, SHA_A, "u", [59]) != []


def test_marker_shas_ignore_non_bot_comments():
    spoof = {"user": {"login": "someone"}, "body": f"<!-- fixed-in-dev sha={SHA_A} pr=1 -->"}
    real = {"user": BOT, "body": f"x <!-- fixed-in-dev sha={SHA_B} pr=2 -->"}
    assert marker_shas([spoof, real]) == [SHA_B]


def _git(repo, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repo, name):
    (repo / name).write_text(name)
    _git(repo, "add", name)
    _git(repo, "commit", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


def _promotion_repo(tmp_path):
    """main has a dev-merged SHA via a merge commit; another dev SHA is not in main."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _commit(tmp_path, "base")
    _git(tmp_path, "checkout", "-q", "-b", "dev")
    in_main = _commit(tmp_path, "fix-in-main")
    _git(tmp_path, "checkout", "-q", "main")
    _git(tmp_path, "merge", "-q", "--no-ff", "dev", "-m", "promote")
    main_sha = _git(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "checkout", "-q", "dev")
    not_in_main = _commit(tmp_path, "fix-not-in-main")
    return main_sha, in_main, not_in_main


def _marked(*shas):
    return [{"user": BOT, "body": marker(sha, 1)} for sha in shas]


def test_main_path_closes_ancestor_and_keeps_non_ancestor_open(tmp_path):
    main_sha, in_main, not_in_main = _promotion_repo(tmp_path)
    gh = FakeGitHub(
        issues={1: _labeled_issue(), 2: _labeled_issue()},
        comments={1: _marked(in_main), 2: _marked(not_in_main)},
    )
    failures = close_reached_main(gh, main_sha, "u", repo_dir=str(tmp_path))
    assert failures == []
    assert gh.closed == [1]
    assert gh.unlabeled == [1]
    assert main_sha[:7] in gh.commented[0][1]


def test_main_path_reopened_issue_stays_open_on_next_push(tmp_path):
    main_sha, in_main, _ = _promotion_repo(tmp_path)
    gh = FakeGitHub(issues={6: _labeled_issue()}, comments={6: _marked(in_main)})
    assert close_reached_main(gh, main_sha, "u", repo_dir=str(tmp_path)) == []
    assert gh.closed == [6]
    gh.issues[6]["state"] = "open"  # a person reopens it: the fix was incomplete
    assert close_reached_main(gh, main_sha, "u", repo_dir=str(tmp_path)) == []
    assert gh.closed == [6]
    assert gh.issues[6]["state"] == "open"


def test_main_path_needs_every_marked_sha_in_main(tmp_path):
    main_sha, in_main, not_in_main = _promotion_repo(tmp_path)
    gh = FakeGitHub(
        issues={3: _labeled_issue()},
        comments={3: _marked(in_main, not_in_main)},
    )
    assert close_reached_main(gh, main_sha, "u", repo_dir=str(tmp_path)) == []
    assert gh.closed == []


def test_main_path_unknown_sha_fails_loudly_and_stays_open(tmp_path):
    main_sha, in_main, _ = _promotion_repo(tmp_path)
    gh = FakeGitHub(issues={4: _labeled_issue()}, comments={4: _marked("c" * 40)})
    failures = close_reached_main(gh, main_sha, "u", repo_dir=str(tmp_path))
    assert len(failures) == 1 and failures[0].startswith("#4:")
    assert gh.closed == []


def test_main_path_label_without_marker_fails_loudly(tmp_path):
    main_sha, _, _ = _promotion_repo(tmp_path)
    gh = FakeGitHub(issues={5: _labeled_issue()})
    failures = close_reached_main(gh, main_sha, "u", repo_dir=str(tmp_path))
    assert len(failures) == 1 and failures[0].startswith("#5:") and "no marker" in failures[0]
    assert gh.closed == []


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


def test_entrypoint_push_dry_run_and_requirements():
    proc = _run_script(EVENT_NAME="push", MAIN_SHA="d" * 40, DRY_RUN="1")
    assert proc.returncode == 0, proc.stderr
    assert _run_script(EVENT_NAME="push").returncode == 2
