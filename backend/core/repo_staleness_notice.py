"""Warn when a read touches a registered source repo behind its remote (#936).

A registered base checkout sits at whatever commit it was left on; nothing
updates it. This attaches a one-line warning to the model-facing text of a
``read_file``, ``grep``, ``glob``, or ``list_dir`` result whose resolved
target lives inside such a repo and whose remote has moved ahead of it.

Design: ``shared/substrate/stale-repo-warning-design-2026-09-19.md``.

Two independent TTL caches keep this off the hot path:

- The repo policy list (SQLite) is cached ~60s (``_POLICY_CACHE_TTL_SECONDS``),
  mirroring the freshness ``infra.project_repo_policy`` callers already accept.
- The remote-vs-local staleness answer per repo is cached 5 minutes
  (``_REMOTE_CACHE_TTL_SECONDS``), mirroring ``infra/update_status.py``'s
  banner cache. Unlike that module, the network probe (``git ls-remote``)
  never runs synchronously: a miss or expired entry schedules a background
  thread and this call answers immediately from whatever it already has
  (``None`` on a cold cache — silent, the next read a moment later carries
  the answer). This is the one place this module intentionally departs from
  ``update_status.py``'s shape, because that module's caller is an on-demand
  UI endpoint and this one runs on every tool result.

A repo is "behind" when its remote branch head is not provably an ancestor of
local HEAD. Without ever fetching, the remote's tip commit is usually not in
the local object database at all, so ``git merge-base --is-ancestor`` exits
non-zero (1 "not an ancestor", or 128 "not a valid object") for both the
plain-behind and the diverged case; either way the local checkout does not
provably contain the remote's history, which is exactly "behind" by this
module's definition. Only a clean 0 exit (or an identical SHA) means the
remote commit is confirmed present locally, i.e. not behind.

The repeat rule (at most once per repo per 10 minutes per thread, and again
whenever the remote head moves) lives in ``_repeat_state``, keyed by
``(thread_id, repo path)``.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from infra.config import WORKSPACE
from infra.product_events import emit_product_event
from infra.project_repo_policy import (
    RepoPolicy,
    RepoPolicyQueryError,
    list_registered_repo_policies,
)
from tools.filesystem_gate import join_and_resolve
from tools.sandbox_config import registered_repo_for_path, repo_worktree_root

_STALENESS_READ_TOOLS = frozenset({"read_file", "grep", "glob", "list_dir"})
_DEFAULT_TARGET_TOOLS = frozenset({"grep", "glob"})

_GIT_ENV_OVERRIDES = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "true",
    "GIT_SSH_COMMAND": "ssh -oBatchMode=yes",
}
_LOCAL_GIT_TIMEOUT_SECONDS = 5
_NETWORK_GIT_TIMEOUT_SECONDS = 10

_POLICY_CACHE_TTL_SECONDS = 60
_REMOTE_CACHE_TTL_SECONDS = 300
_REPEAT_WINDOW_SECONDS = 600

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _RepoProbe:
    """One background probe's answer for a repo, cached process-wide."""

    branch: str
    behind: bool
    commits_behind: int | None
    remote_sha: str | None
    checked_at: float


_policy_cache_lock = threading.Lock()
_policy_cache: dict[str, Any] = {"data": None, "ts": 0.0}

_remote_cache_lock = threading.Lock()
_remote_cache: dict[str, dict[str, Any]] = {}

_repeat_lock = threading.Lock()
_repeat_state: dict[tuple[str, str], tuple[float, str | None]] = {}


def repo_staleness_notice(
    *, thread_id: str | None, tool_name: str, arguments: Any, workspace: Path | None = None,
) -> str | None:
    """Return the stale-repo notice for this read result, or None.

    Does filesystem I/O (path resolution, policy-path stats, an occasional
    SQLite read on a cold 60s policy cache) and, when it delivers a notice,
    a local analytics write via ``emit_product_event``; never a network
    call. Both call sites run it through ``asyncio.to_thread`` for exactly
    that reason, so it must never be called inline on an event loop.

    ``workspace`` must match the workspace the calling tool actually resolved
    a relative path against (the harness bridge builds each registry against
    its own working directory, not the global one), or a relative-path read
    resolves to the wrong location and the notice is silently skipped or
    attributed to the wrong repo.
    """
    target = _target_path(tool_name, arguments)
    if target is None:
        return None
    resolved = _resolve_target(target, workspace or WORKSPACE)
    if resolved is None:
        return None
    policy = registered_repo_for_path(resolved, policies=_cached_policies())
    if policy is None or repo_worktree_root(resolved, policy.path) is not None:
        return None
    probe = _cached_probe(policy.path)
    if probe is None or not probe.behind:
        return None
    if not _should_emit(thread_id, str(policy.path), probe.remote_sha):
        return None
    emit_product_event(
        "repo_staleness_notice",
        "notice_emitted",
        thread_id=thread_id,
        properties={
            "repo_name": policy.repo_name,
            "commits_behind": probe.commits_behind,
        },
    )
    return _render_notice(policy, probe)


