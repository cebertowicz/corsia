"""Repository branch-pin policy derived from registered project sources."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
from collections.abc import Callable

from core.project_context import resolve_project_paths
from infra.config import WORKSPACE
from infra.database import get_db
from infra.git_state_cache import cached_git_answer
from infra.project_repo_lock_recovery import recover_stale_git_index_lock
from infra.project_repo_policy_writes import (
    clear_repo_policy as clear_repo_policy,
    register_prepared_repo_policy as register_prepared_repo_policy,
)


@dataclass(frozen=True)
class RepoPolicy:
    """Canonical policy for one repository registered under ``source/``."""

    project_id: str
    repo_name: str
    path: Path
    pinned_branch: str | None
    protect_base_checkout: bool
    enforcement_error: str | None = None
    source_binding_valid: bool = True

    @property
    def branch_pinned(self) -> bool:
        """Return whether branch identity is pinned independently of checkout writes."""
        return self.pinned_branch is not None


@dataclass(frozen=True)
class RepoBranch:
    """One selectable local or uniquely-owned cached remote branch."""

    name: str
    source: str
    remote: str | None = None


@dataclass(frozen=True)
class RepoBranchSwitch:
    """The reversible effects of one controlled settings branch switch."""

    previous_branch: str
    selected_branch: str
    created_local_branch: bool


@dataclass(frozen=True)
class RepoCheckoutState:
    """Exact checkout identity and cleanliness at one policy boundary."""

    branch: str | None
    head_oid: str
    clean: bool


@dataclass(frozen=True)
class RepoFastForward:
    """Verified result of one controlled configured-upstream fast-forward."""

    branch: str
    upstream: str
    previous_oid: str
    current_oid: str
    updated: bool


RepoGitRunner = Callable[
    [Path, list[str], str, float],
    subprocess.CompletedProcess[str],
]
RepoPolicyLoader = Callable[[], list[RepoPolicy]]


class RepoPolicyQueryError(RuntimeError):
    """Repository policy truth could not be loaded safely."""


class RepoGitTimeout(ValueError):
    """A Git subprocess query or mutation exceeded its bounded deadline."""


@dataclass(frozen=True)
class UnsupportedPinnedRepo:
    """Unsupported pin plus the Git metadata paths that must stay read-only."""

    policy: RepoPolicy
    reason: str
    metadata_roots: tuple[Path, ...]

    @property
    def message(self) -> str:
        """Return descriptive recovery guidance for a rejected metadata write."""
        return (
            f"Repository '{self.policy.repo_name}' is branch-pinned but cannot be "
            f"protected safely: {self.reason}. Register a standalone files-format "
            "clone, or explicitly unpin the repository in Project settings."
        )


def _git_layout_support_error(repo: Path, git_dir: Path) -> str | None:
    """Validate the Git/common directories and files ref backend."""
    try:
        ref_storage = _run_git(
            repo,
            ["config", "--includes", "--file", str(git_dir / "config"), "--get-all", "extensions.refStorage"],
            operation="Git ref storage verification",
            timeout=5,
        )
        layout = _run_git(
            repo,
            ["rev-parse", "--show-toplevel", "--absolute-git-dir", "--git-common-dir"],
            operation="Git metadata layout verification",
            timeout=5,
        )
    except RepoGitTimeout:
        raise
    except ValueError as exc:
        return f"Git metadata layout could not be verified: {exc}"
    storage_values = [
        line.strip().lower()
        for line in ref_storage.stdout.splitlines()
        if line.strip()
    ]
    if ref_storage.returncode not in (0, 1):
        detail = (ref_storage.stderr or ref_storage.stdout).strip()[:160]
        return f"Git ref storage could not be verified: {detail or 'git config failed'}"
    if storage_values and storage_values != ["files"]:
        storage = ", ".join(storage_values)
        return (
            f"Git ref storage `{storage}` is not supported by branch pinning; "
            "only `files` ref storage can be protected"
        )
    lines = [line.strip() for line in layout.stdout.splitlines() if line.strip()]
    if layout.returncode != 0 or len(lines) != 3:
        detail = (layout.stderr or layout.stdout).strip()[:160]
        return f"Git metadata layout could not be verified: {detail or 'git failed'}"
    expected_git_dir = git_dir.resolve(strict=False)
    if Path(lines[0]).resolve(strict=False) != repo:
        return "Git top-level does not match the registered checkout"
    if any(
        (
            (path if (path := Path(value)).is_absolute() else repo / path).resolve(
                strict=False
            )
            != expected_git_dir
        )
        for value in lines[1:3]
    ):
        return (
            "branch pinning requires repository-local Git metadata; this checkout "
            "resolves to a different Git or common directory"
        )

    return None


def _branch_path(git_dir: Path) -> tuple[Path | None, str | None]:
    """Return the local branch path plus any validation error."""
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError as exc:
        return None, f"Git HEAD could not be verified: {exc}"
    prefix = "ref: refs/heads/"
    branch = head.removeprefix(prefix) if head.startswith(prefix) else ""
    branch_path = Path(branch)
    if not branch or branch_path.is_absolute() or ".." in branch_path.parts:
        return None, "branch pinning requires HEAD to name a local branch"
    return branch_path, None


def _identity_paths(git_dir: Path, branch_path: Path | None) -> list[Path]:
    """Return metadata paths that carry the checkout or branch identity."""
    paths = [
        git_dir / "HEAD",
        git_dir / "config",
        git_dir / "index",
        git_dir / "packed-refs",
        git_dir / "refs",
        git_dir / "refs" / "heads",
        git_dir / "logs",
        git_dir / "logs" / "HEAD",
        git_dir / "reftable",
    ]
    if branch_path is not None:
        paths.extend(
            [
                git_dir / "refs" / "heads" / branch_path,
                git_dir / "logs" / "refs" / "heads" / branch_path,
            ]
        )
    return paths


def branch_pin_support_error(repo_path: Path) -> str | None:
    """Return why this checkout cannot be protected by the branch-pin sandbox."""
    repo = repo_path.expanduser().resolve(strict=False)
    git_dir = repo / ".git"
    if git_dir.is_symlink() or not git_dir.is_dir():
        return (
            "branch pinning requires a standalone checkout with a real `.git` "
            "directory; linked worktrees and submodules share Git metadata "
            "outside the registered checkout"
        )
    if (git_dir / "commondir").exists():
        return (
            "branch pinning requires repository-local Git metadata; this checkout "
            "uses a shared common Git directory"
        )
    if (git_dir / "reftable").exists():
        return (
            "Git ref storage `reftable` is not supported by branch pinning; "
            "only `files` ref storage can be protected"
        )
    if layout_error := cached_git_answer(
        repo, "layout", lambda: _git_layout_support_error(repo, git_dir), cache_if=lambda error: error is None,
    ):
        return layout_error
    branch_path, branch_error = _branch_path(git_dir)
    if branch_error:
        return branch_error
    expected_git_dir = git_dir.resolve(strict=False)
    for path in _identity_paths(git_dir, branch_path):
        resolved = path.resolve(strict=False)
        if path.is_symlink() or not resolved.is_relative_to(expected_git_dir):
            return (
                "branch pinning requires repository-local Git identity metadata; "
                f"`{path.relative_to(git_dir)}` is symlinked or resolves outside `.git`"
            )
    return None


def _minimal_roots(roots: set[Path]) -> tuple[Path, ...]:
    """Drop child roots already covered by a parent quarantine."""
    return tuple(
        sorted(
            (
                root
                for root in roots
                if not any(
                    root != other and root.is_relative_to(other)
                    for other in roots
                )
            ),
            key=str,
        )
    )


def _resolved_git_metadata_roots(repo: Path) -> set[Path]:
    """Ask Git for private/common metadata roots when the checkout is valid."""
    try:
        layout = _run_git(
            repo,
            ["rev-parse", "--absolute-git-dir", "--git-common-dir"],
            operation="Git metadata root resolution",
            timeout=5,
        )
    except ValueError:
        return set()
    lines = [line.strip() for line in layout.stdout.splitlines() if line.strip()]
    if layout.returncode != 0 or len(lines) != 2:
        return set()
    roots: set[Path] = set()
    for value in lines:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = repo / path
        roots.add(path.resolve(strict=False))
    return roots


def _unsupported_metadata_roots(repo_path: Path) -> tuple[Path, ...]:
    """Return metadata roots to quarantine for an unsupported checkout."""
    repo = repo_path.expanduser().resolve(strict=False)
    marker = repo / ".git"
    roots = _resolved_git_metadata_roots(repo)
    if marker.is_dir() and not marker.is_symlink():
        git_dir = marker.resolve(strict=False)
        branch_path, _ = _branch_path(marker)
        roots.add(git_dir)
        roots.update(
            resolved
            for path in _identity_paths(marker, branch_path)
            if not (resolved := path.resolve(strict=False)).is_relative_to(git_dir)
        )
    return _minimal_roots(roots)


def list_unsupported_pinned_repos(policies: list[RepoPolicy]) -> list[UnsupportedPinnedRepo]:
    """Return unsupported pins with the metadata roots they quarantine."""
    unsupported: list[UnsupportedPinnedRepo] = []
    for policy in policies:
        if not policy.branch_pinned:
            continue
        try:
            pin_error = branch_pin_support_error(policy.path)
        except RepoGitTimeout as exc:
            pin_error = str(exc)
        if pin_error:
            unsupported.append(
                UnsupportedPinnedRepo(
                    policy=policy,
                    reason=pin_error,
                    metadata_roots=_unsupported_metadata_roots(policy.path),
                )
            )
    return unsupported


def _run_git(
    repo: Path, argv: list[str], *, operation: str, timeout: int,
) -> subprocess.CompletedProcess[str]:
    """Run one bounded Git command; a timeout raises the retryable RepoGitTimeout."""
    try:
        return subprocess.run(
            ["git", *argv], cwd=str(repo), capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RepoGitTimeout(f"{operation} timed out: {exc}") from exc
    except (FileNotFoundError, OSError) as exc:
        raise ValueError(f"{operation} failed: {exc}") from exc


def current_repo_branch(repo_path: Path) -> str:
    """Return the checkout's current named local branch."""
    repo = repo_path.expanduser().resolve(strict=False)
    result = _run_git(
        repo, ["symbolic-ref", "--quiet", "--short", "HEAD"],
        operation="current branch read", timeout=5,
    )
    branch = result.stdout.strip()
    if result.returncode != 0 or not branch:
        detail = (result.stderr or result.stdout).strip()[:160]
        raise ValueError(
            "repository must be on a local branch"
            + (f": {detail}" if detail else "")
        )
    return branch


