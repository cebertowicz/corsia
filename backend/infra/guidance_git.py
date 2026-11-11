"""The single writer of the workspace history: guidance (issue #2272) and documents (C11).

:func:`save_with_history` checks a save once (text, the person's ignore rules, the file unchanged since the
caller read it), then under one lock under ``.git/`` (every server process) writes the caller's bytes and
records them through :func:`infra.history_record.record`: outside edits at its paths first, as
``Reason: unrecorded``, then the save. Guidance and documents alike are recorded before the save answers;
guidance then sweeps other dirty guidance. A fault never fails a save, and no path writes over a file that
changed since the caller read it.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import tempfile
import time
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from loguru import logger

from core.document_history_paths import MAX_HISTORY_BYTES, is_history_text
from infra.backend_git import GIT_CONFIG_ARGS
from infra.guidance_history_paths import digest, file_version, find_commit, is_empty_git_dir, read_regular, write_bytes as _write_bytes
from infra.guidance_index_sync import CORSIA_COMMITTER, consume_records, index_lock_fault, record_failure, seed_legacy_records
from infra.guidance_messages import (
    SAVE_COMMITTED, SAVE_FAILED, SAVE_HISTORY_OFF, SAVE_UNRECORDED, commit_message, is_person_author, recorded_reason,
)
from infra.guidance_states import HistoryOff, GitFault, NoRepository, _LockBusy, history_is_off
from infra.guidance_save_after import after_save
from infra.guidance_save_types import KnownHead, SaveResult, _Commit
from infra.guidance_targets import HistoryTarget
from infra.history_ignore import ignored
from infra.skill_save_validation import refused_save

_LOCK_NAME, _LOCK_WAIT_SECONDS = "corsia-guidance.lock", 30.0
_GIT_TIMEOUT_SECONDS = 30
_GIT_CONFIG = (*GIT_CONFIG_ARGS, "-c", "commit.gpgsign=false", "-c", "safe.bareRepository=explicit")
_OUTSIDE = "Unknown editor <outside-edit@corsia.local>"
_SWEEP_LIMIT = 100
# A workspace's own repository and Git directory, by the ``.git`` entry they were read through: neither
# changes per save or open, so each is read from Git once, and again only when ``.git`` is replaced.
# A state history is off for is not kept: an enclosing repository can come or go with ``.git`` unchanged.
_REPOSITORIES: dict[tuple[Path, bool, tuple[int, int] | None], tuple[Path | None, str | None]] = {}
_GIT_DIRS: dict[tuple[Path, tuple[int, int] | None], Path] = {}


def _git_bytes(repo: Path, *args: str, env: dict[str, str] | None = None,
               data: bytes | None = None, ok: tuple[int, ...] = (0,)) -> bytes:
    """Run the one configured Git subprocess path and retain raw object bytes."""
    try:
        done = subprocess.run(
            ["git", "-C", str(repo), *_GIT_CONFIG, *args], input=data, capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS, env={**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitFault(f"git {args[0]}: {exc}") from exc
    if done.returncode not in ok:
        raise GitFault(f"git {args[0]} failed: {done.stderr.decode(errors='replace').strip()[:200] or done.returncode}")
    return done.stdout


def _git(repo: Path, *args: str, env: dict[str, str] | None = None,
         data: bytes | None = None, ok: tuple[int, ...] = (0,)) -> str:
    """String wrapper for existing guidance-history callers."""
    return _git_bytes(repo, *args, env=env, data=data, ok=ok).decode(errors="surrogateescape")


# Public digest and path helpers remain re-exported from guidance_history_paths.

def _git_entry(workspace: Path) -> tuple[int, int] | None:
    try:
        found = os.stat(workspace / ".git")
    except OSError:
        return None
    return found.st_dev, found.st_ino


def workspace_repository(workspace: Path, *, create: bool = False) -> tuple[Path | None, str | None]:
    """The workspace's own repository, never an enclosing one; a guidance save or Linux shell launch passes ``create``, where an empty ``.git`` counts as absent."""
    workspace = workspace.resolve(strict=False)
    key = (workspace, create, _git_entry(workspace))
    if (known := _REPOSITORIES.get(key)) is not None:
        return known
    found = _workspace_repository(workspace, create)
    if found[0] is not None:
        _REPOSITORIES[(workspace, create, _git_entry(workspace))] = found
    return found


def _workspace_repository(workspace: Path, create: bool) -> tuple[Path | None, str | None]:
    try:
        if (workspace / ".git").exists() and not (create and is_empty_git_dir(workspace / ".git")):
            top = Path(_git(workspace, "rev-parse", "--show-toplevel").strip()).resolve(strict=False)
            return (workspace, None) if top == workspace else (None, HistoryOff("the workspace Git directory resolves elsewhere"))
        if _git(workspace, "rev-parse", "--show-toplevel", ok=(0, 128)).strip():
            return None, HistoryOff("the workspace is inside an enclosing repository")
        if not create:
            return None, NoRepository("the workspace has no repository yet")
        _git(workspace, "init", "--quiet")
    except GitFault as exc:
        return None, str(exc)
    return workspace, None


