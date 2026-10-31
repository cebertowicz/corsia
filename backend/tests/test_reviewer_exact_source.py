"""Exact-revision source access for the tool-enabled reviewer."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _two_commit_repo(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "reviewer@example.test")
    _git(repo, "config", "user.name", "Reviewer Test")
    (repo / "example.py").write_text("VALUE = 1\n")
    _git(repo, "add", "example.py")
    _git(repo, "commit", "-m", "base")
    base_sha = _git(repo, "rev-parse", "HEAD")
    (repo / "example.py").write_text("VALUE = 2\n")
    _git(repo, "commit", "-am", "head")
    return repo, base_sha, _git(repo, "rev-parse", "HEAD")


def test_exact_source_uses_object_store_from_a_neutral_directory(tmp_path):
    from reviewer.reviewer_exact_source import exact_review_source

    repo, base_sha, head_sha = _two_commit_repo(tmp_path)

    with exact_review_source(str(repo), base_sha, head_sha) as (
        source, cwd, git_dir,
    ):
        assert Path(cwd).parent != repo
        assert not (Path(cwd) / "example.py").exists()
        assert Path(git_dir) == repo / ".git"
        assert f"reviewer_read.py {git_dir} {base_sha} {head_sha} diff" in source
        assert "show <path>" in source
        assert "grep <fixed-string>" in source


def test_exact_diff_is_grounded_in_the_requested_commits(tmp_path):
    from reviewer.reviewer_exact_source import exact_review_diff

    repo, base_sha, head_sha = _two_commit_repo(tmp_path)
    (repo / "example.py").write_text("VALUE = 999\n")

    diff = exact_review_diff(str(repo), base_sha, head_sha)

    assert "+VALUE = 2" in diff
    assert "VALUE = 999" not in diff


def test_codex_source_profile_allows_only_minimal_runtime_and_exact_source(tmp_path):
    from reviewer.reviewer_exact_source import codex_exact_source_controls

    review_dir = tmp_path / "review"
    git_dir = tmp_path / "repo.git"
    args = codex_exact_source_controls(str(review_dir), str(git_dir))
    rendered = "\n".join(args)

    assert 'default_permissions="corsia_reviewer"' in args
    assert 'approval_policy="never"' in args
    assert "permissions.corsia_reviewer.filesystem=" in rendered
    assert '":minimal"="read"' in rendered
    assert f'"{review_dir}"="read"' in rendered
    assert f'"{git_dir}"="read"' in rendered
    assert 'permissions.corsia_reviewer.network.enabled=false' in args
    assert '":root"="read"' not in rendered


def test_claude_source_allowlist_is_pinned_to_exact_revisions(tmp_path):
    from reviewer.reviewer_exact_source import claude_exact_source_controls, reader_command

    base_sha = "b" * 40
    head_sha = "a" * 40
    git_dir = str(tmp_path / "repo.git")
    args, _ = claude_exact_source_controls(git_dir, base_sha, head_sha)
    reader = reader_command(git_dir, base_sha, head_sha)

    assert args == [
        "--restricted", "--safe-mode", "--setting-sources", "",
        "--strict-mcp-config", "--permission-mode", "dontAsk", "--tools", "Bash",
        "--allowedTools",
        f"Bash({reader} stat)",
        f"Bash({reader} names)",
        f"Bash({reader} diff)",
        f"Bash({reader} diff *)",
        f"Bash({reader} show *)",
        f"Bash({reader} grep *)",
    ]
    # No rule lets the model run Git itself, so it can never add a Git option.
    assert not any(rule.startswith("Bash(git") for rule in args)
    assert "--add-dir" not in args


def _read(git_dir, base_sha, head_sha, *args):
    from reviewer import reviewer_read

    return subprocess.run(
        [sys.executable, "-I", reviewer_read.__file__, str(git_dir), base_sha, head_sha, *args],
        capture_output=True,
        text=True,
    )


def test_reader_serves_the_exact_change(tmp_path):
    repo, base_sha, head_sha = _two_commit_repo(tmp_path)
    git_dir = repo / ".git"

    assert "example.py" in _read(git_dir, base_sha, head_sha, "names").stdout
    assert "+VALUE = 2" in _read(git_dir, base_sha, head_sha, "diff").stdout
    assert "+VALUE = 2" in _read(git_dir, base_sha, head_sha, "diff", "example.py").stdout
    assert _read(git_dir, base_sha, head_sha, "show", "example.py").stdout == "VALUE = 2\n"
    grep = _read(git_dir, base_sha, head_sha, "grep", "-VALUE", "example.py")
    assert grep.returncode == 1  # a leading dash is searched for, not parsed
    assert "VALUE = 2" in _read(git_dir, base_sha, head_sha, "grep", "VALUE").stdout


@pytest.mark.parametrize("args", [
    ("diff", "--output=OUT"),
    ("diff", "example.py", "--output=OUT"),
    ("show", "--output=OUT"),
    ("show", "example.py", "--output=OUT"),
    ("grep", "VALUE", "--open-files-in-pager=touch OUT"),
    ("grep", "VALUE", "-OOUT"),
    ("stat", "--output=OUT"),
    ("names", "extra"),
    ("log",),
])
def test_reader_refuses_anything_that_could_become_an_option(tmp_path, args):
    repo, base_sha, head_sha = _two_commit_repo(tmp_path)
    out = tmp_path / "written"
    args = tuple(arg.replace("OUT", str(out)) for arg in args)

    result = _read(repo / ".git", base_sha, head_sha, *args)

    assert result.returncode == 2
    assert "reviewer_read:" in result.stderr
    assert not out.exists()


def _can_apply_sandbox() -> bool:
    if sys.platform != "darwin":
        return False
    probe = ["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)", "/usr/bin/true"]
    return subprocess.run(probe, capture_output=True).returncode == 0


@pytest.mark.skipif(
    not _can_apply_sandbox(),
    reason="needs macOS sandbox-exec outside another sandbox (the backend and CI runners are not sandboxed)",
)
def test_write_confinement_allows_only_the_scratch_directory(tmp_path):
    from reviewer.reviewer_exact_source import write_confined

    inside, outside = tmp_path / "review", tmp_path / "outside"
    inside.mkdir()
    script = f"echo ok > {inside}/a; echo no > {outside}"

    subprocess.run(write_confined(["/bin/sh", "-c", script], [str(inside)]))

    assert (inside / "a").read_text() == "ok\n"
    assert not outside.exists()


def test_write_confinement_fails_closed_without_sandbox(monkeypatch):
    from reviewer import reviewer_exact_source

    monkeypatch.setattr(reviewer_exact_source.sys, "platform", "linux")

    with pytest.raises(reviewer_exact_source.ExactSourceError):
        reviewer_exact_source.write_confined(["claude"], ["/tmp"])


@pytest.mark.parametrize("bad_sha", ["", "abc123", "G" * 40, "a" * 41])
def test_exact_source_rejects_noncanonical_commit_ids(tmp_path, bad_sha):
    from reviewer.reviewer_exact_source import ExactSourceError, exact_review_source

    repo, _, head_sha = _two_commit_repo(tmp_path)

    with pytest.raises(ExactSourceError, match="40-character lowercase SHA"):
        with exact_review_source(str(repo), bad_sha, head_sha):
            pass


def test_exact_source_fails_loudly_when_commit_cannot_be_grounded(tmp_path):
    from reviewer.reviewer_exact_source import ExactSourceError, exact_review_source

    repo, base_sha, _ = _two_commit_repo(tmp_path)

    with pytest.raises(ExactSourceError, match="could not ground reviewed commit"):
        with exact_review_source(str(repo), base_sha, "f" * 40):
            pass


def test_fetch_diagnostics_do_not_reach_reviewer_status(monkeypatch, tmp_path):
    from reviewer.on_pr_review import _error_status_text
    from reviewer import reviewer_exact_source

    left = "reviewer"
    right = "fixture-" + "secret"
    remote = (
        "https" + chr(58) + "/" * 2 + left + chr(58) + right
        + "@example.test/repository.git"
    )
    diagnostic = "fatal: unable to access " + remote
    failed_commit_check = subprocess.CompletedProcess([], 1, "", "")
    failed_fetch = subprocess.CompletedProcess([], 128, "", diagnostic)

    monkeypatch.setattr(
        reviewer_exact_source,
        "_run_git",
        lambda *_args, **_kwargs: (
            failed_fetch if _args[0][0] == "fetch" else failed_commit_check
        ),
    )

    with pytest.raises(reviewer_exact_source.ExactSourceError) as raised:
        reviewer_exact_source._ensure_commit(
            str(tmp_path / "repo.git"), "a" * 40, cwd=str(tmp_path),
        )

    assert diagnostic not in str(raised.value)
    assert diagnostic not in _error_status_text(raised.value)


def test_diff_diagnostics_do_not_reach_reviewer_status(monkeypatch, tmp_path):
    from reviewer.on_pr_review import _error_status_text
    from reviewer import reviewer_exact_source

    left = "reviewer"
    right = "fixture-" + "secret"
    remote = (
        "https" + chr(58) + "/" * 2 + left + chr(58) + right
        + "@example.test/repository.git"
    )
    diagnostic = "fatal: unable to access " + remote
    failed_diff = subprocess.CompletedProcess([], 1, "", diagnostic)

    monkeypatch.setattr(
        reviewer_exact_source, "_prepare_source", lambda *_args: ("", str(tmp_path)),
    )
    monkeypatch.setattr(
        reviewer_exact_source, "_run_git", lambda *_args, **_kwargs: failed_diff,
    )

    with pytest.raises(reviewer_exact_source.ExactSourceError) as raised:
        reviewer_exact_source.exact_review_diff(
            str(tmp_path), "b" * 40, "a" * 40,
        )

    assert diagnostic not in str(raised.value)
    assert diagnostic not in _error_status_text(raised.value)


_RUN_DIR = "/tmp/review/corsia-cc-ab_c1234"
_RUN_FOLDER = "[^/]*-corsia[^A-Za-z0-9]cc[^A-Za-z0-9]ab[^A-Za-z0-9]c1234"
_BASE_RULES = (
    '(version 1)(allow default)(deny file-write*)'
    '(allow file-write* (subpath "{work}") (literal "/dev/null") (literal "/dev/tty")'
    ' (regex #"^/dev/fd/") (regex #"^/dev/ttys"))'
)


def _temp_rule() -> str:
    import os
    from pathlib import Path

    temp_root = f"{Path('/tmp').resolve()}/claude-{os.getuid()}"
    escaped = temp_root.replace(".", "\\.")
    return (
        f'(allow file-write* (literal "{temp_root}")'
        f' (regex #"^{escaped}/{_RUN_FOLDER}(/.*)?$"))'
    )


def _cwd_tracking_file_rule() -> str:
    from pathlib import Path

    temp_parent = str(Path("/tmp").resolve()).replace(".", "\\.")
    return f'(allow file-write* (regex #"^{temp_parent}/claude-[^/]*-cwd$"))'


def test_write_scope_with_custom_config_root_allows_only_the_run_state_folder():
    from reviewer.reviewer_exact_source import (
        claude_config_state_scope, render_write_profile,
    )

    scope = claude_config_state_scope({"CLAUDE_CONFIG_DIR": "/x/acct"}, _RUN_DIR)

    assert render_write_profile(["/x/work"], scope) == (
        _BASE_RULES.format(work="/x/work")
        + f'(allow file-write* (regex #"^/x/acct/projects/{_RUN_FOLDER}(/.*)?$"))'
        + _temp_rule()
        + _cwd_tracking_file_rule()
    )
    assert scope.global_file.as_posix() == "/x/acct/.claude.json"


def test_write_scope_without_config_root_refuses_ambient_home():
    from reviewer.reviewer_exact_source import ExactSourceError, claude_config_state_scope

    with pytest.raises(ExactSourceError):
        claude_config_state_scope({"HOME": "/srv/home"}, _RUN_DIR)


def test_write_profile_contains_no_counted_regex_interval():
    import re

    from reviewer.reviewer_exact_source import claude_config_state_scope, render_write_profile

    profile = render_write_profile(
        ["/x/work"], claude_config_state_scope({"CLAUDE_CONFIG_DIR": "/x/acct"}, _RUN_DIR),
    )

    assert not re.search(r"\{\d", profile)


def test_write_scope_opens_the_global_config_file_only_on_request():
    import dataclasses

    from reviewer.reviewer_exact_source import (
        claude_config_state_scope, render_write_profile,
    )

    custom = claude_config_state_scope({"CLAUDE_CONFIG_DIR": "/x/acct"}, _RUN_DIR)

    for scope, file_rule in (
        (custom, r'^/x/acct/\.claude\.json(\.[^/]*)?$'),
    ):
        assert file_rule not in render_write_profile(["/x/work"], scope)
        opened = dataclasses.replace(scope, allow_global_file=True)
        assert render_write_profile(["/x/work"], opened).endswith(
            f'(allow file-write* (regex #"{file_rule}"))'
        )


def test_write_scope_escapes_paths_for_profile_literals():
    from reviewer.reviewer_exact_source import (
        ExactSourceError, claude_config_state_scope, render_write_profile,
    )

    scope = claude_config_state_scope(
        {"CLAUDE_CONFIG_DIR": "/x/a c.d/[1]+(2)"}, _RUN_DIR,
    )
    profile = render_write_profile(['/x/work "q" \\b'], scope)

    assert '(subpath "/x/work \\"q\\" \\\\b")' in profile
    assert r'#"^/x/a c\.d/\[1\]\+\(2\)/projects/' in profile
    with pytest.raises(ExactSourceError):
        render_write_profile(["/x/work\nline"], scope)
    quoted = claude_config_state_scope({"CLAUDE_CONFIG_DIR": '/x/"a"'}, _RUN_DIR)
    with pytest.raises(ExactSourceError):
        render_write_profile(["/x/work"], quoted)


@pytest.mark.parametrize("run_dir", ["/tmp/short", "/tmp/review/corsia-cc-éééééééééé"])
def test_write_scope_requires_a_distinctive_run_directory_name(run_dir):
    from reviewer.reviewer_exact_source import ExactSourceError, claude_config_state_scope

    with pytest.raises(ExactSourceError):
        claude_config_state_scope({"CLAUDE_CONFIG_DIR": "/x/acct"}, run_dir)


def test_write_scope_fails_closed_without_sandbox_for_config_state(monkeypatch):
    from reviewer import reviewer_exact_source

    monkeypatch.setattr(reviewer_exact_source.sys, "platform", "linux")
    scope = reviewer_exact_source.claude_config_state_scope(
        {"CLAUDE_CONFIG_DIR": "/x/acct"}, _RUN_DIR,
    )

    with pytest.raises(reviewer_exact_source.ExactSourceError):
        reviewer_exact_source.write_confined(["claude"], ["/tmp"], scope)


def test_discard_config_state_removes_only_this_runs_folder(tmp_path):
    import dataclasses

    from reviewer.reviewer_exact_source import claude_config_state_scope, discard_config_state

    scope = dataclasses.replace(
        claude_config_state_scope(
            {"CLAUDE_CONFIG_DIR": str(tmp_path)}, "/tmp/review/corsia-cc-ab_c1234",
        ),
        temp_root=tmp_path / "cli-tmp",
    )
    mine = tmp_path / "projects" / "-private-tmp-review-corsia-cc-ab-c1234"
    other = tmp_path / "projects" / "-private-tmp-review-corsia-cc-zz-c9999"
    my_temp = tmp_path / "cli-tmp" / "-private-tmp-review-corsia-cc-ab-c1234"
    other_temp = tmp_path / "cli-tmp" / "-private-tmp-review-corsia-cc-zz-c9999"
    for folder in (mine, other, my_temp, other_temp):
        (folder / "s" / "tool-results").mkdir(parents=True)
    (tmp_path / "settings.json").write_text("{}")

    discard_config_state(scope)

    assert not mine.exists() and not my_temp.exists()
    assert other.is_dir() and other_temp.is_dir()
    assert (tmp_path / "settings.json").is_file()


@pytest.mark.skipif(
    not _can_apply_sandbox(),
    reason="needs macOS sandbox-exec outside another sandbox (the backend and CI runners are not sandboxed)",
)
def test_write_scope_confines_config_state_to_the_run_folder(tmp_path):
    import dataclasses

    from reviewer.reviewer_exact_source import claude_config_state_scope, write_confined

    config, work = tmp_path / "acct", tmp_path / "work"
    temp_root = (tmp_path / "cli-tmp").resolve()
    run_dir = work / "corsia-cc-ab_c1234"
    run_folder = config / "projects" / "-tmp-work-corsia-cc-ab-c1234"
    sibling = config / "projects" / "-tmp-work-corsia-cc-zz-c9999"
    temp_folder = temp_root / "-tmp-work-corsia-cc-ab-c1234"
    temp_sibling = temp_root / "-tmp-work-corsia-cc-zz-c9999"
    cwd_file = temp_root.parent / "claude-abc123-cwd"
    cwd_other = temp_root.parent / "claude-abc123-other"
    other_cwd = temp_root.parent / "other-cwd"
    nested_cwd = temp_root / "nested" / "claude-abc123-cwd"
    plain_file = temp_root.parent / "qg-plain-file"
    for folder in (run_dir, run_folder, sibling, config / "hooks", temp_sibling, nested_cwd.parent):
        folder.mkdir(parents=True)
    scope = dataclasses.replace(
        claude_config_state_scope({"CLAUDE_CONFIG_DIR": str(config)}, str(run_dir)),
        temp_root=temp_root,
    )
    script = f"mkdir -p {temp_folder} ; " + " ; ".join(
        f"echo x > {target}" for target in (
            run_folder / "ok", temp_folder / "ok", cwd_file, sibling / "no", temp_sibling / "no",
            temp_root / "no", cwd_other, other_cwd, nested_cwd, plain_file,
            config / "hooks" / "no", config / "settings.json", config / ".claude.json",
        )
    )

    subprocess.run(write_confined(["/bin/sh", "-c", script], [str(work)], scope))

    assert (run_folder / "ok").is_file()
    assert (temp_folder / "ok").is_file()
    assert cwd_file.is_file()
    assert not any(
        path.exists() for path in (
            sibling / "no", temp_sibling / "no", temp_root / "no", cwd_other, other_cwd,
            nested_cwd, plain_file, config / "hooks" / "no", config / "settings.json",
            config / ".claude.json",
        )
    )