def _git_lines(repo: Path, args: list[str], *, operation: str) -> list[str]:
    """Run one read-only Git query and return its non-empty output lines."""
    result = _run_git(repo, args, operation=operation, timeout=10)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[:240]
        raise ValueError(f"{operation} failed: {detail or 'git failed'}")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _branch_inventory(
    repo_path: Path,
) -> tuple[set[str], dict[str, set[str]]]:
    """Return local names and configured-remote owners for cached refs."""
    repo = repo_path.expanduser().resolve(strict=False)
    local = {
        ref.removeprefix("refs/heads/")
        for ref in _git_lines(
            repo,
            ["for-each-ref", "--format=%(refname)", "refs/heads"],
            operation="local branch listing",
        )
        if ref.startswith("refs/heads/")
    }
    remotes = set(_git_lines(repo, ["remote"], operation="remote listing"))
    remote_branches: dict[str, set[str]] = {}
    for ref in _git_lines(
        repo,
        ["for-each-ref", "--format=%(refname)", "refs/remotes"],
        operation="remote branch listing",
    ):
        owners = [
            remote
            for remote in remotes
            if ref.startswith(f"refs/remotes/{remote}/")
        ]
        if not owners:
            continue
        remote = max(owners, key=len)
        branch = ref.removeprefix(f"refs/remotes/{remote}/")
        if not branch or branch == "HEAD":
            continue
        remote_branches.setdefault(branch, set()).add(remote)
    return local, remote_branches


