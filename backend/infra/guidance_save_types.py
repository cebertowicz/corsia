"""Value types a guidance save returns; :mod:`infra.guidance_git` owns the save itself."""

from __future__ import annotations

import shlex
from dataclasses import dataclass

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
