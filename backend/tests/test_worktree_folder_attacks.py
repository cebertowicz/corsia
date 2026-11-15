"""Additional repository-isolation attacks and exact path-pattern checks."""
from __future__ import annotations

import re
import shlex

import pytest

from infra.project_repo_policy import RepoPolicy
from tools import shell_guard
from tests.test_worktree_folder_sandbox import repository_sandbox, repo_name  # noqa: F401
from tests.worktree_sandbox_baseline import ConfinementBreach, compare_per_worktree, unchanged


@pytest.mark.parametrize("name,sibling", [
    ("repo.dot", "repoXdot"), ("repo+plus", "repoXplus"), ("repo[bracket]", "repoXbracketY"),
    ('repo"quoted', "repoXquoted"), ("repo\\backslash", "repoXbackslash"),
])
def test_worktree_pointer_pattern_matches_only_its_repository(tmp_path, monkeypatch, name, sibling):
    repo = tmp_path.resolve() / name
    policy = RepoPolicy("project", "repo", repo, "main", True)
    monkeypatch.setattr(shell_guard, "_verified_worktrees", lambda *_: [])
    monkeypatch.setattr(shell_guard, "list_unsupported_pinned_repos", lambda _: [])
    text = shell_guard.pinned_repo_sandbox_profile("printf probe", str(repo), policies=[policy])
    rule = next(line for line in text.splitlines() if "/[^/]+/[.][Gg][Ii][Tt]" in line)
    # SBPL #"..." is a regex literal, not a JSON/string-escaped pattern.
    pattern = rule.partition('regex #"')[2][:-3]
    assert re.search(pattern, str(repo / ".worktrees/own/.git"))
    assert re.search(pattern, str(repo / ".worktrees/own/.git/config"))
    assert not re.search(pattern, str(repo.parent / sibling / ".worktrees/own/.git"))
    assert not re.search(pattern, str(repo / "deep/.worktrees/own/.git"))
    assert not re.search(pattern, str(repo / ".worktrees/own/nested/.git"))


@pytest.mark.parametrize("failure", ["regression", "existing-gap", "setup"])
def test_baseline_comparison_never_xfails_setup_or_candidate_only_breaches(failure):
    from types import SimpleNamespace

    legacy = object()
    calls = []
    sandbox = SimpleNamespace(make_baseline=lambda: legacy)

    def attack(repository_sandbox):
        calls.append(repository_sandbox)
        if failure == "setup":
            raise AssertionError("setup failed")
        if repository_sandbox is sandbox or failure == "existing-gap":
            raise ConfinementBreach("pointer changed")

    expected = pytest.xfail.Exception if failure == "existing-gap" else AssertionError
    with pytest.raises(expected):
        compare_per_worktree(attack)(repository_sandbox=sandbox)
    assert calls == ([sandbox] if failure == "setup" else [sandbox, legacy])


@pytest.mark.integration
@compare_per_worktree
def test_real_worktree_pointer_cannot_be_changed_through_a_hard_link(repository_sandbox):
    sandbox = repository_sandbox
    pointer = sandbox.own / ".git"
    before = pointer.read_bytes()
    alias = sandbox.own / "pointer-alias"
    result = sandbox.run(f"ln {shlex.quote(str(pointer))} {shlex.quote(str(alias))} && printf forged > {shlex.quote(str(alias))}")
    unchanged(pointer.read_bytes(), before)
    if result.returncode == 0:
        raise ConfinementBreach("hard-link alias permitted a pointer write")
    assert "Operation not permitted" in result.stderr or "Permission denied" in result.stderr, result.stderr


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["file", "directory"])
@compare_per_worktree
def test_real_worktree_pointer_cannot_arrive_from_outside(repository_sandbox, kind):
    sandbox = repository_sandbox
    prepared = sandbox.root / "prepared"
    pointer = prepared / ".git"
    if kind == "file":
        sandbox.allowed(f"mkdir {shlex.quote(str(prepared))} && printf forged > {shlex.quote(str(pointer))}")
    else:
        sandbox.allowed(f"mkdir -p {shlex.quote(str(pointer / 'hooks'))} && printf forged > {shlex.quote(str(pointer / 'config'))}")
    target = sandbox.trees / "incoming"
    sandbox.denied(f"mv {shlex.quote(str(prepared))} {shlex.quote(str(target))}")
    unchanged(target.exists(), False)
    assert prepared.exists()