def list_repo_branches(repo_path: Path) -> list[RepoBranch]:
    """List local branches and cached branches unique to one configured remote."""
    local, remote_branches = _branch_inventory(repo_path)
    local.add(current_repo_branch(repo_path))
    branches = [RepoBranch(name=name, source="local") for name in local]
    branches.extend(
        RepoBranch(name=name, source="remote", remote=next(iter(remotes)))
        for name, remotes in remote_branches.items()
        if name not in local and len(remotes) == 1
    )
    return sorted(branches, key=lambda branch: branch.name)


def _repo_is_clean(repo: Path) -> bool:
    return not _git_lines(
        repo,
        ["status", "--porcelain=v1", "--untracked-files=normal"],
        operation="repository status",
    )


def repo_checkout_state(repo_path: Path) -> RepoCheckoutState:
    """Capture branch, HEAD, and cleanliness in one Git status query."""
    repo = repo_path.expanduser().resolve(strict=False)
    lines = _git_lines(
        repo,
        ["status", "--porcelain=v2", "--branch", "--untracked-files=normal"],
        operation="repository checkout status",
    )
    branch_values = [
        line.removeprefix("# branch.head ")
        for line in lines
        if line.startswith("# branch.head ")
    ]
    oid_values = [
        line.removeprefix("# branch.oid ")
        for line in lines
        if line.startswith("# branch.oid ")
    ]
    if len(branch_values) != 1 or len(oid_values) != 1:
        raise ValueError("repository checkout identity could not be read")
    branch = branch_values[0]
    return RepoCheckoutState(
        branch=None if branch == "(detached)" else branch,
        head_oid=oid_values[0],
        clean=all(line.startswith("# ") for line in lines),
    )