def _target_path(tool_name: str, arguments: Any) -> str | None:
    """Return the raw path argument a staleness-relevant tool call resolved."""
    if tool_name not in _STALENESS_READ_TOOLS or not isinstance(arguments, dict):
        return None
    path = arguments.get("path")
    if path is None and tool_name in _DEFAULT_TARGET_TOOLS:
        path = "."
    return path if isinstance(path, str) and path else None


def _resolve_target(raw: str, workspace: Path) -> Path | None:
    """Resolve a tool's raw path argument through the one join/resolve rule tools share.

    Delegates to ``tools.filesystem_gate.join_and_resolve`` rather than
    re-deriving the join/resolve computation here, so a future change to how
    tools resolve their own targets cannot silently desync this module's
    notion of "what did this read touch" from theirs. Deliberately not the
    gate's full ``resolve_path``: that also makes an allow/deny decision,
    which is a different question from "where does this path point", and
    this module must never raise or duplicate a permission judgment it does
    not own.
    """
    try:
        return join_and_resolve(raw, workspace)
    except (OSError, ValueError):
        return None


def _cached_policies() -> list[RepoPolicy]:
    """Return the registered-repo policy list, refreshed at most once per minute.

    A best-effort advisory notice must never fail the tool result it rides on:
    ``list_registered_repo_policies`` raises ``RepoPolicyQueryError`` on a
    malformed project identity or a failed DB query, which every other
    non-test caller in the tree catches. An empty list here means "no notice
    this round", not "no repos registered" — the next call retries once the
    cache expires.
    """
    now = time.monotonic()
    with _policy_cache_lock:
        data = _policy_cache["data"]
        if data is not None and now - _policy_cache["ts"] < _POLICY_CACHE_TTL_SECONDS:
            return data
    try:
        policies = list_registered_repo_policies(workspace=WORKSPACE)
    except RepoPolicyQueryError:
        log.warning("repo_staleness_notice: policy query failed", exc_info=True)
        emit_product_event("repo_staleness_notice", "policy_query_failed")
        policies = []
    with _policy_cache_lock:
        _policy_cache["data"] = policies
        _policy_cache["ts"] = now
    return policies


def _cached_probe(repo_root: Path) -> _RepoProbe | None:
    """Return the last known staleness answer, scheduling a refresh if stale."""
    key = str(repo_root)
    now = time.monotonic()
    should_refresh = False
    with _remote_cache_lock:
        entry = _remote_cache.get(key)
        expired = entry is None or now - entry["ts"] >= _REMOTE_CACHE_TTL_SECONDS
        if expired and not (entry or {}).get("refreshing", False):
            should_refresh = True
            _remote_cache[key] = {
                "data": entry["data"] if entry else None,
                "ts": entry["ts"] if entry else 0.0,
                "refreshing": True,
            }
        data = _remote_cache[key]["data"] if key in _remote_cache else None
    if should_refresh:
        threading.Thread(
            target=_refresh_probe, args=(repo_root, key), daemon=True,
        ).start()
    return data


def _refresh_probe(repo_root: Path, key: str) -> None:
    """Run the real probe off-thread and publish its answer to the cache."""
    try:
        result = _probe(repo_root)
    except Exception as exc:
        log.warning("repo_staleness_notice: probe failed for %s", repo_root, exc_info=True)
        emit_product_event(
            "repo_staleness_notice",
            "probe_failed",
            properties={"repo_root": str(repo_root), "error": str(exc)[:200]},
        )
        result = None
    with _remote_cache_lock:
        _remote_cache[key] = {"data": result, "ts": time.monotonic(), "refreshing": False}


def _run_git(
    repo_root: Path, args: list[str], timeout: float,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env={**os.environ, **_GIT_ENV_OVERRIDES},
    )


