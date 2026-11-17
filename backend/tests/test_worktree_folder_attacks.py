"""Additional repository-isolation attacks and exact path-pattern checks."""
from __future__ import annotations

import re
import shlex

import pytest

from infra.project_repo_policy import RepoPolicy
from tools import shell_guard
from tests.test_worktree_folder_sandbox import (  # noqa: F401
    ConfinementBreach, protect_base, repository_sandbox, repo_name, unchanged,
)


@pytest.mark.parametrize("name,sibling", [
    ("repo.dot", "repoXdot"), ("repo+plus", "repoXplus"), ("repo[bracket]", "repoXbracketY"),
    ('repo"quoted', "repoXquoted"), ("repo\\backslash", "repoXbackslash"),
])
def test_worktree_pointer_pattern_matches_only_its_repository(tmp_path, monkeypatch, name, sibling):
    repo = tmp_path.resolve() / name
    policy = RepoPolicy("project", "repo", repo, "main", True)
    monkeypatch.setattr(shell_guard, "_has_verified_worktree", lambda *_: False)
    monkeypatch.setattr(shell_guard, "list_unsupported_pinned_repos", lambda _: [])
    text = shell_guard.pinned_repo_sandbox_profile("printf probe", str(repo), policies=[policy])
    rule = next(line for line in text.splitlines() if "/[^/]+/[.][Gg][Ii][Tt]" in line)
    # Python model of (require-all (subpath S) (regex R)); L3 proves Seatbelt.
    subtree = rule.partition('(subpath "')[2].partition('") (regex')[0]
    subtree = subtree.replace('\\"', '"').replace("\\\\", "\\")
    pattern = rule.partition('regex #"')[2][:-4]
    assert subtree == str(repo / ".worktrees")
    assert '"' not in pattern and "\\" not in pattern  # wildcards, never escapes

    def denied(path):
        text = str(path)
        return (text == subtree or text.startswith(subtree + "/")) and re.search(pattern, text)

    assert denied(repo / ".worktrees/own/.git")
    assert denied(repo / ".worktrees/own/.GIT")
    assert denied(repo / ".worktrees/own/.git/config")
    assert not denied(repo / ".worktrees/own/nested/.git")
    assert not denied(repo.parent / sibling / ".worktrees/own/.git")
    assert not denied(repo / "deep/.worktrees/own/.git")
    assert not denied(repo / ".worktrees/own/.gitignore")


@pytest.mark.integration
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


@pytest.mark.parametrize("kind", ["protected", "pinned-only", "error", "unbound"])
def test_folder_rules_only_for_enforceable_repositories(tmp_path, monkeypatch, kind):
    from tools.sandbox_git import worktree_folder_rules

    repo = tmp_path.resolve() / "repo"
    policy = RepoPolicy(
        "project", "repo", repo, "main", kind != "pinned-only",
        enforcement_error="broken" if kind == "error" else None,
        source_binding_valid=kind != "unbound",
    )
    monkeypatch.setattr(shell_guard, "_has_verified_worktree", lambda *_: False)
    monkeypatch.setattr(shell_guard, "list_unsupported_pinned_repos", lambda _: [])
    text = shell_guard.pinned_repo_sandbox_profile("printf probe", str(repo), policies=[policy])
    folder_allow = f'(allow file-read* file-write* (subpath "{repo / ".worktrees"}"))'
    if kind == "protected":
        assert folder_allow in text
        assert all(rule in text for rule in worktree_folder_rules(repo))
    elif kind == "pinned-only":
        # Unprotected: per-worktree pointer denies only, as before; none here.
        assert ".worktrees" not in text
    else:
        # Not enforceable: the whole-repository deny stays, with no folder rules.
        assert f'(deny file-write* (subpath "{repo}"))' in text
        assert ".worktrees" not in text