def _run_branch_mutation(repo: Path, args: list[str], *, operation: str) -> None:
    """Run a direct-argv Git mutation with all repository hooks disabled."""
    result = _run_git(
        repo, ["-c", "core.hooksPath=/dev/null", *args], operation=operation, timeout=30,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[:240]
        raise ValueError(f"{operation} failed: {detail or 'git failed'}")


def _switch_existing_local(repo: Path, branch: str) -> None:
    _run_branch_mutation(
        repo,
        ["switch", "--", branch],
        operation=f"branch switch to {branch}",
    )


def switch_repo_branch(repo_path: Path, target_branch: str) -> RepoBranchSwitch:
    """Move a clean named checkout to a local or unique cached remote branch."""
    repo = repo_path.expanduser().resolve(strict=False)
    target = target_branch.strip()
    if not target:
        raise ValueError("missing branch selection")
    _git_lines(
        repo,
        ["check-ref-format", "--branch", target],
        operation="branch validation",
    )
    previous = current_repo_branch(repo)
    if not _repo_is_clean(repo):
        raise ValueError("dirty checkout cannot change branches")
    local, remote_branches = _branch_inventory(repo)
    created = False
    if target == previous:
        return RepoBranchSwitch(previous, target, False)
    if target in local:
        mutation = ["switch", "--", target]
    else:
        owners = remote_branches.get(target, set())
        if len(owners) > 1:
            raise ValueError(
                f"ambiguous remote branch '{target}' exists on multiple remotes"
            )
        if not owners:
            raise ValueError(f"missing branch '{target}'")
        remote = next(iter(owners))
        mutation = [
            "switch",
            "--track",
            "-c",
            target,
            "--",
            f"{remote}/{target}",
        ]
        created = True
    try:
        _run_branch_mutation(
            repo,
            mutation,
            operation=f"branch switch to {target}",
        )
        if current_repo_branch(repo) != target or not _repo_is_clean(repo):
            raise ValueError(
                f"branch switch to {target} did not leave the requested clean checkout"
            )
    except Exception as exc:
        try:
            if not _repo_is_clean(repo):
                raise RuntimeError("branch-switch rollback found a dirty checkout")
            if current_repo_branch(repo) != previous:
                _switch_existing_local(repo, previous)
            local_after_failure, _remote = _branch_inventory(repo)
            if created and target in local_after_failure:
                _run_branch_mutation(
                    repo,
                    ["branch", "-D", "--", target],
                    operation=f"rollback branch {target}",
                )
        except Exception as rollback_exc:
            raise RuntimeError(
                f"{exc}; checkout rollback also failed: {rollback_exc}"
            ) from exc
        raise
    return RepoBranchSwitch(previous, target, created)


def restore_repo_branch_switch(
    repo_path: Path,
    switched: RepoBranchSwitch,
) -> None:
    """Undo one controlled switch, including any created tracking branch."""
    repo = repo_path.expanduser().resolve(strict=False)
    if not _repo_is_clean(repo):
        raise RuntimeError("cannot restore branch switch from a dirty checkout")
    if current_repo_branch(repo) != switched.previous_branch:
        _switch_existing_local(repo, switched.previous_branch)
    if (
        switched.created_local_branch
        and switched.selected_branch != switched.previous_branch
    ):
        try:
            _run_branch_mutation(
                repo,
                ["branch", "-D", "--", switched.selected_branch],
                operation=f"rollback branch {switched.selected_branch}",
            )
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc


def fast_forward_repo(
    policy: RepoPolicy,
    *,
    runner: RepoGitRunner,
    policy_loader: RepoPolicyLoader,
    timeout: float,
) -> RepoFastForward:
    """Fast-forward one clean base to its sole configured upstream."""
    repo = policy.path
    if policy.enforcement_error:
        raise ValueError(
            f"repository enforcement is invalid: {policy.enforcement_error}"
        )
    if timeout <= 0:
        raise ValueError("controlled fast-forward timeout must be positive")
    if (
        not repo.is_absolute()
        or repo.is_symlink()
        or not repo.is_dir()
        or repo.resolve(strict=True) != repo
        or (repo / ".git").is_symlink()
        or not (repo / ".git").is_dir()
    ):
        raise ValueError(
            "registered repository must be a canonical standalone checkout"
        )
    deadline = time.monotonic() + timeout

    def _run(
        args: list[str],
        *,
        operation: str,
    ) -> subprocess.CompletedProcess[str]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"{operation} timed out before the Git command could start"
            )
        try:
            return runner(repo, args, operation, remaining)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"{operation} timed out") from exc
        except (FileNotFoundError, OSError) as exc:
            raise ValueError(f"{operation} failed: {exc}") from exc

    def _query(args: list[str], *, operation: str) -> list[str]:
        result = _run(args, operation=operation)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[:240]
            raise ValueError(f"{operation} failed: {detail or 'git failed'}")
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]
    def _one(args: list[str], *, operation: str) -> str:
        lines = _query(args, operation=operation)
        if len(lines) != 1:
            raise ValueError(
                f"{operation} failed: expected one value, found {len(lines)}"
            )
        return lines[0]

    def _branch() -> str:
        return _one(
            ["symbolic-ref", "--quiet", "--short", "HEAD"],
            operation="current branch verification",
        )

    def _clean() -> bool:
        return not _query(
            ["status", "--porcelain=v1", "--untracked-files=normal"],
            operation="repository status",
        )

    def _local_config(key: str, label: str) -> str:
        return _one(
            ["config", "--local", "--includes", "--get-all", key],
            operation=label,
        )

    def _revalidate_policy() -> None:
        try:
            live = [
                candidate
                for candidate in policy_loader()
                if candidate.path == policy.path
            ]
        except Exception as exc:
            raise ValueError(f"repository policy query failed: {exc}") from exc
        if len(live) != 1 or (
            live[0].pinned_branch,
            live[0].protect_base_checkout,
            live[0].enforcement_error,
        ) != (
            policy.pinned_branch,
            policy.protect_base_checkout,
            policy.enforcement_error,
        ):
            raise ValueError("repository policy changed during fast-forward")

    recover_stale_git_index_lock(repo / ".git")  # Shared runtime rule before mutation.
    branch = _branch()
    if policy.pinned_branch is not None and branch != policy.pinned_branch:
        raise ValueError(
            f"pinned branch {policy.pinned_branch!r} does not match {branch!r}"
        )
    if not _clean():
        raise ValueError("dirty checkout cannot be fast-forwarded")
    original_oid = _one(
        ["rev-parse", "--verify", "HEAD^{commit}"],
        operation="current HEAD verification",
    )
    remote = _local_config(
        f"branch.{branch}.remote",
        "configured upstream remote",
    )
    merge_ref = _local_config(
        f"branch.{branch}.merge",
        "configured upstream merge ref",
    )
    if remote == "." or not merge_ref.startswith("refs/heads/"):
        raise ValueError("missing configured remote upstream")
    if remote not in set(_query(["remote"], operation="remote listing")):
        raise ValueError(f"missing configured upstream remote {remote!r}")

    _revalidate_policy()
    fetched = _run(
        ["fetch", "--", remote],
        operation=f"configured upstream fetch from {remote}",
    )
    if fetched.returncode != 0:
        detail = (fetched.stderr or fetched.stdout).strip()[:240]
        raise ValueError(f"configured upstream fetch failed: {detail or 'git failed'}")

    if _branch() != branch or not _clean():
        raise RuntimeError("configured upstream fetch changed base checkout state")
    if _one(
        ["rev-parse", "--verify", "HEAD^{commit}"],
        operation="pre-merge HEAD verification",
    ) != original_oid:
        raise RuntimeError("configured upstream fetch changed base HEAD")
    upstream = _one(
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
        operation="configured upstream resolution",
    )
    upstream_oid = _one(
        ["rev-parse", "--verify", "@{upstream}^{commit}"],
        operation="configured upstream commit resolution",
    )
    ancestry = _run(
        ["merge-base", "--is-ancestor", original_oid, upstream_oid],
        operation="fast-forward ancestry check",
    )
    if ancestry.returncode == 1:
        raise ValueError("configured upstream requires a non-fast-forward update")
    if ancestry.returncode != 0:
        detail = (ancestry.stderr or ancestry.stdout).strip()[:240]
        raise ValueError(
            f"fast-forward ancestry check failed: {detail or 'git failed'}"
        )

    if original_oid != upstream_oid:
        _revalidate_policy()
        merged = _run(
            ["merge", "--ff-only", "--no-edit", upstream_oid],
            operation=f"fast-forward {branch} from {upstream}",
        )
        if merged.returncode != 0:
            detail = (merged.stderr or merged.stdout).strip()[:240]
            raise ValueError(f"fast-forward merge failed: {detail or 'git failed'}")
    current_oid = _one(
        ["rev-parse", "--verify", "HEAD^{commit}"],
        operation="fast-forward postcondition",
    )
    if _branch() != branch or current_oid != upstream_oid or not _clean():
        raise RuntimeError(
            "controlled fast-forward did not leave the requested clean branch"
        )
    return RepoFastForward(
        branch=branch,
        upstream=upstream,
        previous_oid=original_oid,
        current_oid=current_oid,
        updated=original_oid != current_oid,
    )


