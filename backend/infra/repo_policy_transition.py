"""Process-local admission fence for repository policy transitions.

One coordinator, one lock. Every tool effect that can write and every owned
process carries a :class:`Reach`: the exact paths a file-writing call writes, or
else the projects its caller was fenced from when the effect was admitted, or
everywhere. A transition carries a claim: the projects whose write rules it
changes, or everywhere, and the folders it changes when it knows them. They
conflict when the reach meets the claim. A call that only reads is no effect.

A registration that meets an in-flight effect or a bounded owned process waits for
it (:func:`wait_for_transition`), up to :data:`TRANSITION_WAIT_SECONDS`, then
proceeds; it is refused only once that bound passes. A long-running process has no
bound a wait could outlast, so meeting one is refused at once, naming it.

Only a registration whose target sits inside the registering project's own
folder claims less than everywhere (``api.project_repo_claim``): every caller not
reaching that project is already walled from that folder, and the workspace is
already writable, so the registration adds no writable path for anyone outside
it. Every other transition claims everywhere, which excludes every effect and
every owned process, as before.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import logging
from pathlib import Path
import threading
import time
from typing import TypeVar

from loguru import logger

log = logging.getLogger(__name__)

_T = TypeVar("_T")

#: How long a registration waits for what it meets: the longest shell call, foreground
#: or background (a long-running process is never waited on).
TRANSITION_WAIT_SECONDS = 600.0
_FIRST_POLL_SECONDS = 0.25
_MAX_POLL_SECONDS = 2.0


class RepoPolicyTransitionBusy(RuntimeError):
    """A repository effect or policy transition cannot start safely.

    ``retryable`` marks a conflict that ends when what is in flight finishes;
    ``running`` is what is still running, as a clause ("agent "X" is running an action").
    """

    def __init__(
        self, message: str, *, retryable: bool = False, running: str | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.running = running


def _overlaps(paths: tuple[Path, ...], folders: tuple[Path, ...]) -> bool:
    from tools.host_path_resolution import contains

    return any(contains(a, b) or contains(b, a) for a in paths for b in folders)


@dataclass(frozen=True, slots=True)
class Reach:
    """What one effect or owned process could write.

    ``paths``, when set, are the only paths the call writes (a file tool's target, as
    written and resolved). Otherwise it could write everything outside ``fenced``, the
    set of projects the caller was walled from when its call was admitted; ``None``
    means everywhere. A project created after that read is in no snapshot, so it
    counts as reachable, which fails closed.
    """

    fenced: frozenset[str] | None = None
    paths: tuple[Path, ...] | None = None

    def meets(
        self, claim: frozenset[str] | None, folders: tuple[Path, ...] | None = None,
    ) -> bool:
        """Whether this reach can touch anything the claim changes.

        ``claim`` is the projects (``None``: everywhere); ``folders`` the folders the
        transition changes, when known (``None``: not known).
        """
        if self.paths is not None:
            return folders is None or _overlaps(self.paths, folders)
        if self.fenced is None or claim is None:
            return True
        return any(project_id not in self.fenced for project_id in claim)


#: The reach of a caller that is walled from nothing, or that could not be read.
EVERYWHERE = Reach()


def _scope(projects: frozenset[str] | None) -> str:
    """A claim or fenced set as one log token: ``everywhere`` or its sorted project ids."""
    return "everywhere" if projects is None else ",".join(sorted(projects)) or "none"


def _reach_token(reach: Reach) -> str:
    """A blocker's reach as one log token: the paths it writes, or the projects it is walled from."""
    if reach.paths is not None:
        return "writes:" + ",".join(str(path) for path in reach.paths)
    # ``everywhere`` would misread here: a reach fenced from nothing reaches all.
    return "walled_from:" + ("nothing" if reach.fenced is None else _scope(reach.fenced))


@dataclass(frozen=True, slots=True)
class Holder:
    """One in-flight effect or live owned process, with what the refusal names.

    ``what`` is what its starter is running, as the refusal reads it ("an action").
    ``waitable`` is false for a holder with no bound a wait could outlast (a
    long-running process).
    """

    reach: Reach
    thread_id: str | None
    what: str
    waitable: bool = True


# The reach of the tool effect running in this task, so an owned process spawned by
# that call keeps the reach of the call that launched it.
_EFFECT_REACH: ContextVar[Reach | None] = ContextVar("repo_effect_reach", default=None)


def current_effect_reach() -> Reach | None:
    """The reach of the admitted effect this task runs inside, or ``None`` outside one."""
    return _EFFECT_REACH.get()


