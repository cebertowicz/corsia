"""Process-local admission fence for repository policy transitions.

A repository policy change on a project (register, link, create, detach, pin or
base-checkout protection) changes which paths agents may write. A tool effect or
owned process admitted under the old policy must not keep writing across that
change, so the two stay mutually exclusive — but only where they can meet.

An agent's own calls are walled by ``infra.project_fence.fenced_project_ids``, so
an effect's **reach** is every project except those its caller is fenced from. It
is held as that fenced set, so a project created after admission counts as
reachable. A transition's **claim** is the projects whose paths it changes: the
project it names, plus every project whose folder or registered repository
overlaps the repository path (a linked directory may already belong to another
project, or contain or sit inside one of its repositories). They conflict when
the claim names a project the reach is not fenced from.

Both sides fail closed. A reach is unconfined, meeting every claim, for a call
the caller's fence does not confine (a host-access permit, which opens any path, or a
connected app, whose server has no caller and is fenced only from Local only
projects), a call with no thread, a caller that cannot be read, or any error
reading it. A claim is everywhere when the path sits inside the workspace and no
project folder or repository contains it — every agent can write some of it
before the change — or when the claim cannot be read. A path outside the
workspace that no project holds is writable only under a permit, so it claims
only the projects holding part of it; permit holders reach everything anyway.

Effects and transitions meet under one lock. Owned processes are read after the
transition is published and outside the lock, so tool admission never waits on
those reads: a process starts only inside an admitted effect, and once the
transition is published an effect that meets it is refused. The claim is read
again at that point too: only one transition runs at a time, so holders added
between the first read and publication show up there, and the transition is
refused rather than run under a claim that leaves them out.
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
class Claim:
    """The projects one policy change touches, or everywhere."""

    projects: frozenset[str] = frozenset()
    everywhere: bool = False

    def covers(self, other: Claim) -> bool:
        """Whether every project ``other`` touches is already claimed here."""
        return self.everywhere or (not other.everywhere and other.projects <= self.projects)


#: The fail-closed claim: meets every reach.
EVERYWHERE = Claim(everywhere=True)


@dataclass(frozen=True, slots=True)
class Reach:
    """The projects one effect can write: every project but ``fenced``, or any path at all."""

    fenced: frozenset[str] = frozenset()
    unconfined: bool = False

    def meets(self, claim: Claim) -> bool:
        """Whether this effect can write a path the claimed change touches."""
        return self.unconfined or claim.everywhere or bool(claim.projects - self.fenced)


#: The fail-closed reach: it can write any path, so it meets every claim.
UNCONFINED = Reach(unconfined=True)


def caller_reach(thread_id: str | None, *, unconfined: bool, db=None) -> Reach:
    """The projects one caller's effect can write; :data:`UNCONFINED` when unsure.

    ``unconfined`` marks a call its caller's project fence does not confine.
    """
    if unconfined or not thread_id:
        return UNCONFINED
    try:
        from infra.database import get_db
        from infra.project_fence import fenced_project_ids, read_caller

        connection = db or get_db()
        fenced = fenced_project_ids(read_caller(thread_id, db=connection), db=connection)
        return Reach(fenced) if isinstance(fenced, frozenset) else UNCONFINED
    except Exception:
        log.warning("repo effect reach unreadable for %s; treating as unconfined", thread_id, exc_info=True)
        return UNCONFINED


def _overlaps(a: Path, b: Path) -> bool:
    return a == b or a in b.parents or b in a.parents


def repo_claim(project_id: str | None, repo_path: Path | None, *, db=None, workspace: Path | None = None) -> Claim:
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
        contained = False

        def hold(holder: str, held: Path) -> None:
            nonlocal contained
            if _overlaps(target, held):
                holders.add(holder)
                contained = contained or held == target or held in target.parents

        for row in connection.execute("SELECT project_id, repo_path FROM project_repo_policies"):
            path = row["repo_path"]
            if not (isinstance(path, str) and Path(path).is_absolute()):
                return EVERYWHERE
            hold(str(row["project_id"]), Path(os.path.normpath(Path(path).resolve(strict=False))))
        for row in connection.execute("SELECT id, name, path FROM projects"):
            # A folder that doesn't resolve is fenced, like the path fence does, only
            # as a folder under ``projects/`` claiming no row: inside the workspace
            # and contained by nobody below, which claims everywhere.
            _name, folder = resolve_project_row_paths(root, project_name=row["name"], raw_path=row["path"])
            if folder is not None:
                hold(str(row["id"]), folder.resolve(strict=False))
        if not contained and (target == root or root in target.parents):
            return EVERYWHERE
        return Claim(frozenset(holders | ({project_id} if project_id else set())))
    except Exception:
        log.warning("repo policy claim unreadable for %s; treating as everywhere", repo_path, exc_info=True)
        return EVERYWHERE


class _RepoPolicyTransitionCoordinator:
    """One atomic owner for in-process repo effects and policy transitions."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active_effects: Counter[Reach] = Counter()
        self._transition: Claim | None = None

    @contextmanager
    def admit_effect(self, reach: Reach) -> Iterator[None]:
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
        read_claim: Callable[[], Claim],
        active_process_reaches: Callable[[], Iterable[Reach]],
    ) -> Iterator[None]:
        claim = read_claim()
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
            self._transition = claim
        try:
            if not claim.covers(read_claim()):
                raise RepoPolicyTransitionBusy(
                    "Repository holders changed while the policy transition "
                    "started; try again."
                )
            try:
                reaches = list(active_process_reaches())
            except Exception as exc:
                raise RepoPolicyTransitionBusy(
                    "Owned process activity could not be verified."
                ) from exc
            if not all(isinstance(reach, Reach) for reach in reaches):
                raise RepoPolicyTransitionBusy(
                    "Owned process activity could not be verified."
                )
            if any(reach.meets(claim) for reach in reaches):
                raise RepoPolicyTransitionBusy(
                    "Repository policy transition is blocked by an active "
                    "owned process."
                )
            yield
        finally:
            with self._lock:
                self._transition = None


_COORDINATOR = _RepoPolicyTransitionCoordinator()


@contextmanager
def admit_repo_effect(reach: Reach = UNCONFINED) -> Iterator[None]:
    """Admit one tool effect unless it can meet an active repo transition."""
    with _COORDINATOR.admit_effect(reach):
        yield


def owned_process_reaches() -> list[Reach]:
    """The reach of every owned process still outstanding, read once per thread and permit."""
    from host_access.processes import PROCESS_OWNER

    callers = {
        (record.thread_id, record.lease_id is not None or record.permit_id is not None)
        for record in PROCESS_OWNER.outstanding_records()
    }
    return [caller_reach(thread_id, unconfined=unconfined) for thread_id, unconfined in callers]


@contextmanager
def repo_policy_transition(
    *,
    read_claim: Callable[[], Claim] = lambda: EVERYWHERE,
    active_process_reaches: Callable[[], Iterable[Reach]] | None = None,
) -> Iterator[None]:
    """Quarantine one durable repo policy change from the effects it can meet.

    ``read_claim`` is called before publication and once after it.
    """
    reaches = active_process_reaches or (lambda: owned_process_reaches())
    with _COORDINATOR.transition(read_claim=read_claim, active_process_reaches=reaches):
        yield
