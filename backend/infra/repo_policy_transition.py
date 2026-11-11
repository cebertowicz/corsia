"""Process-local admission fence for repository policy transitions.

A repository policy change on a project (register, link, create, detach, pin or
base-checkout protection) changes which paths agents may write. A tool effect or
owned process admitted under the old policy must not keep writing across that
change, so the two stay mutually exclusive — but only where they can meet.

Every agent call is already walled by ``infra.project_fence.fenced_project_ids``,
so an effect's **reach** is the set of projects its caller is not fenced from.
A transition's **claim** is the projects whose paths it changes: the project it
names, plus every project whose folder or registered repository overlaps the
repository path (a linked directory may already belong to another project, or
contain or sit inside one of its repositories). They conflict when the two share
a project.

Both sides fail closed to everywhere. A reach is everywhere for a call under a
host-access permit (which opens any path), a call with no thread, a caller that
cannot be read, or any error reading it. A claim is everywhere when the path sits
inside the workspace but in no project folder or repository — every agent can
write such a path before the change — or when the claim cannot be read. A path
outside the workspace that no project holds is writable only under a permit, so
it claims only the named project; permit holders reach everywhere anyway.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import threading

log = logging.getLogger(__name__)


class RepoPolicyTransitionBusy(RuntimeError):
    """A repository effect or policy transition cannot start safely."""


@dataclass(frozen=True, slots=True)
class Scope:
    """Projects one effect can write, or one transition changes; or everywhere."""

    projects: frozenset[str] = frozenset()
    everywhere: bool = False

    def meets(self, other: "Scope") -> bool:
        """Whether an effect with one scope and a transition with the other can meet."""
        return self.everywhere or other.everywhere or bool(self.projects & other.projects)


#: The fail-closed scope: meets every other scope.
EVERYWHERE = Scope(everywhere=True)


def caller_reach(thread_id: str | None, *, leased: bool, db=None) -> Scope:
    """The projects one caller's effect can write; :data:`EVERYWHERE` when unsure."""
    if leased or not thread_id:
        return EVERYWHERE
    try:
        from infra.database import get_db
        from infra.project_fence import fenced_project_ids, read_caller

        connection = db or get_db()
        fenced = fenced_project_ids(read_caller(thread_id, db=connection), db=connection)
        if not isinstance(fenced, frozenset):
            return EVERYWHERE
        every = frozenset(str(row["id"]) for row in connection.execute("SELECT id FROM projects"))
        return Scope(every - fenced)
    except Exception:
        log.warning("repo effect reach unreadable for %s; treating as everywhere", thread_id, exc_info=True)
        return EVERYWHERE


def _overlaps(a: Path, b: Path) -> bool:
    return a == b or a in b.parents or b in a.parents


def repo_claim(project_id: str | None, repo_path: Path | None, *, db=None, workspace: Path | None = None) -> Scope:
    """The projects a policy change on ``repo_path`` touches; :data:`EVERYWHERE` when unsure or unknown."""
    if repo_path is None:
        return EVERYWHERE
    try:
        from core.project_context import resolve_project_row_paths
        from infra.config import WORKSPACE
        from infra.database import get_db

        connection = db or get_db()
        root = Path(workspace or WORKSPACE).expanduser().resolve(strict=False)
        target = Path(os.path.normpath(repo_path.expanduser().resolve(strict=False)))
        holders: set[str] = set()
        for row in connection.execute("SELECT project_id, repo_path FROM project_repo_policies"):
            path = row["repo_path"]
            if not (isinstance(path, str) and Path(path).is_absolute()):
                return EVERYWHERE
            if _overlaps(target, Path(os.path.normpath(Path(path).resolve(strict=False)))):
                holders.add(str(row["project_id"]))
        for row in connection.execute("SELECT id, name, path FROM projects"):
            # A folder that doesn't resolve is fenced, like the path fence does, only
            # as a folder under ``projects/`` claiming no row: inside the workspace
            # and held by nobody below, which claims everywhere.
            _name, folder = resolve_project_row_paths(root, project_name=row["name"], raw_path=row["path"])
            if folder is not None and _overlaps(target, folder.resolve(strict=False)):
                holders.add(str(row["id"]))
        if not holders and (target == root or root in target.parents):
            return EVERYWHERE
        return Scope(frozenset(holders | ({project_id} if project_id else set())))
    except Exception:
        log.warning("repo policy claim unreadable for %s; treating as everywhere", repo_path, exc_info=True)
        return EVERYWHERE


class _RepoPolicyTransitionCoordinator:
    """One atomic owner for in-process repo effects and policy transitions."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active_effects: Counter[Scope] = Counter()
        self._transition: Scope | None = None

    @contextmanager
    def admit_effect(self, reach: Scope) -> Iterator[None]:
        with self._lock:
            if self._transition is not None and reach.meets(self._transition):
                raise RepoPolicyTransitionBusy(
                    "Repository tool effect is blocked by an active "
                    "policy transition."
                )
            self._active_effects[reach] += 1
        try:
            yield
        finally:
            with self._lock:
                self._active_effects[reach] -= 1
                if not self._active_effects[reach]:
                    del self._active_effects[reach]

    @contextmanager
    def transition(
        self,
        *,
        claim: Scope,
        active_process_reaches: Callable[[], Iterable[Scope]],
    ) -> Iterator[None]:
        with self._lock:
            if self._transition is not None:
                raise RepoPolicyTransitionBusy(
                    "Repository policy transition is already in progress."
                )
            if any(reach.meets(claim) for reach in self._active_effects):
                raise RepoPolicyTransitionBusy(
                    "Repository policy transition is blocked by an active "
                    "tool effect."
                )
            try:
                reaches = list(active_process_reaches())
            except Exception as exc:
                raise RepoPolicyTransitionBusy(
                    "Owned process activity could not be verified."
                ) from exc
            if not all(isinstance(reach, Scope) for reach in reaches):
                raise RepoPolicyTransitionBusy(
                    "Owned process activity could not be verified."
                )
            if any(reach.meets(claim) for reach in reaches):
                raise RepoPolicyTransitionBusy(
                    "Repository policy transition is blocked by an active "
                    "owned process."
                )
            self._transition = claim
        try:
            yield
        finally:
            with self._lock:
                self._transition = None


_COORDINATOR = _RepoPolicyTransitionCoordinator()


@contextmanager
def admit_repo_effect(reach: Scope = EVERYWHERE) -> Iterator[None]:
    """Admit one tool effect unless it can meet an active repo transition."""
    with _COORDINATOR.admit_effect(reach):
        yield


def owned_process_reaches() -> list[Scope]:
    """The reach of every owned process still outstanding, by its thread and permit."""
    from host_access.processes import PROCESS_OWNER

    return [
        caller_reach(record.thread_id, leased=record.lease_id is not None or record.permit_id is not None)
        for record in PROCESS_OWNER.outstanding_records()
    ]


@contextmanager
def repo_policy_transition(
    *,
    claim: Scope = EVERYWHERE,
    active_process_reaches: Callable[[], Iterable[Scope]] | None = None,
) -> Iterator[None]:
    """Quarantine one durable repo policy change from the effects it can meet."""
    reaches = active_process_reaches or (lambda: owned_process_reaches())
    with _COORDINATOR.transition(claim=claim, active_process_reaches=reaches):
        yield