def recorded_blob_reader(workspace: Path):
    """Return a render-owned reader for immutable recorded guidance blobs; the caller closes it."""
    from infra.guidance_blob import BatchBlobReader

    repo, _reason = workspace_repository(workspace, create=False)
    if repo is None:
        return None, "workspace guidance history unavailable"
    return BatchBlobReader.for_repo(repo, _GIT_CONFIG, _GIT_TIMEOUT_SECONDS), None


def git_dir(repo: Path) -> Path:
    """The repository's Git directory, read from Git once per ``.git`` entry."""
    key = (repo, _git_entry(repo))
    if (known := _GIT_DIRS.get(key)) is None:
        known = _GIT_DIRS[key] = Path(_git(repo, "rev-parse", "--absolute-git-dir").strip())
    return known


def _acquire(repo: Path, directory: Path | None = None) -> tuple[int, float]:
    try:
        handle = os.open((directory or git_dir(repo)) / _LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    except OSError as exc:
        raise GitFault(f"guidance lock: {exc}") from exc
    started = time.monotonic()
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle, (time.monotonic() - started) * 1000
        except BlockingIOError:
            if time.monotonic() - started >= _LOCK_WAIT_SECONDS:
                os.close(handle)
                raise _LockBusy("the guidance lock stayed busy") from None
            time.sleep(0.02)


def _release(handle: int) -> None:
    fcntl.flock(handle, fcntl.LOCK_UN)
    os.close(handle)


@contextmanager
def _locked(handle: int) -> Iterator[Any]:
    """Hold the acquired lock with one database connection; release both."""
    from infra.database import get_db

    try:
        with closing(get_db()) as db:
            yield db
    finally:
        _release(handle)


def _changed(repo: Path, specs: list[str]) -> list[str]:
    """Repo-relative paths under ``specs`` with uncommitted changes; a fault raises."""
    out = _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames", "--ignore-submodules=all", "--", *specs) if specs else ""
    return [record[3:] for record in out.split("\0") if record]


def _repair(repo: Path, shown: set[str] | None = None, git_dir: Path | None = None) -> tuple[list[str], list[str]]:
    """Repair every recorded path; report only ``shown`` ones, so a caller never learns another scope's paths."""
    try:
        return consume_records(_git, repo, shown, git_dir=git_dir)
    except Exception as exc:  # a repair fault must not fail a landed save
        logger.warning("Guidance index repair failed: {}.", exc)
        return [], ["Guidance index repair could not run; the server log has the error."]


def _commit(
    repo: Path, files: dict[str, bytes | None], message: str, author: str, *, repair_unchanged: bool = True,
    known: KnownHead | None = None,
) -> _Commit | None:
    """Commit exactly ``files`` on HEAD, or return recovery results for an unchanged tree.
    One retry is safe: every failure precedes the compare-and-swap ``update-ref``, and the retry reads
    HEAD again rather than trusting ``known``. A real-index sync fault after it is returned, not raised.

    The real-index sync is a second write HEAD does not need: a proven-stale ``index.lock`` is cleared
    first, so a stale lock can no longer fail every later save's sync."""
    for attempt in (1, 2):
        trusted = known if attempt == 1 else None
        head = trusted.head if trusted else (_git(repo, "rev-parse", "--verify", "-q", "HEAD", ok=(0, 1)).strip() or None)
        staged: list[tuple[str, tuple[str, ...]]] = []
        try:
            with tempfile.TemporaryDirectory(prefix="corsia-guidance-") as scratch:
                env = {"GIT_INDEX_FILE": str(Path(scratch) / "index")}
                _git(repo, "read-tree", head or "--empty", env=env)
                for rel, data in files.items():
                    if data is None:
                        args: tuple[str, ...] = ("--force-remove", "--", rel)
                    else:
                        blob = _git(repo, "hash-object", "-w", "--stdin", data=data).strip()
                        mode = (trusted.modes.get(rel, "100644") if trusted
                                else (_git(repo, "ls-files", "-s", "--", rel, env=env).split() or ["100644"])[0])
                        args = ("--add", "--cacheinfo", f"{mode},{blob},{rel}")
                    _git(repo, "update-index", *args, env=env)
                    staged.append((rel, args))
                tree = _git(repo, "write-tree", env=env).strip()
            if head and tree == (trusted.tree if trusted and trusted.tree else _git(repo, "rev-parse", f"{head}^{{tree}}").strip()):
                repaired, notes = _repair(repo, set(files), known and known.git_dir) if repair_unchanged else ([], [])
                return _Commit(head, changed=False, repaired=tuple(repaired), notes=tuple(notes)) if repaired or notes else None
            name, _, email = author.rpartition(" <")
            identity = {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email.rstrip(">"), "GIT_COMMITTER_NAME": "CORSIA", "GIT_COMMITTER_EMAIL": CORSIA_COMMITTER}
            sha = _git(repo, "commit-tree", tree, *(("-p", head) if head else ()), "-F", "-", data=message.encode(), env=identity).strip()
            _git(repo, "update-ref", "HEAD", sha, head or "0" * 40)
            if known is not None:
                known.head, known.tree = sha, tree
            break
        except GitFault:
            if attempt == 2:
                raise
    unsynced: list[str] = []
    git_dir = known.git_dir if known is not None else None
    lock_fault = fault = index_lock_fault(_git, repo, git_dir)
    for rel, args in staged:
        try:
            if lock_fault:
                raise GitFault(lock_fault)
            _git(repo, "update-index", *args)
        except GitFault as exc:
            unsynced.append(rel)
            fault = fault or str(exc)
            try:
                record_failure(_git, repo, rel, head, sha)
            except Exception as record_exc:  # the commit already landed
                fault += f"; could not record index recovery for {rel}: {record_exc}"
                logger.warning("{}", fault)
    if unsynced:
        logger.warning("guidance commit {} landed; index sync failed for {}: {}", sha, unsynced, fault)
    repaired, notes = _repair(repo, set(files), git_dir)
    return _Commit(sha, tuple(unsynced), fault, repaired=tuple(repaired), notes=tuple(notes))


def _sweep_others(repo: Path, workspace: Path, db, exclude: set[str]) -> list[str]:
    """Sweep up to ``_SWEEP_LIMIT`` other dirty guidance files; a fault becomes a line."""
    from core.guidance_paths import classify_guidance_path, sweep_pathspecs
    from infra.history_record import sweep

    try:
        found: list[str] = []
        left = 0
        for rel in _changed(repo, sweep_pathspecs(workspace, db)):
            if len(found) == _SWEEP_LIMIT:
                left += 1
            elif rel not in exclude and classify_guidance_path(repo / rel, workspace=workspace, db=db) and is_history_text(read_regular(repo / rel)):
                found.append(rel)
        # The sweep passes the same gate as a save: a credential-shaped file in a skill's evals is never recorded.
        if found:
            skip = ignored(repo, found)
            found = [rel for rel in found if rel not in skip]
        lines = sweep(repo, found, "guidance") if found else []
        return lines + ([f"{left} more changed guidance candidates wait for the next save."] if left else [])
    except Exception as exc:  # the save stands; the sweep failure must still be visible
        logger.opt(exception=True).warning("guidance sweep failed")
        return [f"Guidance sweep failed: {exc}. Outside edits remain on disk and not in history. Report this."]


def save_with_history(
    writes: dict[Path, bytes],
    *,
    workspace: Path,
    target: HistoryTarget,
    author: str,
    reason: str | None = None,
    evidence: str | None = None,
    message: str | None = None,
    expected: dict[Path, str | None] | None = None,
    deletes: dict[Path, str] | None = None,
    atomic: bool = False,
    binary_safe_history: bool = False,
    write: Callable[[Path, bytes], object] = _write_bytes,
    lesson_gate: bool = True,
    lesson_observation_thread_id: str | None = None,
) -> SaveResult:
    """Save writes and compare-and-swap deletes in one history commit.

    Binary-safe reviewed bundles use raw SHA-256 and a 1 MB file ceiling; ordinary guidance is text-only.
    An agent's new or changed lesson is refused unless the caller passes ``lesson_gate=False``.
    A document target is recorded the same way, with no lesson check, carry-in record or sweep of other
    guidance; its line names only a save history could not record.
    """
    lesson_gate = lesson_gate and not target.document
    lesson_changes = ()
    if lesson_gate and not is_person_author(author):
        from infra.lesson_save_gate import lesson_changes as assess_lesson_changes

        lesson_changes = assess_lesson_changes(writes, author)
    if (refused := refused_save(
        writes, author=author, lesson_gate=lesson_gate, lesson_changes=lesson_changes,
    )) is not None:
        return refused
    deletes = deletes or {}
    if set(writes) & set(deletes):
        return SaveResult("refused", "write-delete-overlap", line="A guidance save cannot write and delete the same path.")
    # A delete has no safe unrecorded equivalent: callers rely on history to recover it.
    atomic = atomic or bool(deletes)
    changes: dict[Path, bytes | None] = {**writes, **{path: None for path in deletes}}
    expected = {**(expected or {}), **deletes}
    timing: dict[str, float] = {}
    done = "" if target.document else (SAVE_COMMITTED if recorded_reason(reason) else SAVE_UNRECORDED).replace("{scope}", target.scope)
    subject = f"{target.kind} save: {', '.join(os.path.relpath(path, workspace) for path in changes)}"
    commit = message or commit_message(subject, target.scope, reason, evidence)
    result = _save(changes, workspace, target, author, commit, done, expected, atomic, binary_safe_history, write, timing)
    return after_save(
        result, workspace=workspace, writes=writes, target=target, author=author, timing=timing,
        lesson_changes=lesson_changes, lesson_observation_thread_id=lesson_observation_thread_id,
    )


def _save(
    writes: dict[Path, bytes | None], workspace: Path, target: HistoryTarget, author: str, message: str, done: str,
    expected: dict[Path, str | None] | None, atomic: bool, binary_safe_history: bool,
    write: Callable[[Path, bytes], object], timing: dict[str, float],
) -> SaveResult:
    from infra.history_record import Version, put_back, record, write_checked

    def unrecorded(outcome: str, why: str, line: str | None) -> SaveResult:
        if not atomic:
            if isinstance(stale := write_checked(writes, expected, write), SaveResult):
                return stale
        return SaveResult(outcome, why, line=line)

    if binary_safe_history and any(data is not None and len(data) > MAX_HISTORY_BYTES for data in writes.values()):
        return SaveResult("refused", "binary-too-large", line="Not saved: reviewed bundle files must be at most 1 MB.")
    if target.fault:
        line = f"Not saved: {target.fault}." if atomic else f"Saved. History may be missing: {target.fault}."
        return unrecorded("failed", target.fault, line)
    if not binary_safe_history and not all(is_history_text(data) for data in writes.values()):
        return unrecorded("ordinary", "not text up to 1 MB", None)
    repo, why = workspace_repository(workspace, create=True)
    if repo is None and not history_is_off(why):
        return unrecorded("failed", why or "", SAVE_FAILED.format(error=why))
    if repo is None:
        return unrecorded("history-off", why or "", SAVE_HISTORY_OFF.format(why=why))
    try:
        rels = {path: path.resolve(strict=False).relative_to(repo).as_posix() for path in writes}
        if ignored(repo, list(rels.values()), tray=target.registered):
            return unrecorded("history-off", "ignored", SAVE_HISTORY_OFF.format(why="the path is ignored by Git"))
        directory = git_dir(repo)
        handle, timing["lock_wait_ms"] = _acquire(repo, directory)
    except _LockBusy as exc:
        return unrecorded("deferred", "deferred-guidance-lock", SAVE_FAILED.format(error=exc))
    except (GitFault, ValueError) as exc:
        return unrecorded("failed", str(exc), SAVE_FAILED.format(error=exc))
    with _locked(handle) as db:
        if isinstance(prior := write_checked(writes, expected, write), SaveResult):
            return prior  # the edit that made it stale is recorded by the next save at its path
        started = time.monotonic()
        version = Version({rels[path]: (prior[path], data) for path, data in writes.items()}, author, message, target.kind)
        landed_all, lines, fault = record(repo, [version], directory)
        timing["commit_ms"] = (time.monotonic() - started) * 1000
        if fault is not None or not landed_all:
            if atomic:
                put_back(prior, writes)
            return SaveResult("failed", str(fault), line="\n".join([*lines, SAVE_FAILED.format(error=fault)]))
        landed = landed_all[0]
        if landed is not None:
            lines += ([done.format(sha=landed.sha[:7])] if done and landed.changed and not landed.unsynced else []) + landed.unsynced_line()
        if not target.document:
            lines += _sweep_others(repo, workspace, db, set(rels.values()))
        if landed is None or not landed.changed:
            return SaveResult("unchanged", line="\n".join(lines) or None)
        return SaveResult(
            "written", "index-unsynced" if landed.unsynced else "", sha=landed.sha,
            line="\n".join(lines), unsynced=landed.unsynced,
        )


def startup_sweep(workspace: Path) -> None:
    """Commit outside guidance edits once at server start; without a repository, do nothing."""
    repo, _why = workspace_repository(workspace)
    if repo is None:
        return
    try:
        with _locked(_acquire(repo)[0]) as db:
            try:
                seed_legacy_records(_git, repo)
            except Exception:  # the recorded repairs and the sweep still run
                logger.opt(exception=True).warning("guidance legacy index check failed")
            repaired, notes = _repair(repo)
            lines = ([f"Repaired guidance index: {', '.join(repaired)}."] if repaired else []) + notes
            lines += _sweep_others(repo, workspace, db, set())
        if lines:
            logger.info("startup guidance sweep: {}", "; ".join(lines))
    except Exception:  # startup must not fail on history
        logger.opt(exception=True).warning("startup guidance sweep failed")