def test_immediate_entries_reopen_only_their_own_metadata(tmp_path):
    from tools.sandbox_git import worktree_folder_rules

    container, entry_deny, entry_allow, pointer_deny = worktree_folder_rules(tmp_path / "repo")
    assert entry_deny.startswith("(deny file-write* ") and entry_deny.endswith('[^/]+$")))')
    operations = entry_allow.removeprefix("(allow ").partition(" (require-all")[0].split()
    assert set(operations) == {
        "file-write-times", "file-write-mode", "file-write-xattr",
        "file-write-flags", "file-write-owner", "file-write-acl",
    }
    # Same entries as the deny it follows; the pointer deny comes last and wins.
    assert entry_allow.partition(" (require-all")[2] == entry_deny.partition(" (require-all")[2]
    assert pointer_deny.startswith("(deny file-write* ") and "[.][Gg][Ii][Tt]" in pointer_deny


def _linked_fixture(tmp_path):
    """A repo with one linked worktree and one leftover folder, built by Git."""
    import subprocess

    repo = tmp_path.resolve() / "repo"
    repo.mkdir()
    env = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "PATH": "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin"}

    def git(*args):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                       cwd=repo, env=env, capture_output=True, text=True, timeout=20, check=True)

    git("init", "-b", "main")
    (repo / "base.txt").write_text("base\n")
    git("add", "base.txt")
    git("commit", "-m", "Initial fixture")
    git("worktree", "add", "-b", "own", str(repo / ".worktrees" / "own"))
    (repo / ".worktrees" / "leftover").mkdir()
    return repo


def test_unprotected_pinned_repo_keeps_per_worktree_pointer_denies(tmp_path, monkeypatch):
    from tools.sandbox_git import worktree_folder_rules

    repo = _linked_fixture(tmp_path)
    policy = RepoPolicy("project", "repo", repo, "main", False)
    monkeypatch.setattr(shell_guard, "list_unsupported_pinned_repos", lambda _: [])
    text = shell_guard.pinned_repo_sandbox_profile(
        "printf probe", str(repo), policies=[policy], grant_policies=[policy],
    )
    own = repo / ".worktrees" / "own"
    assert f'(deny file-write* (literal "{own / ".git"}"))' in text
    assert str(repo / ".worktrees" / "leftover") not in text
    assert f'(allow file-read* file-write* (subpath "{repo / ".worktrees"}"))' not in text
    assert not any(rule in text for rule in worktree_folder_rules(repo))


@pytest.mark.integration
@pytest.mark.parametrize("protect_base", [False])
@pytest.mark.parametrize("probe,expected", [
    # Only the proved worktrees' own pointers are denied; everything else in
    # .worktrees stays as writable as the rest of the repository.
    ("worktree-add-from-worktree", {"succeeded": True, "new_pointer": True}),
    ("remove-stale-folder-with-git", {"succeeded": True, "stale_present": False}),
    ("new-pointer", {"succeeded": True, "new_pointer": True}),
    ("existing-pointer", {"succeeded": False}),
    ("peer-ordinary", {"succeeded": True, "peer_written": True}),
])
def test_real_unprotected_pinned_repo_keeps_per_worktree_rules(repository_sandbox, protect_base, probe, expected):
    """Pinned but unprotected: per-worktree pointer denies, run for real."""
    sandbox = repository_sandbox
    new_root = sandbox.trees / "extra"
    stale = sandbox.trees / "stale"
    stale.mkdir()
    (stale / ".git").write_text(f"gitdir: {sandbox.repo / '.git' / 'worktrees' / 'gone'}\n")
    command = {
        "worktree-add-from-worktree": f"git worktree add -b extra {shlex.quote(str(new_root))}",
        "remove-stale-folder-with-git": f"rm -rf {shlex.quote(str(stale))}",
        "new-pointer": f"mkdir {shlex.quote(str(new_root))} && printf forged > {shlex.quote(str(new_root / '.git'))}",
        "existing-pointer": f"printf forged > {shlex.quote(str(sandbox.own / '.git'))}",
        "peer-ordinary": f"printf allowed > {shlex.quote(str(sandbox.peer / 'ordinary'))}",
    }[probe]
    result = sandbox.run(command)
    outcome = {
        "succeeded": result.returncode == 0,
        "new_pointer": (new_root / ".git").exists(),
        "stale_present": stale.exists(),
        "peer_written": (sandbox.peer / "ordinary").exists(),
    }
    assert {key: outcome[key] for key in expected} == expected, result.stderr
    # The proved pointer stays denied.
    unchanged((sandbox.own / ".git").read_text().startswith("gitdir:"), True)
