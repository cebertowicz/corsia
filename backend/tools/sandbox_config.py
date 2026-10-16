"""Sandbox configuration — single source of truth for allowed directories.

Used by both BashTool (Safehouse CLI wrapper) and filesystem tools (Python
path gating). Both layers consult the same allowlist on every call, so a
project added at runtime is reachable without a restart.

Allowlist composition:
- The configured workspace (``infra.config.WORKSPACE``).
- Repositories with writable base checkouts as read/write.
- Repositories with protected base checkouts as read-only, with verified
  ``.worktrees`` added back as read/write. Branch identity is an independent
  control. Policy rows grant canonical real targets, not raw source links.
"""

import os
import shutil
from pathlib import Path

from infra.config import WORKSPACE
from infra.project_repo_policy import (
    RepoPolicy,
    list_registered_repo_policies,
    list_unsupported_pinned_repos,
)
from tools.repo_worktree_guidance import protected_checkout_worktree_guidance
from tools.sandbox_ignored_files import is_ignored_untracked_file
from tools.sandbox_git import verified_worktree_marker
from tools.sandbox_worktree_listing import list_verified_entries
from tools.unix_socket_policy import thread_bind_roots


def _is_within(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


_STATIC_ALLOWED_RW: list[Path] = [WORKSPACE]
_STATIC_ALLOWED_RO: list[Path] = []


def _list_authoritative_repo_policies(
    *,
    workspace: Path | None = None,
    db=None,
    containing: Path | None = None,
) -> list[RepoPolicy]:
    """Load canonical grants and stale rows retained only for quarantine."""
    return list_registered_repo_policies(
        workspace=workspace or WORKSPACE,
        db=db,
        containing=containing,
    )


def _protected_repo_policies(
    policies: list[RepoPolicy] | None = None,
) -> list[RepoPolicy]:
    return [
        policy
        for policy in (
            policies if policies is not None else _list_authoritative_repo_policies()
        )
        if (
            policy.source_binding_valid
            and (policy.protect_base_checkout or policy.enforcement_error)
        )
    ]


def _resolved_path(path: Path, cache: dict | None = None) -> Path:
    """Resolve ``path`` once when a caller owns a per-request cache."""
    key = ("sandbox-resolved-path", str(path))
    if cache is not None and key in cache:
        return cache[key]
    resolved = path.expanduser().resolve(strict=False)
    if cache is not None:
        cache[key] = resolved
    return resolved


def registered_repo_for_path(
    path: Path,
    *,
    pinned_only: bool = False,
    protected_only: bool = False,
    policies: list[RepoPolicy] | None = None,
    cache: dict | None = None,
) -> RepoPolicy | None:
    """Return the most-specific registered repository containing ``path``."""
    resolved = _resolved_path(path, cache)
    policies = (
        policies
        if policies is not None
        else _list_authoritative_repo_policies()
    )
    if pinned_only:
        policies = [policy for policy in policies if policy.branch_pinned]
    if protected_only:
        policies = [
            policy
            for policy in policies
            if policy.protect_base_checkout or policy.enforcement_error
        ]
    matches = [
        policy
        for policy in policies
        if _is_within(resolved, _resolved_path(policy.path, cache))
    ]
    return max(matches, key=lambda policy: len(policy.path.parts), default=None)


def _verified_worktree_root_and_gitdir(
    path: Path, repo_root: Path, *, cache: dict | None = None,
    deps: list[Path] | None = None,
) -> tuple[Path, Path] | None:
    """Return a linked-worktree root and gitdir after reciprocal-marker proof.

    ``deps`` collects the paths a fresh proof reads; it is ignored when the
    request ``cache`` already holds the verdict, so pass it without a cache.
    """
    repo = _resolved_path(repo_root, cache)
    resolved = _resolved_path(path, cache)
    try:
        relative = resolved.relative_to(repo)
    except ValueError:
        return None
    if len(relative.parts) < 2 or relative.parts[0] != ".worktrees":
        return None
    candidate = repo / ".worktrees" / relative.parts[1]
    cache_key = ("sandbox-verified-worktree", str(repo), str(candidate))
    if cache is not None and deps is None and cache_key in cache:
        verified = cache[cache_key]
    else:
        verified = verified_worktree_marker(candidate, repo, deps)
        if cache is not None:
            cache[cache_key] = verified
    return verified if verified is None or _is_within(resolved, verified[0]) else None


def repo_worktree_root(
    path: Path, repo_root: Path, *, cache: dict | None = None,
    deps: list[Path] | None = None,
) -> Path | None:
    """Return the verified ``.worktrees/<name>`` root containing ``path``."""
    verified = _verified_worktree_root_and_gitdir(
        path, repo_root, cache=cache, deps=deps,
    )
    return verified[0] if verified is not None else None


def repo_worktree_branch(
    path: Path, repo_root: Path, *, cache: dict | None = None,
) -> tuple[Path, str | None] | None:
    """Return a verified linked-worktree root and readable files-ref branch."""
    verified = _verified_worktree_root_and_gitdir(path, repo_root, cache=cache)
    if verified is None:
        return None
    worktree, gitdir = verified
    repo = _resolved_path(repo_root, cache)
    if (repo / ".git" / "reftable").exists():
        return worktree, None
    try:
        head = (gitdir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return worktree, None
    prefix = "ref: refs/heads/"
    branch = head.removeprefix(prefix)
    return worktree, branch if head.startswith(prefix) and branch else None


def list_repo_worktrees(repo_root: Path) -> list[Path]:
    """List verified linked worktrees beneath the required local directory."""
    return list_verified_entries(
        repo_root,
        lambda entry, repo, deps: repo_worktree_root(entry, repo, deps=deps),
    )



def _repo_write_check(
    path: Path,
    policies: list[RepoPolicy],
) -> tuple[str | None, Path | None]:
    """Return a denial or one exact protected-base file exception root."""
    policy = registered_repo_for_path(path, policies=policies)
    if policy is not None and (
        not policy.source_binding_valid or policy.enforcement_error
    ):
        reason = policy.enforcement_error or "source binding is invalid"
        return (
            f"Repository '{policy.repo_name}' enforcement failed: "
            f"{reason}. Writes are blocked.",
            None,
        )

    pinned = registered_repo_for_path(
        path,
        pinned_only=True,
        policies=policies,
    )
    if pinned is not None:
        worktree = repo_worktree_root(path, pinned.path)
        if worktree is not None and path == worktree / ".git":
            return (
                f"Repository '{pinned.repo_name}' is branch-pinned. Direct "
                "writes to worktree Git control metadata are blocked.",
                None,
            )
        try:
            relative = path.relative_to(pinned.path)
        except ValueError:
            relative = Path()
        if relative.parts and relative.parts[0] == ".git":
            return (
                f"Repository '{pinned.repo_name}' is branch-pinned. Direct "
                "writes to its Git control metadata are blocked.",
                None,
            )
    policy = registered_repo_for_path(
        path,
        protected_only=True,
        policies=policies,
    )
    if policy is not None and repo_worktree_root(path, policy.path) is None:
        if is_ignored_untracked_file(path, policy.path):
            return None, policy.path
        return protected_checkout_worktree_guidance(policy), None

    resolved = path.expanduser().resolve(strict=False)
    for unsupported in list_unsupported_pinned_repos(policies):
        if any(_is_within(resolved, root) for root in unsupported.metadata_roots):
            return (
                f"{unsupported.message} Writes to its Git metadata are blocked.",
                None,
            )
    return None, None


def check_repo_write_path(path: Path) -> str | None:
    """Reject protected-base writes; verified worktrees stay writable."""
    error, _exception_root = _repo_write_check(
        path.expanduser().resolve(strict=False),
        _list_authoritative_repo_policies(),
    )
    return error


def _allowed_dirs_rw(
    policies: list[RepoPolicy], *, worktrees: bool = True
) -> list[Path]:
    usable = [
        policy for policy in policies
        if policy.source_binding_valid and not policy.enforcement_error
    ]
    writable_roots = {
        policy.path.resolve(strict=False)
        for policy in usable if not policy.protect_base_checkout
    }
    return [
        *_STATIC_ALLOWED_RW,
        *sorted(writable_roots),
        *(
            worktree
            for policy in usable if worktrees and policy.protect_base_checkout
            for worktree in list_repo_worktrees(policy.path)
        ),
    ]


def get_sandbox_add_dirs_rw(
    thread_id: str | None = None,
    *,
    policies: list[RepoPolicy] | None = None,
) -> list[Path]:
    """Safehouse ``--add-dirs``; the appended profile grants worktrees.

    A shell with an authenticated thread identity additionally receives only
    that thread's AF_UNIX root when it is usable; the shared parent is never
    writable. Ordinary filesystem tools retain their separate path gate.

    ``policies`` is the caller's already-loaded authoritative snapshot; without
    it the policies are read now.
    """
    if policies is None:
        policies = _list_authoritative_repo_policies()
    return list(dict.fromkeys((
        *_allowed_dirs_rw(policies, worktrees=False),
        *thread_bind_roots(thread_id),
    )))


def _candidate_worktree_root(policy: RepoPolicy, target: Path) -> Path | None:
    """Return ``target``'s verified direct worktree root for one usable policy."""
    lexical = _lexical_absolute(target)
    repo = _lexical_absolute(policy.path)
    if not (
        policy.source_binding_valid and not policy.enforcement_error
        and policy.protect_base_checkout and _is_within(lexical, repo)
    ):
        return None
    relative = lexical.relative_to(repo)
    if len(relative.parts) < 2 or relative.parts[0] != ".worktrees":
        return None
    candidate = policy.path / ".worktrees" / relative.parts[1]
    return candidate if repo_worktree_root(candidate, policy.path) == candidate else None


def _allowed_dirs_rw_containing(
    policies: list[RepoPolicy], target: Path,
) -> list[Path]:
    """Return writable grant roots holding ``target`` without listing all worktrees."""
    roots = [root for root in _STATIC_ALLOWED_RW if _is_within(target, root.resolve(strict=False))]
    for policy in policies:
        worktree = _candidate_worktree_root(policy, target)
        if worktree is not None:
            roots.append(worktree)
        elif (
            policy.source_binding_valid and not policy.enforcement_error
            and not policy.protect_base_checkout
            and _is_within(target, policy.path.resolve(strict=False))
        ):
            roots.append(policy.path.resolve(strict=False))
    return roots


def get_allowed_dirs_rw(*, db=None, containing: Path | None = None) -> list[Path]:
    """RW dirs at this moment — recomputed each call so new projects pick up.

    ``containing`` narrows the answer to the roots holding that path: only its
    own repositories are verified and only its own worktree is checked.
    """
    kwargs = {} if db is None else {"db": db}
    if containing is None:
        return _allowed_dirs_rw(_list_authoritative_repo_policies(**kwargs))
    target = containing.expanduser().resolve(strict=False)
    policies = _list_authoritative_repo_policies(containing=target, **kwargs)
    return _allowed_dirs_rw_containing(policies, target)


def get_allowed_dirs_ro(*, policies: list[RepoPolicy] | None = None) -> list[Path]:
    return [
        *_STATIC_ALLOWED_RO,
        *(policy.path for policy in _protected_repo_policies(policies)),
    ]


def _lexical_absolute(path: Path) -> Path:
    """Return an absolute normalized path without following symlinks."""
    return Path(os.path.abspath(str(path.expanduser())))


def is_path_allowed(path: Path, *, for_write: bool = False) -> bool:
    """Return whether both the lexical path and resolved target are authorized."""
    resolved = path.expanduser().resolve(strict=False)
    lexical = _lexical_absolute(path)
    policies = _list_authoritative_repo_policies()
    exception_root = None
    if for_write:
        error, exception_root = _repo_write_check(resolved, policies)
        if error:
            return False
    allowed_dirs = [*_STATIC_ALLOWED_RW]
    for target in (lexical, resolved):
        allowed_dirs.extend(_allowed_dirs_rw_containing(policies, target))
    if exception_root is not None:
        allowed_dirs.append(exception_root)
    if not for_write:
        allowed_dirs += [
            *_STATIC_ALLOWED_RO,
            *(policy.path for policy in policies if policy.source_binding_valid
              and (policy.protect_base_checkout or policy.enforcement_error)),
        ]
    lexical_allowed = any(
        _is_within(lexical, _lexical_absolute(allowed))
        or _is_within(lexical, allowed.expanduser().resolve(strict=False))
        for allowed in allowed_dirs
    )
    resolved_allowed = any(
        _is_within(resolved, allowed.expanduser().resolve(strict=False))
        for allowed in allowed_dirs
    )
    return lexical_allowed and resolved_allowed


def is_safehouse_available() -> bool:
    """Check if the safehouse binary is installed."""
    return shutil.which("safehouse") is not None