def caller_reach(thread_id: str | None, *, db) -> Reach:
    """The reach of a caller, read once: one read of the caller and its fenced projects.

    No thread (the person's own session), a caller walled from nothing, and a caller
    that cannot be read all reach everywhere.
    """
    from infra.project_fence import ALL, fenced_project_ids, read_caller

    if not thread_id:
        return EVERYWHERE
    try:
        fenced = fenced_project_ids(read_caller(thread_id, db=db), db=db)
    except Exception:
        log.exception("repo effect reach read failed for thread=%s", thread_id)
        return EVERYWHERE
    if fenced is ALL or not isinstance(fenced, frozenset) or not fenced:
        return EVERYWHERE
    return Reach(fenced)


def _agent_label(thread_id: str) -> str:
    """The blocking agent by thread title, without naming a Local only project's thread."""
    from infra.database import get_db
    from llm.locality import cloud_off_project_ids

    try:
        db = get_db()
        try:
            row = db.execute(
                "SELECT title, project_id FROM threads WHERE id = ?", (thread_id,)
            ).fetchone()
            if row is None:
                return "an agent that no longer exists"
            if row["project_id"] is not None and row["project_id"] in cloud_off_project_ids(db=db):
                return "an agent in a Local only project"
            return f'agent "{row["title"]}"' if row["title"] else "an untitled agent"
        finally:
            db.close()
    except Exception:
        log.exception("repo transition blocker label read failed")
        return "an agent"


def _busy(fallback: str, holder: Holder | None, *, retryable: bool) -> RepoPolicyTransitionBusy:
    """The refusal a person or agent reads: who is still running what, and what to do.

    No thread is the person's own session, so the refusal names them as "you".
    """
    if holder is None:
        running = f"{fallback} is running"
    elif holder.thread_id:
        running = f"{_agent_label(holder.thread_id)} is running {holder.what}"
    else:
        running = f"you're running {holder.what}"
    return RepoPolicyTransitionBusy(
        f"Repositories can't change while {running}. Try again once it finishes.",
        retryable=retryable,
        running=running,
    )


def _duration(seconds: float) -> str:
    """A wait bound as a person says it: whole minutes, or seconds under a minute."""
    if seconds < 60:
        count, unit = max(1, round(seconds)), "second"
    else:
        count, unit = round(seconds / 60), "minute"
    return f"{count} {unit}{'' if count == 1 else 's'}"


class _RepoPolicyTransitionCoordinator:
    """One atomic owner for in-process repo effects and policy transitions."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._effects: dict[int, Holder] = {}
        self._next_effect = 0
        self._transition_active = False
        # ``None`` while the active transition claims everywhere.
        self._claim: frozenset[str] | None = None
        # The folders the active transition changes; ``None`` when not known.
        self._folders: tuple[Path, ...] | None = None
        # Bumped as each transition ends, so a claim read before one can be checked.
        self._generation = 0

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @contextmanager
    def admit_effect(self, reach: Reach, holder: Holder) -> Iterator[None]:
        with self._lock:
            if self._transition_active and reach.meets(self._claim, self._folders):
                raise RepoPolicyTransitionBusy(
                    "Repository tool effect is blocked by an active "
                    "policy transition."
                )
            key = self._next_effect
            self._next_effect += 1
            self._effects[key] = holder
        token = _EFFECT_REACH.set(reach)
        try:
            yield
        finally:
            _EFFECT_REACH.reset(token)
            with self._lock:
                del self._effects[key]

    def _process_blocker(
        self,
        claim: frozenset[str] | None,
        folders: tuple[Path, ...] | None,
        active_process_count: Callable[[], int],
        owned_processes: Callable[[], Iterable[Holder]] | None,
    ) -> tuple[bool, Holder | None, bool]:
        """Whether a live owned process meets the claim, which one to name, and
        whether every one that meets it can be waited out."""
        unverified = RepoPolicyTransitionBusy("Owned process activity could not be verified.")
        holders: tuple[Holder, ...] | None = None
        if owned_processes is not None:
            try:
                holders = tuple(owned_processes())
            except Exception as exc:
                raise unverified from exc
            if any(not isinstance(holder, Holder) for holder in holders):
                raise unverified
        if claim is not None:
            if holders is None:
                raise unverified
            meeting = [holder for holder in holders if holder.reach.meets(claim, folders)]
            stuck = [holder for holder in meeting if not holder.waitable]
            return bool(meeting), next(iter(stuck or meeting), None), not stuck
        try:
            process_count = active_process_count()
        except Exception as exc:
            raise unverified from exc
        if (
            isinstance(process_count, bool)
            or not isinstance(process_count, int)
            or process_count < 0
        ):
            raise unverified
        if not process_count:
            return False, None, True
        # Unless every counted process has its holder, none is known to end within the wait.
        stuck = [holder for holder in holders or () if not holder.waitable]
        waitable = holders is not None and len(holders) >= process_count and not stuck
        return True, next(iter(stuck or holders or ()), None), waitable

    @contextmanager
    def transition(
        self,
        *,
        active_process_count: Callable[[], int],
        owned_processes: Callable[[], Iterable[Holder]] | None = None,
        claim: frozenset[str] | None = None,
        claim_generation: int | None = None,
        folders: tuple[Path, ...] | None = None,
    ) -> Iterator[frozenset[str] | None]:
        blocker: RepoPolicyTransitionBusy | None = None
        with self._lock:
            if self._transition_active:
                raise RepoPolicyTransitionBusy(
                    "Repository policy transition is already in progress.", retryable=True,
                    running="another repository change is in progress",
                )
            if claim is not None and claim_generation != self._generation:
                # Another transition ended after the claim was read: claim everywhere.
                logger.info("repo policy transition claim {} widened to everywhere: "
                            "another transition ended after it was read", _scope(claim))
                claim = None
            effects = [h for h in self._effects.values() if h.reach.meets(claim, folders)]
            # An in-flight tool call always ends within its own time limit.
            waitable = True
            if effects:
                blocker_holder: Holder | None = effects[0]
                running = "an agent's action"
            else:
                busy, blocker_holder, waitable = self._process_blocker(
                    claim, folders, active_process_count, owned_processes,
                )
                running = "an agent's process" if busy else ""
            if not running:
                self._transition_active = True
                self._claim = claim
                self._folders = folders
        # The claim each transition runs or is refused under, and the blocker's reach,
        # through loguru so production log reads see them (stdlib INFO is not captured).
        if running:
            logger.info(
                "repo policy transition refused claim={} blocker_thread={} blocker_reach={}",
                _scope(claim),
                blocker_holder.thread_id if blocker_holder else None,
                _reach_token(blocker_holder.reach) if blocker_holder else None,
            )
            # Named outside the lock: the label reads the database.
            blocker = _busy(running, blocker_holder, retryable=waitable)
            raise blocker
        logger.info("repo policy transition admitted claim={}", _scope(claim))
        try:
            yield claim
        finally:
            with self._lock:
                self._transition_active = False
                self._claim = None
                self._folders = None
                self._generation += 1


_COORDINATOR = _RepoPolicyTransitionCoordinator()


def transition_generation() -> int:
    """The coordinator's transition count; pass it with a claim read before the transition."""
    return _COORDINATOR.generation


