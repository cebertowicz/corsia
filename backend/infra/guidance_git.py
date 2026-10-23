"""The single owner of CORSIA guidance history (issue #2272).

Under one lock under ``.git/`` (every server process), :func:`save_guidance` commits
outside edits at its paths as ``Reason: unrecorded``, writes the caller's bytes and
commits those in-memory bytes, then sweeps other dirty guidance. A fault never fails a save,
and no path writes over a file that changed since the caller read it.
"""

from __future__ import annotations

import fcntl
import os
import shlex
import stat
import subprocess
import tempfile
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from loguru import logger

from infra.guidance_cas import stale_path
from infra.guidance_history_paths import digest, file_version, find_commit
from infra.guidance_messages import (
    SAVE_COMMITTED, SAVE_FAILED, SAVE_HISTORY_OFF, SAVE_STALE, SAVE_UNRECORDED, SAVE_UNSYNCED,
    commit_message, recorded_reason,
)
from infra.guidance_targets import GuidanceTarget
from infra.skill_save_validation import refused_save

_LOCK_NAME, _LOCK_WAIT_SECONDS = "corsia-guidance.lock", 30.0
_GIT_TIMEOUT_SECONDS = 30
_GIT_CONFIG = ("-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false")
_OUTSIDE = "Unknown editor <outside-edit@corsia.local>"
MAX_GUIDANCE_BYTES = 1024 * 1024
_SWEEP_LIMIT = 100


class GitFault(RuntimeError):
    """A Git or OS failure; callers surface it and never read it as clean."""


class _LockBusy(GitFault):
    pass



@dataclass(frozen=True)
class SaveResult:
    """``outcome``: written, unchanged, failed, deferred, stale, history-off, ordinary or refused; ``sha`` is full.
    ``unsynced`` names paths whose commit landed while the real index kept the old blob (reason
    ``index-unsynced``): a partial success the caller must show, never a plain "committed". ``refused``
    (reason ``invalid-skill``) wrote nothing; ``errors`` holds the validation errors and ``line`` the fix."""

    outcome: str
    reason: str = ""
    sha: str | None = None
    line: str | None = None
    unsynced: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Commit:
    """A landed commit and the paths whose real-index sync failed after it, with that fault."""

    sha: str
    unsynced: tuple[str, ...] = ()
    fault: str = ""

    def unsynced_line(self) -> list[str]:
        command = shlex.join(["git", "reset", "-q", "--", *self.unsynced])
        paths = ", ".join(self.unsynced)
        return [SAVE_UNSYNCED.format(sha=self.sha[:7], paths=paths, error=self.fault, command=command)] if self.unsynced else []


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

def workspace_repository(workspace: Path, *, create: bool = False) -> tuple[Path | None, str | None]:
    """The workspace's own repository, never an enclosing one; only a guidance save passes ``create``."""
    workspace = workspace.resolve(strict=False)
    try:
        if (workspace / ".git").exists():
            top = Path(_git(workspace, "rev-parse", "--show-toplevel").strip()).resolve(strict=False)
            return (workspace, None) if top == workspace else (None, "the workspace Git directory resolves elsewhere")
        if _git(workspace, "rev-parse", "--show-toplevel", ok=(0, 128)).strip():
            return None, "the workspace is inside an enclosing repository"
        if not create:
            return None, "the workspace has no repository yet"
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