def _persisted_repo_path(value: object) -> Path | None:
    """Return a canonical absolute lexical identity without following it."""
    if not isinstance(value, str):
        return None
    raw = value
    path = Path(raw)
    if (
        not path.is_absolute()
        or str(path) != raw
        or os.path.normpath(raw) != raw
    ):
        return None
    return path

def _valid_repo_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    name = value
    if (
        not name
        or name != name.strip()
        or Path(name).name != name
        or name in {".", ".."}
        or name.startswith(".")
    ):
        return None
    return name


def _policy_from_row(project_id: str, row) -> RepoPolicy | None:
    repo_name = _valid_repo_name(row["repo_name"])
    repo_path = _persisted_repo_path(row["repo_path"])
    pinned = row["pinned_branch"]
    protection = row["protect_base_checkout"]
    if (
        repo_name is None
        or repo_path is None
        or type(protection) is not int
        or protection not in (0, 1)
        or (
            pinned is not None
            and (
                not isinstance(pinned, str)
                or not pinned
                or pinned != pinned.strip()
            )
        )
    ):
        return None
    return RepoPolicy(
        project_id=project_id,
        repo_name=repo_name,
        path=repo_path,
        pinned_branch=pinned,
        protect_base_checkout=bool(protection),
    )


def load_project_repo_policy_snapshot(
    project_id: str | None,
    *,
    db=None,
) -> tuple[list[dict[str, object]], str, str]:
    """Return strict scoped rows, canonical JSON, and their identity hash.

    Persisted values are canonical truth, so reads never trim or otherwise
    repair whitespace. A noncanonical row blocks snapshot creation instead of
    silently disappearing from reviewer context.
    """
    if project_id is not None and (
        not isinstance(project_id, str)
        or not project_id
        or project_id != project_id.strip()
    ):
        raise RepoPolicyQueryError("requesting project identity is malformed")

    owns_connection = db is None
    connection = db or get_db()
    try:
        if project_id is None:
            policies: list[RepoPolicy] = []
        else:
            try:
                raw_rows = connection.execute(
                    "SELECT repo_name,repo_path,pinned_branch,"
                    "protect_base_checkout FROM project_repo_policies "
                    "WHERE project_id=? ORDER BY repo_name",
                    (project_id,),
                ).fetchall()
            except sqlite3.Error as exc:
                raise RepoPolicyQueryError(
                    "requesting project repository policy is unavailable"
                ) from exc
            policies = []
            for row in raw_rows:
                policy = _policy_from_row(project_id, row)
                if policy is None:
                    raise RepoPolicyQueryError(
                        "requesting project repository policy is malformed"
                    )
                policies.append(policy)
    finally:
        if owns_connection:
            connection.close()

    rows: list[dict[str, object]] = [
        {
            "repo_name": policy.repo_name,
            "repo_path": str(policy.path),
            "pinned_branch": policy.pinned_branch,
            "protect_base_checkout": policy.protect_base_checkout,
        }
        for policy in policies
    ]
    encoded = json.dumps(
        rows,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    hash_payload = json.dumps(
        {
            "version": 1,
            "requesting_project_id": project_id,
            "repo_policy_snapshot": rows,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return rows, encoded, hashlib.sha256(hash_payload).hexdigest()


def get_project_repo_policies(
    project_id: str,
    *,
    db=None,
) -> dict[str, RepoPolicy]:
    """Return canonical persisted rows without granting unvalidated bindings."""
    owns_connection = db is None
    connection = db or get_db()
    try:
        rows = connection.execute(
            "SELECT repo_name, repo_path, pinned_branch, protect_base_checkout "
            "FROM project_repo_policies WHERE project_id = ?",
            (project_id,),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        detail = str(exc).lower()
        if "no such table" not in detail and "no such column" not in detail:
            raise
        rows = []
    finally:
        if owns_connection:
            connection.close()
    policies = (
        policy
        for row in rows
        if (policy := _policy_from_row(project_id, row)) is not None
    )
    return {policy.repo_name: policy for policy in policies}


def prepare_repo_policy(
    project_id: str, repo_name: str, repo_path: Path, *,
    pinned_branch: str | None, protect_base_checkout: bool,
) -> RepoPolicy:
    """Validate registration values and resolve its path before database work."""
    valid_name = _valid_repo_name(repo_name)
    if valid_name is None:
        raise ValueError(f"invalid repository name: {repo_name!r}")
    normalized_branch = pinned_branch.strip() if pinned_branch is not None else None
    if pinned_branch is not None and not normalized_branch:
        raise ValueError("pinned_branch must be a non-empty branch name or null")
    try:
        resolved = repo_path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"repository path could not be resolved: {exc}") from exc
    if not resolved.is_dir():
        raise ValueError(f"repository path is not a directory: {resolved}")
    return RepoPolicy(
        project_id=project_id, repo_name=valid_name, path=resolved,
        pinned_branch=normalized_branch, protect_base_checkout=protect_base_checkout,
    )


def register_repo_policy(
    project_id: str, repo_name: str, repo_path: Path, *,
    pinned_branch: str | None, protect_base_checkout: bool, db=None,
) -> RepoPolicy:
    """Persist one authoritative registration without committing caller state."""
    policy = prepare_repo_policy(
        project_id, repo_name, repo_path, pinned_branch=pinned_branch,
        protect_base_checkout=protect_base_checkout,
    )
    return register_prepared_repo_policy(policy, db=db)


def coalesce_repo_policies(
    policies: list[RepoPolicy],
    *,
    verify_current_branch: bool = True,
) -> list[RepoPolicy]:
    """Coalesce duplicate paths into one strict, fail-closed policy."""
    grouped: dict[Path, list[RepoPolicy]] = {}
    for policy in policies:
        path = (
            policy.path.resolve(strict=False)
            if policy.source_binding_valid
            else policy.path
        )
        grouped.setdefault(path, []).append(policy)

    effective: list[RepoPolicy] = []
    for path, registrations in grouped.items():
        first = registrations[0]
        pins = sorted(
            {
                policy.pinned_branch
                for policy in registrations
                if policy.pinned_branch is not None
            }
        )
        pinned_branch = pins[0] if pins else None
        errors = [
            policy.enforcement_error
            for policy in registrations
            if policy.enforcement_error
        ]
        source_binding_valid = any(
            policy.source_binding_valid for policy in registrations
        )
        if len(pins) > 1:
            errors.append(
                "conflicting pinned branches for the same registered repository: "
                + ", ".join(repr(pin) for pin in pins)
            )
        elif (
            pinned_branch is not None
            and verify_current_branch
            and source_binding_valid
        ):
            try:
                actual_branch = cached_git_answer(path, "current-branch", lambda: current_repo_branch(path))
            except ValueError as exc:
                errors.append(
                    f"could not verify pinned branch {pinned_branch!r}: {exc}"
                )
            else:
                if actual_branch != pinned_branch:
                    errors.append(
                        f"current branch {actual_branch!r} differs from pinned "
                        f"branch {pinned_branch!r}"
                    )
        effective.append(
            RepoPolicy(
                project_id=first.project_id,
                repo_name=first.repo_name,
                path=path,
                pinned_branch=pinned_branch,
                protect_base_checkout=any(
                    policy.protect_base_checkout
                    for policy in registrations
                ),
                enforcement_error="; ".join(errors) if errors else None,
                source_binding_valid=source_binding_valid,
            )
        )
    return effective

def list_registered_repo_policies(
    *, workspace: Path | None = None, db=None, project_id: str | None = None,
    containing: Path | None = None,
) -> list[RepoPolicy]:
    """Return live policies scoped by project or by a contained path before coalescing."""
    workspace = workspace or WORKSPACE
    owns_connection = db is None
    connection = db or get_db()
    try:
        try:
            where = " WHERE p.id = ?" if project_id is not None else ""
            params = (project_id,) if project_id is not None else ()
            rows = connection.execute(
                "SELECT p.id, p.name, p.path, r.repo_name, r.repo_path, "
                "r.pinned_branch, r.protect_base_checkout "
                "FROM project_repo_policies AS r "
                "JOIN projects AS p ON p.id = r.project_id" + where
                + " ORDER BY p.id, r.repo_name",
                params,
            ).fetchall()
        except sqlite3.Error as exc:
            raise RepoPolicyQueryError(
                f"repository policy query failed: {exc}"
            ) from exc
        policies: list[RepoPolicy] = []
        for row in rows:
            project_id = str(row["id"])
            policy = _policy_from_row(project_id, row)
            if policy is None:
                continue
            try:
                _name, planning_root = resolve_project_paths(
                    workspace,
                    project_id,
                    db=connection,
                )
            except ValueError as exc:
                binding_error = f"project planning root is invalid: {exc}"
            else:
                binding_error = _source_binding_error(planning_root, policy)
            if binding_error is not None:
                policy = replace(
                    policy, enforcement_error=binding_error, source_binding_valid=False,
                )
            # Same key coalescing groups by, so a kept group is always whole.
            key = policy.path.resolve(strict=False) if policy.source_binding_valid else policy.path
            if containing is None or containing.is_relative_to(key):
                policies.append(policy)
        return coalesce_repo_policies(policies)
    finally:
        if owns_connection:
            connection.close()


def _source_binding_error(
    planning_root: Path | None,
    policy: RepoPolicy,
) -> str | None:
    """Return why a policy is not bound through its canonical planning root."""
    if planning_root is None:
        return "project planning root is unresolved"
    entry = planning_root / "source" / policy.repo_name
    try:
        current_target = entry.resolve(strict=True)
    except OSError:
        return f"source binding '{policy.repo_name}' is missing"
    if current_target.is_dir() and current_target == policy.path:
        return None
    return (
        f"source binding '{policy.repo_name}' no longer targets "
        "the registered checkout"
    )