@contextmanager
def admit_repo_effect(
    reach: Reach = EVERYWHERE,
    *,
    thread_id: str | None = None,
) -> Iterator[None]:
    """Admit one tool effect unless it conflicts with a repo transition."""
    holder = Holder(reach=reach, thread_id=thread_id, what="an action")
    with _COORDINATOR.admit_effect(reach, holder):
        yield


@contextmanager
def repo_policy_transition(
    *,
    active_process_count: Callable[[], int],
    owned_processes: Callable[[], Iterable[Holder]] | None = None,
    claim: frozenset[str] | None = None,
    claim_generation: int | None = None,
    folders: tuple[Path, ...] | None = None,
) -> Iterator[frozenset[str] | None]:
    """Quarantine durable repo policy changes from the effects and processes they meet.

    ``claim`` is ``None`` (everywhere) unless the caller read a narrower one, with the
    :func:`transition_generation` it read it at; a narrowed claim also needs
    ``owned_processes`` so each live process is judged by its own reach. ``folders``
    are the folders whose write rules change, when the caller knows them; a call that
    writes known paths meets the transition only if they overlap one, and the caller
    must confirm inside the transition that what it changes is still inside them.
    Yields the claim the transition was admitted under (``None`` once widened to
    everywhere), so the caller can check what it resolves inside the transition.
    """
    with _COORDINATOR.transition(
        active_process_count=active_process_count,
        owned_processes=owned_processes,
        claim=claim,
        claim_generation=claim_generation,
        folders=folders,
    ) as admitted:
        yield admitted


async def wait_for_transition(
    attempt: Callable[[], Awaitable[_T]],
    *,
    wait_seconds: float | None = None,
) -> _T:
    """Run ``attempt`` until no in-flight effect or process it meets is left.

    Each attempt enters its transition afresh, so it re-reads its claim. Between
    attempts it sleeps on the event loop, holding no thread or lock. A conflict still
    there after ``wait_seconds`` (default :data:`TRANSITION_WAIT_SECONDS`) is refused,
    naming what is still running; any other refusal is raised at once.
    """
    limit = TRANSITION_WAIT_SECONDS if wait_seconds is None else wait_seconds
    deadline = time.monotonic() + limit
    poll = _FIRST_POLL_SECONDS
    while True:
        try:
            return await attempt()
        except RepoPolicyTransitionBusy as exc:
            remaining = deadline - time.monotonic()
            if not exc.retryable:
                raise
            if remaining <= 0:
                running = exc.running or "another repository change is in progress"
                raise RepoPolicyTransitionBusy(
                    f"Repositories can't change while {running}. It was still going after "
                    f"{_duration(limit)}; try again once it finishes.",
                    running=running,
                ) from exc
            await asyncio.sleep(min(poll, remaining))
            poll = min(poll * 2, _MAX_POLL_SECONDS)
