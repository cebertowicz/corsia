"""Value types a guidance save returns; :mod:`infra.guidance_git` owns the save itself."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path

from infra.guidance_messages import SAVE_UNSYNCED


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


@dataclass
class KnownHead:
    """What one read under the lock already holds: the Git directory, the commit to build on, its tree and
    the file modes in it, so a document record asks Git for none of them again; a commit moves it on."""

    git_dir: Path
    head: str | None
    tree: str | None
    modes: dict[str, str]


@dataclass(frozen=True)
class _Commit:
    """A landed commit and the paths whose real-index sync failed after it, with that fault."""

    sha: str
    unsynced: tuple[str, ...] = ()
    fault: str = ""
    changed: bool = True
    repaired: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def unsynced_line(self) -> list[str]:
        command = shlex.join(["git", "reset", "-q", "--", *self.unsynced])
        paths = ", ".join(self.unsynced)
        lines = [SAVE_UNSYNCED.format(sha=self.sha[:7], paths=paths, error=self.fault, command=command)] if self.unsynced else []
        if self.repaired:
            lines.append(f"Repaired guidance index: {', '.join(self.repaired)}.")
        return [*lines, *self.notes]