def _probe(repo_root: Path) -> _RepoProbe | None:
    """Answer whether ``repo_root`` is behind its remote, or None if unknown."""
    try:
        branch_result = _run_git(
            repo_root, ["symbolic-ref", "--quiet", "--short", "HEAD"],
            _LOCAL_GIT_TIMEOUT_SECONDS,
        )
        if branch_result.returncode != 0:
            return None  # detached HEAD
        branch = branch_result.stdout.strip()

        local_result = _run_git(
            repo_root, ["rev-parse", "HEAD"], _LOCAL_GIT_TIMEOUT_SECONDS,
        )
        if local_result.returncode != 0:
            return None
        local_sha = local_result.stdout.strip()

        remote_result = _run_git(
            repo_root, ["ls-remote", "--heads", "origin", branch],
            _NETWORK_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if remote_result.returncode != 0:
        # Unreachable remote, auth required, or no such remote — the probe's
        # most likely production failure, and indistinguishable from a
        # healthy "not behind" answer to every caller unless logged here:
        # _refresh_probe's except only covers a raised exception, and this
        # branch returns normally.
        log.warning(
            "repo_staleness_notice: remote probe failed for %s (git ls-remote "
            "exit %d): %s",
            repo_root, remote_result.returncode, remote_result.stderr.strip()[:200],
        )
        emit_product_event(
            "repo_staleness_notice",
            "probe_unreachable",
            properties={
                "repo_root": str(repo_root),
                "exit_code": remote_result.returncode,
            },
        )
        return None
    lines = [line for line in remote_result.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        return None  # no such remote branch, or an ambiguous match
    remote_sha = lines[0].split()[0]

    if remote_sha == local_sha:
        return _RepoProbe(
            branch=branch, behind=False, commits_behind=None,
            remote_sha=remote_sha, checked_at=time.time(),
        )
    return _probe_ancestry(repo_root, branch, local_sha, remote_sha)


def _probe_ancestry(
    repo_root: Path, branch: str, local_sha: str, remote_sha: str,
) -> _RepoProbe | None:
    """Classify behind/ahead/diverged from local objects alone; no fetch."""
    try:
        ancestor_result = _run_git(
            repo_root, ["merge-base", "--is-ancestor", remote_sha, local_sha],
            _LOCAL_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    behind = ancestor_result.returncode != 0
    commits_behind = _commits_behind(repo_root, local_sha, remote_sha) if behind else None
    return _RepoProbe(
        branch=branch, behind=behind, commits_behind=commits_behind,
        remote_sha=remote_sha, checked_at=time.time(),
    )


def _commits_behind(repo_root: Path, local_sha: str, remote_sha: str) -> int | None:
    """Return the exact count only when the remote commit is already present locally."""
    try:
        result = _run_git(
            repo_root, ["rev-list", "--count", f"{local_sha}..{remote_sha}"],
            _LOCAL_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def _should_emit(thread_id: str | None, repo_key: str, remote_sha: str | None) -> bool:
    """Enforce the repeat rule: once per 10 minutes, again if the remote head moved."""
    key = (thread_id or "", repo_key)
    now = time.monotonic()
    with _repeat_lock:
        prior = _repeat_state.get(key)
        if prior is not None:
            last_emit, last_sha = prior
            if now - last_emit < _REPEAT_WINDOW_SECONDS and last_sha == remote_sha:
                return False
        _repeat_state[key] = (now, remote_sha)
        _prune_repeat_state(now)
    return True


def _prune_repeat_state(now: float) -> None:
    """Drop entries outside the repeat window; caller must hold ``_repeat_lock``."""
    stale = [
        k for k, (last_emit, _) in _repeat_state.items()
        if now - last_emit >= _REPEAT_WINDOW_SECONDS
    ]
    for k in stale:
        del _repeat_state[k]


def _format_age(seconds: float) -> str:
    minutes = int(max(0.0, seconds) // 60)
    return "under a minute" if minutes < 1 else f"{minutes} min"


def _commit_count_phrase(count: int) -> str:
    return "1 commit" if count == 1 else f"{count} commits"


def _render_notice(policy: RepoPolicy, probe: _RepoProbe) -> str:
    remote_ref = f"origin/{probe.branch}"
    age = _format_age(time.time() - probe.checked_at)
    if probe.commits_behind is not None:
        state = f"is {_commit_count_phrase(probe.commits_behind)} behind {remote_ref}"
    else:
        state = f"is behind {remote_ref}: the remote has commits this checkout does not"
    # A diverged checkout is classified identically to plain-behind (no fetch
    # means the two cannot be told apart), so the remedy must hold for both:
    # the fast-forward for the common case, with the worktree fallback named
    # for the one it refuses on -- a checkout with its own unpushed commits.
    return (
        f"[CORSIA repo notice] Source repo '{policy.repo_name}' "
        f"(branch {probe.branch}) {state}, as of {age} ago. What you just read "
        f"may not be current. Run `git pull --ff-only` in {policy.path}; if it "
        f"refuses because this checkout has its own commits, work in a "
        f"worktree created from {remote_ref} instead."
    )
