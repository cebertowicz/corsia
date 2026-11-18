"""Process-local admission fence for repository policy transitions.

One coordinator, one lock. Every tool effect that can write carries a
:class:`Reach`: the exact paths a file-writing call writes, or else the projects
its caller was fenced from when the effect was admitted, or everywhere. A
transition carries a claim: the projects whose write rules it changes, or
everywhere, and the folders it changes when it knows them. They conflict when the
reach meets the claim. A call that only reads is no effect. Owned processes are
judged by count alone: any live one meets every transition that checks them.

A registration never waits for or refuses over effects and processes already
running (``checks_running=False``): it proceeds at once, and a call that meets the
registration while it runs is refused. The new write rules apply to what starts
after it, and to a file tool's own write check, which reads them on each call. A
shell call or owned process already running keeps the sandbox write rules it
launched with until it exits, so it can still write in a checkout the registration
protects. Other transitions are refused while an effect or process meets them.

Only a registration whose target sits inside the registering project's own
folder claims less than everywhere (``api.project_repo_claim``): every caller not
reaching that project is already walled from that folder, and the workspace is
already writable, so the registration adds no writable path for anyone outside
it. Every other transition claims everywhere, which excludes every effect and
every owned process, as before.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import logging
from pathlib import Path
import threading

from loguru import logger

log = logging.getLogger(__name__)


class RepoPolicyTransitionBusy(RuntimeError):
    """A repository effect or policy transition cannot start safely."""


def _overlaps(paths: tuple[Path, ...], folders: tuple[Path, ...]) -> bool:
    from tools.host_path_resolution import contains

    return any(contains(a, b) or contains(b, a) for a in paths for b in folders)


@dataclass(frozen=True, slots=True)
class Reach:
    """What one tool effect could write.

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
    """

    reach: Reach
    thread_id: str | None
    what: str


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


def _busy(fallback: str, holder: Holder | None) -> RepoPolicyTransitionBusy:
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
        f"Repositories can't change while {running}. Try again once it finishes."
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
        try:
            yield
        finally:
            with self._lock:
                del self._effects[key]

    @staticmethod
    def _any_process(active_process_count: Callable[[], int]) -> bool:
        """Whether any owned process is live; refused when that can't be read."""
        unverified = RepoPolicyTransitionBusy("Owned process activity could not be verified.")
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
        return process_count > 0

    @contextmanager
    def transition(
        self,
        *,
        active_process_count: Callable[[], int],
        claim: frozenset[str] | None = None,
        claim_generation: int | None = None,
        folders: tuple[Path, ...] | None = None,
        checks_running: bool = True,
    ) -> Iterator[frozenset[str] | None]:
        blocker: RepoPolicyTransitionBusy | None = None
        with self._lock:
            if self._transition_active:
                raise RepoPolicyTransitionBusy(
                    "Repository policy transition is already in progress."
                )
            if claim is not None and claim_generation != self._generation:
                # Another transition ended after the claim was read: claim everywhere.
                logger.info("repo policy transition claim {} widened to everywhere: "
                            "another transition ended after it was read", _scope(claim))
                claim = None
            effects = [h for h in self._effects.values() if h.reach.meets(claim, folders)]
            blocker_holder: Holder | None = None
            if not checks_running:
                running = ""
            elif effects:
                blocker_holder = effects[0]
                running = "an agent's action"
            else:
                running = "an agent's process" if self._any_process(active_process_count) else ""
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
            blocker = _busy(running, blocker_holder)
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
    claim: frozenset[str] | None = None,
    claim_generation: int | None = None,
    folders: tuple[Path, ...] | None = None,
    checks_running: bool = True,
) -> Iterator[frozenset[str] | None]:
    """Quarantine durable repo policy changes from the effects and processes they meet.

    ``claim`` is ``None`` (everywhere) unless the caller read a narrower one, with the
    :func:`transition_generation` it read it at. ``folders``
    are the folders whose write rules change, when the caller knows them; a call that
    writes known paths meets the transition only if they overlap one, and the caller
    must confirm inside the transition that what it changes is still inside them.
    Yields the claim the transition was admitted under (``None`` once widened to
    everywhere), so the caller can check what it resolves inside the transition.
    With ``checks_running`` false (registration) the transition never waits for or
    refuses over calls and processes already running; while it runs, a new call that
    meets it is refused. A shell call or owned process already running keeps the
    sandbox write rules it launched with until it exits.
    """
    with _COORDINATOR.transition(
        active_process_count=active_process_count,
        claim=claim,
        claim_generation=claim_generation,
        folders=folders,
        checks_running=checks_running,
    ) as admitted:
        yield admitted