def _acquire(repo: Path) -> tuple[int, float]:
    try:
        handle = os.open(Path(_git(repo, "rev-parse", "--absolute-git-dir").strip()) / _LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
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


@contextmanager
def _locked(handle: int) -> Iterator[Any]:
    """Hold the acquired lock with one database connection; release both."""
    from infra.database import get_db

    try:
        with closing(get_db()) as db:
            yield db
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        os.close(handle)


def _changed(repo: Path, specs: list[str]) -> list[str]:
    """Repo-relative paths under ``specs`` with uncommitted changes; a fault raises."""
    out = _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames", "--", *specs) if specs else ""
    return [record[3:] for record in out.split("\0") if record]


def _commit(repo: Path, files: dict[str, bytes | None], message: str, author: str) -> _Commit | None:
    """Commit exactly ``files`` on HEAD, or None when nothing changed. One retry is safe: every failure
    precedes the compare-and-swap ``update-ref``. A real-index sync fault after it is returned, not raised."""
    for attempt in (1, 2):
        head = _git(repo, "rev-parse", "--verify", "-q", "HEAD", ok=(0, 1)).strip() or None
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
                        mode = (_git(repo, "ls-files", "-s", "--", rel, env=env).split() or ["100644"])[0]
                        args = ("--add", "--cacheinfo", f"{mode},{blob},{rel}")
                    _git(repo, "update-index", *args, env=env)
                    staged.append((rel, args))
                tree = _git(repo, "write-tree", env=env).strip()
            if head and tree == _git(repo, "rev-parse", f"{head}^{{tree}}").strip():
                return None
            name, _, email = author.rpartition(" <")
            identity = {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email.rstrip(">"), "GIT_COMMITTER_NAME": "CORSIA", "GIT_COMMITTER_EMAIL": "corsia@localhost"}
            sha = _git(repo, "commit-tree", tree, *(("-p", head) if head else ()), "-F", "-", data=message.encode(), env=identity).strip()
            _git(repo, "update-ref", "HEAD", sha, head or "0" * 40)
            break
        except GitFault:
            if attempt == 2:
                raise
    unsynced: list[str] = []
    fault = ""
    for rel, args in staged:
        try:
            _git(repo, "update-index", *args)
        except GitFault as exc:
            unsynced.append(rel)
            fault = fault or str(exc)
    if unsynced:
        logger.warning("guidance commit {} landed; index sync failed for {}: {}", sha, unsynced, fault)
    return _Commit(sha, tuple(unsynced), fault)


def _is_text(data: bytes | None) -> bool:
    return data is None or (len(data) <= MAX_GUIDANCE_BYTES and b"\0" not in data[:8192])


def _read(path: Path) -> bytes | None:
    """A regular file's bytes, or None when absent; a symlink or other file type raises."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise OSError(f"{path} is not a regular file")
        return handle.read()


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _sweep(repo: Path, rels: list[str]) -> list[str]:
    """Commit outside edits at ``rels`` from disk in one commit by an unknown editor: nothing
    records who made them, and the last CORSIA writer did not write these bytes."""
    names = sorted(rels)
    shown = ", ".join(names[:12]) + (f" and {len(names) - 12} more" if len(names) > 12 else "")
    files = {rel: _read(repo / rel) for rel in names}
    done = _commit(repo, files, f"guidance sweep: {shown}\n\nReason: unrecorded\nScope: sweep", _OUTSIDE)
    if done is None:
        return []
    return [f"Swept and committed {shown} as {done.sha[:7]} with Reason: unrecorded.", *done.unsynced_line()]


def _sweep_others(repo: Path, workspace: Path, db, exclude: set[str]) -> list[str]:
    """Sweep up to ``_SWEEP_LIMIT`` other dirty guidance files; a fault becomes a line."""
    from core.guidance_paths import classify_guidance_path, sweep_pathspecs

    try:
        found: list[str] = []
        left = 0
        for rel in _changed(repo, sweep_pathspecs(workspace, db)):
            if len(found) == _SWEEP_LIMIT:
                left += 1
            elif rel not in exclude and classify_guidance_path(repo / rel, workspace=workspace, db=db) and _is_text(_read(repo / rel)):
                found.append(rel)
        lines = _sweep(repo, found) if found else []
        return lines + ([f"{left} more changed guidance candidates wait for the next save."] if left else [])
    except Exception as exc:  # the save stands; the sweep failure must still be visible
        logger.opt(exception=True).warning("guidance sweep failed")
        return [f"Guidance sweep failed: {exc}. Outside edits remain on disk and not in history. Report this."]



def save_guidance(
    writes: dict[Path, bytes],
    *,
    workspace: Path,
    target: GuidanceTarget,
    author: str,
    reason: str | None = None,
    evidence: str | None = None,
    message: str | None = None,
    expected: dict[Path, str | None] | None = None,
    deletes: dict[Path, str] | None = None,
    atomic: bool = False,
    binary_safe_history: bool = False,
    write: Callable[[Path, bytes], object] = _write_bytes,
) -> SaveResult:
    """Save writes and compare-and-swap deletes in one guidance-history commit.

    Binary-safe reviewed bundles use raw SHA-256 and a 1 MB file ceiling; ordinary guidance is text-only.
    """
    if (refused := refused_save(writes)) is not None:
        return refused
    deletes = deletes or {}
    if set(writes) & set(deletes):
        return SaveResult("refused", "write-delete-overlap", line="A guidance save cannot write and delete the same path.")
    # A delete has no safe unrecorded equivalent: callers rely on history to recover it.
    atomic = atomic or bool(deletes)
    changes: dict[Path, bytes | None] = {**writes, **{path: None for path in deletes}}
    expected = {**(expected or {}), **deletes}
    timing: dict[str, float] = {}
    done = (SAVE_COMMITTED if recorded_reason(reason) else SAVE_UNRECORDED).replace("{scope}", target.scope)
    subject = f"guidance save: {', '.join(os.path.relpath(path, workspace) for path in changes)}"
    commit = message or commit_message(subject, target.scope, reason, evidence)
    result = _save(
        changes, workspace, target, author, commit, done, expected, atomic,
        binary_safe_history, write, timing,
    )
    from infra.product_events import emit_product_event

    properties = {
        "outcome": result.outcome, "scope": target.scope, "history_off": result.outcome == "history-off",
        "index_unsynced": list(result.unsynced), **timing,
    }
    logger.info("guidance save {}", properties)
    emit_product_event("guidance_history", "save", properties=properties)
    return result


def _stale(
    expected: dict[Path, str | None] | None, *, binary_safe_history: bool = False,
) -> SaveResult | None:
    """Return a stale result when the destination changed after the caller read it."""
    path = stale_path(expected, binary_safe_history=binary_safe_history, read_bytes=_read)
    return SaveResult("stale", "deferred-destination-changed", line=SAVE_STALE.format(path=path)) if path else None


def _save(
    writes: dict[Path, bytes | None], workspace: Path, target: GuidanceTarget, author: str, message: str, done: str,
    expected: dict[Path, str | None] | None, atomic: bool, binary_safe_history: bool,
    write: Callable[[Path, bytes], object], timing: dict[str, float],
) -> SaveResult:
    def unrecorded(outcome: str, why: str, line: str | None) -> SaveResult:
        if not atomic:
            if (stale := _stale(expected, binary_safe_history=binary_safe_history)) is not None:
                return stale
            for path, data in writes.items():
                if data is None:
                    path.unlink(missing_ok=True)
                else:
                    write(path, data)
        return SaveResult(outcome, why, line=line)

    if binary_safe_history and any(data is not None and len(data) > MAX_GUIDANCE_BYTES for data in writes.values()):
        return SaveResult("refused", "binary-too-large", line="Not saved: reviewed bundle files must be at most 1 MB.")
    if target.fault:
        line = f"Not saved: {target.fault}." if atomic else f"Saved. History may be missing: {target.fault}."
        return unrecorded("failed", target.fault, line)
    if not binary_safe_history and not all(_is_text(data) for data in writes.values()):
        return unrecorded("ordinary", "not text up to 1 MB", None)
    repo, why = workspace_repository(workspace, create=True)
    if repo is None:
        return unrecorded("history-off", why or "", SAVE_HISTORY_OFF.format(why=why))
    try:
        rels = {path: path.resolve(strict=False).relative_to(repo).as_posix() for path in writes}
        if _git(repo, "check-ignore", "--", *rels.values(), ok=(0, 1)).strip():
            return unrecorded("history-off", "ignored", SAVE_HISTORY_OFF.format(why="the path is ignored by Git"))
        handle, timing["lock_wait_ms"] = _acquire(repo)
    except _LockBusy as exc:
        return unrecorded("deferred", "deferred-guidance-lock", SAVE_FAILED.format(error=exc))
    except (GitFault, ValueError) as exc:
        return unrecorded("failed", str(exc), SAVE_FAILED.format(error=exc))
    with _locked(handle) as db:
        try:
            outside = _changed(repo, [f":(literal){rel}" for rel in rels.values()])
            lines = _sweep(repo, outside) if outside else []
        except Exception as exc:  # an unrecorded outside edit must never join the saver's commit
            return unrecorded("failed", str(exc), SAVE_FAILED.format(error=f"outside edits could not be recorded first ({exc})"))
        if (stale := _stale(expected, binary_safe_history=binary_safe_history)) is not None:
            return stale
        prior = {path: _read(path) for path in writes} if atomic else {}
        for path, data in writes.items():
            if data is None:
                path.unlink(missing_ok=True)
            else:
                write(path, data)
        started = time.monotonic()
        try:
            landed = _commit(repo, {rels[path]: data for path, data in writes.items()}, message, author)
        except GitFault as exc:
            for path, old in prior.items():  # HEAD never moved, so restoring prior bytes is safe
                if _read(path) == writes[path]:
                    _write_bytes(path, old) if old is not None else path.unlink(missing_ok=True)
            return SaveResult("failed", str(exc), line="\n".join([*lines, SAVE_FAILED.format(error=exc)]))
        finally:
            timing["commit_ms"] = (time.monotonic() - started) * 1000
        if landed is not None:
            lines += landed.unsynced_line() or [done.format(sha=landed.sha[:7])]
        lines += _sweep_others(repo, workspace, db, set(rels.values()))
        if landed is None:
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
            lines = _sweep_others(repo, workspace, db, set())
        if lines:
            logger.info("startup guidance sweep: {}", "; ".join(lines))
    except Exception:  # startup must not fail on history
        logger.opt(exception=True).warning("startup guidance sweep failed")
