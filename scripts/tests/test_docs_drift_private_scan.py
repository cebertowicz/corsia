"""Tests for the private-names/paths scan (check_docs_drift.private_scan_check)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from check_docs_drift import private_scan_check as scan


def _git_add_all(repo_root: Path) -> None:
    """Init a throwaway git repo and stage everything, so `git ls-files` works.

    `git ls-files` reports staged files without needing a commit.
    """
    subprocess.run(["git", "init", "-q"], cwd=repo_root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo_root, check=True)


def _not_a_placeholder_home(rest: str = "") -> str:
    """Build a realistic-looking home directory path at runtime, so it never
    appears as a contiguous literal in this file's own source -- this check
    scans this file too, and the whole point of these fixtures is a name that
    is NOT on the placeholder word list, so it must actually be caught rather
    than exempted as a self-reference.
    """
    return "/Users" + "/" + "quinn" + "morgan" + rest


def test_home_dir_hit_is_found_for_a_real_looking_path():
    """Bounded at the next `/`: only the segment after `/Users/` is the hit."""
    text = f"Backups land in `{_not_a_placeholder_home('/Library/Application Support/x')}`."

    assert scan._home_dir_hits(text) == [_not_a_placeholder_home()]


def test_ellipsis_placeholder_is_not_reported_as_a_leaked_path():
    """`/Users/...` is illustrative prose, not a real username segment."""
    text = "full filesystem paths (e.g., `/Users/...`, `/var/...`, `C:\\...`)"

    assert scan._home_dir_hits(text) == []


def test_ellipsis_placeholder_and_a_real_hit_both_present_only_the_real_one_reports():
    text = f"See `/Users/...` for the pattern, but ours is `{_not_a_placeholder_home('/CORSIA')}`."

    assert scan._home_dir_hits(text) == [_not_a_placeholder_home()]


def test_check_private_names_finds_home_dir_hits_in_docs(tmp_path):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "guide.md").write_text(
        f"Config lives at `{_not_a_placeholder_home('/config.json')}`.", encoding="utf-8"
    )
    _git_add_all(tmp_path)

    result = scan.check_private_names(tmp_path, internal_names_file=None, owner_tokens=[])

    assert result.home_dir_hits == {"docs/guide.md": [_not_a_placeholder_home()]}
    assert not result.ok


def test_check_private_names_finds_configured_owner_tokens(tmp_path):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "guide.md").write_text("Written by Alice Anderson.", encoding="utf-8")
    _git_add_all(tmp_path)

    result = scan.check_private_names(
        tmp_path, internal_names_file=None, owner_tokens=["Alice Anderson"]
    )

    assert result.owner_hits == {"docs/guide.md": ["Alice Anderson"]}


def test_check_private_names_scans_the_whole_tracked_tree_not_just_docs(tmp_path):
    """The leak class this check exists to catch can live outside docs/ too."""
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "helper.py").write_text('OWNER = "Alice Anderson"\n', encoding="utf-8")
    _git_add_all(tmp_path)

    result = scan.check_private_names(
        tmp_path, internal_names_file=None, owner_tokens=["Alice Anderson"]
    )

    assert result.owner_hits == {"scripts/helper.py": ["Alice Anderson"]}


def test_check_private_names_exempts_a_placeholder_segment_anywhere(tmp_path):
    """A placeholder word is exempt by segment, not because its file is a test."""
    tests_dir = tmp_path / "backend" / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_something.py").write_text(
        "FIXTURE_HOME = '/Users/alice/config.json'\n", encoding="utf-8"
    )
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "guide.md").write_text("See `/Users/example/config.json`.", encoding="utf-8")
    _git_add_all(tmp_path)

    result = scan.check_private_names(tmp_path, internal_names_file=None, owner_tokens=[])

    assert result.home_dir_hits == {}


def test_check_private_names_no_longer_exempts_test_files_as_such(tmp_path):
    """The earlier file-path carve-out is gone: a realistic path in a test fixture
    is exactly as reportable as one anywhere else -- most real leaks in this repo
    were hiding in test fixtures, which is why that carve-out was replaced.
    """
    tests_dir = tmp_path / "backend" / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_something.py").write_text(
        f"FIXTURE_HOME = '{_not_a_placeholder_home('/config.json')}'\n", encoding="utf-8"
    )
    _git_add_all(tmp_path)

    result = scan.check_private_names(tmp_path, internal_names_file=None, owner_tokens=[])

    assert result.home_dir_hits == {
        "backend/tests/test_something.py": [_not_a_placeholder_home()]
    }


def test_check_private_names_exempts_a_regex_fragment_quoting_the_pattern(tmp_path):
    """A segment with no letters or digits cannot be a real username: this is how
    a file *other than* home_path.py that quotes HOME_PATH_RE's own shape (e.g. a
    second, independent leak-scanner's pattern source) avoids self-flagging
    without needing its own entry in `_SELF_REFERENCE_FILES`.
    """
    scripts_dir = tmp_path / "backend" / "tests"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "other_scanner.py").write_text(
        'PATTERN = re.compile(r"/Users/[^/\\"\\s]+")\n', encoding="utf-8"
    )
    _git_add_all(tmp_path)

    result = scan.check_private_names(tmp_path, internal_names_file=None, owner_tokens=[])

    assert result.home_dir_hits == {}


def test_check_private_names_still_catches_a_real_identity_leak_in_a_test_file(tmp_path):
    """The exemption is for the home-path sub-check only, not owner identity."""
    tests_dir = tmp_path / "scripts" / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_something.py").write_text(
        'FIXTURE = "Written by Alice Anderson."\n', encoding="utf-8"
    )
    _git_add_all(tmp_path)

    result = scan.check_private_names(
        tmp_path, internal_names_file=None, owner_tokens=["Alice Anderson"]
    )

    assert result.owner_hits == {"scripts/tests/test_something.py": ["Alice Anderson"]}


def test_check_private_names_does_not_self_flag_the_home_path_module(tmp_path):
    """home_path.py's own pattern literally contains `/Users/`; not a leak."""
    pkg_dir = tmp_path / "scripts" / "check_docs_drift"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "home_path.py").write_text(
        'HOME_PATH_RE = re.compile(r"/Users/([^/]+)")\n', encoding="utf-8"
    )
    _git_add_all(tmp_path)

    result = scan.check_private_names(tmp_path, internal_names_file=None, owner_tokens=[])

    assert result.home_dir_hits == {}