@pytest.mark.integration
@pytest.mark.parametrize("relative", [".git", ".git/config", ".git/hooks/attack"])
@compare_per_worktree
def test_real_worktree_pointer_directory_and_its_contents_stay_readonly(repository_sandbox, relative):
    sandbox = repository_sandbox
    fresh = sandbox.trees / "fresh"
    target = fresh / relative
    target.parent.mkdir(parents=True)
    command = (f"mkdir {shlex.quote(str(target))}" if relative == ".git" else
               f"printf forged > {shlex.quote(str(target))}")
    sandbox.denied(command)
    unchanged(target.exists(), False)


@pytest.mark.integration
@compare_per_worktree
def test_real_worktree_pointer_cannot_be_replaced_by_swapping_the_worktree(repository_sandbox):
    sandbox = repository_sandbox
    pointer = sandbox.own / ".git"
    before = pointer.read_bytes()
    moved = sandbox.root / "moved-worktree"
    sandbox.denied(
        f"mv {shlex.quote(str(sandbox.own))} {shlex.quote(str(moved))} && "
        f"mkdir {shlex.quote(str(sandbox.own))} && printf forged > {shlex.quote(str(pointer))}"
    )
    unchanged(pointer.read_bytes() if pointer.exists() else None, before)
    unchanged(moved.exists(), False)


@pytest.mark.integration
@pytest.mark.parametrize("phase", ["replace", "create-after-removal"])
@compare_per_worktree
def test_real_worktree_pointer_cannot_become_a_symlink(repository_sandbox, phase):
    sandbox = repository_sandbox
    pointer = sandbox.own / ".git"
    before = pointer.read_bytes()
    elsewhere = sandbox.root / "elsewhere"
    elsewhere.write_text("original")
    if phase == "replace":
        prefix = f"rm {shlex.quote(str(pointer))} && "
    else:
        pointer.unlink()  # Simulate a missing entry before the next command.
        before = None
        prefix = ""
    sandbox.denied(prefix + f"ln -s {shlex.quote(str(elsewhere))} {shlex.quote(str(pointer))}")
    unchanged(pointer.read_bytes() if pointer.exists() else None, before)
    unchanged(pointer.is_symlink(), False)
    unchanged(elsewhere.read_text(), "original")


@pytest.mark.integration
@pytest.mark.parametrize("repo_name,sibling", [
    ("repo.dot", "repoXdot"), ("repo+plus", "repoXplus"), ("repo[bracket]", "repoXbracketY"),
    ('repo"quoted', "repoXquoted"), ("repo\\backslash", "repoXbackslash"),
])
@compare_per_worktree
def test_real_worktree_patterns_are_escaped_and_anchored(repository_sandbox, repo_name, sibling):
    sandbox = repository_sandbox
    # The configured repository works, including a deliberately nested repo.
    sandbox.allowed("printf content > ordinary")
    sandbox.allowed("git init nested")
    marker = sandbox.own / ".git"
    before = marker.read_bytes()
    sandbox.denied(f"printf forged > {shlex.quote(str(marker))}")
    unchanged(marker.read_bytes(), before)
    for relative in ("x.worktrees/file", ".worktrees-old/file", "deep/.worktrees/file"):
        target = sandbox.repo / relative
        target.parent.mkdir(parents=True)
        sandbox.denied(f"printf forged > {shlex.quote(str(target))}")
        unchanged(target.exists(), False)
    # A similarly spelled, unregistered sibling is writable already; this
    # repository's pointer pattern must not deny the sibling's unrelated file.
    unrelated = sandbox.root / sibling / ".worktrees/own/.git"
    unrelated.parent.mkdir(parents=True)
    sandbox.allowed(f"printf unrelated > {shlex.quote(str(unrelated))}")
    assert unrelated.read_text() == "unrelated"
    # An explicit pre-existing read-only boundary in another writable area
    # remains closed even when it contains a deeper .worktrees directory.
    outside = sandbox.root / "other-place"
    target = outside / "deep/.worktrees/own/file"
    target.parent.mkdir(parents=True)
    result = sandbox.run(
        f"printf forged > {shlex.quote(str(target))}",
        before_rules=f'(deny file-write* (subpath "{outside}"))\n',
    )
    if result.returncode == 0:
        raise ConfinementBreach("a deeper unrelated .worktrees directory gained a write allow")
    unchanged(target.exists(), False)
