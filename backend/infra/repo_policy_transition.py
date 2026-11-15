"""Process-local admission fence for repository policy transitions.

One coordinator, one lock. Every tool effect and every owned process carries a
:class:`Reach`: the projects its caller was fenced from when the effect was
admitted, or everywhere. A transition carries a claim: the projects whose write
rules it changes, or everywhere. They conflict when the reach meets the claim.

Only a registration whose target sits inside the registering project's own
folder claims less than everywhere (``api.project_repo_claim``): every caller not
reaching that project is already walled from that folder, and the workspace is
already writable, so the registration adds no writable path for anyone outside
it. Every other transition claims everywhere, which excludes every effect and
every owned process, as before.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import logging
import threading

log = logging.getLogger(__name__)


class RepoPolicyTransitionBusy(RuntimeError):
    """A repository effect or policy transition cannot start safely."""


@dataclass(frozen=True, slots=True)
class Reach:
    """What one effect or owned process could write: everything outside ``fenced``.

    ``fenced`` is the set of projects the caller was walled from when its call was
    admitted; ``None`` means everywhere. A project created after that read is in no
    snapshot, so it counts as reachable, which fails closed.
    """

    fenced: frozenset[str] | None = None

    def meets(self, claim: frozenset[str] | None) -> bool:
        """Whether this reach can touch anything ``claim`` (``None``: everywhere) changes."""
        if self.fenced is None or claim is None:
            return True
        return any(project_id not in self.fenced for project_id in claim)


#: The reach of a caller that is walled from nothing, or that could not be read.
EVERYWHERE = Reach()


@dataclass(frozen=True, slots=True)
class Holder:
    """One in-flight effect or live owned process, with what the refusal names."""

    reach: Reach
    thread_id: str | None
    what: str


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


def _agent_label(thread_id: str | None) -> str:
    """The blocking agent by thread title, without naming a Local only project's thread."""
    if not thread_id:
        return "the person's own session"
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


def _busy(prefix: str, holder: Holder | None) -> RepoPolicyTransitionBusy:
    if holder is None:
        return RepoPolicyTransitionBusy(prefix)
    return RepoPolicyTransitionBusy(
        f"{prefix} Blocked by {holder.what} from {_agent_label(holder.thread_id)}."
    )


class _RepoPolicyTransitionCoordinator:
    """One atomic owner for in-process repo effects and policy transitions."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._effects: dict[int, Holder] = {}
        self._next_effect = 0
        self._transition_active = False
        # ``None`` while the active transition claims everywhere.
        self._claim: frozenset[str] | None = None
        # Bumped as each transition ends, so a claim read before one can be checked.
        self._generation = 0

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @contextmanager
    def admit_effect(self, reach: Reach, holder: Holder) -> Iterator[None]:
        with self._lock:
            if self._transition_active and reach.meets(self._claim):
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
        active_process_count: Callable[[], int],
        owned_processes: Callable[[], Iterable[Holder]] | None,
    ) -> tuple[bool, Holder | None]:
        """Whether a live owned process meets the claim, and which one to name."""
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
            meeting = [holder for holder in holders if holder.reach.meets(claim)]
            return bool(meeting), (meeting[0] if meeting else None)
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
        return bool(process_count), (holders[0] if holders else None)

    @contextmanager
    def transition(
        self,
        *,
        active_process_count: Callable[[], int],
        owned_processes: Callable[[], Iterable[Holder]] | None = None,
        claim: frozenset[str] | None = None,
        claim_generation: int | None = None,
    ) -> Iterator[frozenset[str] | None]:
        blocker: RepoPolicyTransitionBusy | None = None
        with self._lock:
            if self._transition_active:
                raise RepoPolicyTransitionBusy(
                    "Repository policy transition is already in progress."
                )
            if claim is not None and claim_generation != self._generation:
                # Another transition ended after the claim was read: claim everywhere.
                claim = None
            effects = [h for h in self._effects.values() if h.reach.meets(claim)]
            if effects:
                blocker_holder: Holder | None = effects[0]
                prefix = "Repository policy transition is blocked by an active tool effect."
            else:
                busy, blocker_holder = self._process_blocker(
                    claim, active_process_count, owned_processes,
                )
                prefix = (
                    "Repository policy transition is blocked by an active owned process."
                    if busy else ""
                )
            if not prefix:
                self._transition_active = True
                self._claim = claim
        if prefix:
            # Named outside the lock: the label reads the database.
            blocker = _busy(prefix, blocker_holder)
            raise blocker
        try:
            yield claim
        finally:
            with self._lock:
                self._transition_active = False
                self._claim = None
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
    tool_name: str | None = None,
) -> Iterator[None]:
    """Admit one tool effect unless it conflicts with a repo transition."""
    holder = Holder(
        reach=reach,
        thread_id=thread_id,
        what=f"tool `{tool_name}`" if tool_name else "a tool call",
    )
    with _COORDINATOR.admit_effect(reach, holder):
        yield


@contextmanager
def repo_policy_transition(
    *,
    active_process_count: Callable[[], int],
    owned_processes: Callable[[], Iterable[Holder]] | None = None,
    claim: frozenset[str] | None = None,
    claim_generation: int | None = None,
) -> Iterator[frozenset[str] | None]:
    """Quarantine durable repo policy changes from the effects and processes they meet.

    ``claim`` is ``None`` (everywhere) unless the caller read a narrower one, with the
    :func:`transition_generation` it read it at; a narrowed claim also needs
    ``owned_processes`` so each live process is judged by its own reach. Yields the
    claim the transition was admitted under (``None`` once widened to everywhere), so
    the caller can check what it resolves inside the transition against it.
    """
    with _COORDINATOR.transition(
        active_process_count=active_process_count,
        owned_processes=owned_processes,
        claim=claim,
        claim_generation=claim_generation,
    ) as admitted:
        yield admitted