def test_check_private_names_skips_binary_extensions(tmp_path):
    (tmp_path / "logo.png").write_bytes(b"Alice Anderson\x00\x01")
    _git_add_all(tmp_path)

    result = scan.check_private_names(
        tmp_path, internal_names_file=None, owner_tokens=["Alice Anderson"]
    )

    assert result.owner_hits == {}


def test_check_private_names_skips_internal_names_without_a_file(tmp_path):
    (tmp_path / "docs").mkdir()
    _git_add_all(tmp_path)

    result = scan.check_private_names(tmp_path, internal_names_file=None, owner_tokens=[])

    assert result.internal_names_skipped_reason is not None
    assert result.internal_name_hits == {}


def test_check_private_names_uses_a_supplied_internal_names_file(tmp_path):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "guide.md").write_text("Ask the Bugs lane about this.", encoding="utf-8")
    _git_add_all(tmp_path)
    names_file = tmp_path / "internal-names.txt"
    names_file.write_text("Bugs\n", encoding="utf-8")

    result = scan.check_private_names(tmp_path, internal_names_file=names_file, owner_tokens=[])

    assert result.internal_name_hits == {"docs/guide.md": ["Bugs"]}
    assert result.internal_names_skipped_reason is None


def test_clean_file_reports_ok(tmp_path):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "guide.md").write_text("Nothing private here.", encoding="utf-8")
    _git_add_all(tmp_path)

    result = scan.check_private_names(tmp_path, internal_names_file=None, owner_tokens=["Alice"])

    assert result.ok


def test_missing_owner_identity_is_a_failure_by_default(tmp_path):
    """An inconclusive owner-identity scan must fail visibly, not pass."""
    (tmp_path / "docs").mkdir()
    _git_add_all(tmp_path)

    result = scan.check_private_names(tmp_path, internal_names_file=None, owner_tokens=[])

    assert result.owner_identity_skipped_reason is not None
    assert not result.ok


def test_missing_owner_identity_can_be_explicitly_allowed(tmp_path):
    """The one legitimate case: an outside contributor with no git identity set."""
    (tmp_path / "docs").mkdir()
    _git_add_all(tmp_path)

    result = scan.check_private_names(
        tmp_path, internal_names_file=None, owner_tokens=[], require_owner_identity=False
    )

    assert result.owner_identity_skipped_reason is not None
    assert result.ok


def test_git_user_name_reads_configured_identity(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.name", "Alice Anderson"], cwd=tmp_path, check=True
    )

    assert scan.git_user_name(tmp_path) == "Alice Anderson"


def test_git_user_name_is_empty_when_unconfigured(tmp_path, monkeypatch):
    """Isolated from any global/system git config, not just the local repo."""
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(fake_home / "nonexistent-gitconfig"))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    assert scan.git_user_name(repo) == ""
