"""The C1 carry-in drain: one bounded saved or pending item per app-loop wake.

``core.carry_in_facts`` owns the append-only facts this reads. Each wake
settles a claim no running wake owns, reaps finished eval threads, then either
matches the oldest saved target or advances the oldest pending case.
It runs only on the one ready local oMLX Native catalog row while ``infra.carry_in_capacity`` proves it idle; otherwise that oldest case
shows ``no_ready_local_model`` and waits for the next save or restart. The
same check, on the claimed model only, runs again just before every sample's
provider call; failing it ends the claimed case in coverage, never a call.
Samples are ordinary fresh eval Turns (``eval.endpoint.run_eval_turn``) under
record-only tool isolation, so they cannot change anything they act on.
A candidate sample of a ``trial`` lesson carries the lesson the way task start
delivers it (``core.lesson_delivery``); a baseline sample carries none. Just
before each provider call the Turn's own recorded input must match its side.
A case whose two sides would get the same input retires as ``same_input``: it
cannot show the lesson's effect.
"""

from __future__ import annotations

import asyncio
from functools import partial
from pathlib import Path
from typing import Any

from loguru import logger

from core import carry_in_facts as facts
from core.carry_in_facts import CarryInFact
from infra import carry_in_capacity as capacity
from infra.carry_in_arm import (
    Arm as _Arm,
    arm as _arm,
    sample_prompt,
    turn_input as _turn_input,
)
from infra.db_async import run_in_db, run_off_db

SAMPLES = 3
NEEDED = 2
SAMPLE_TIMEOUT_SECONDS = 900.0
MAX_TARGET_BYTES = 200_000
EVAL_THREAD_PREFIX = "eval-c1-"
UNAVAILABLE = "no_ready_local_model"

_running = False
_again = False


class _DeliveryMismatch(RuntimeError):
    """The fresh Turn's recorded input does not match its side: the lesson block is missing or present."""


class _Unavailable(RuntimeError):
    """The bound model was no longer the one ready, idle local row at dispatch; no provider call ran."""


class _SampleFailed(RuntimeError):
    """A sample ended without a mechanically scored Turn; it never counts."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


def request_drain(workspace: Path | None = None) -> None:
    """Ask the application loop for one wake; a request during a running wake gets its own after it."""
    from infra.app_loop import AppLoopUnavailable, submit

    try:
        submit(drain(workspace))
    except AppLoopUnavailable:
        logger.debug("carry-in drain not scheduled: no application loop")


async def drain(workspace: Path | None = None) -> None:
    """Run exactly one ``wake`` for this request; one drain at a time per process.

    A request arriving while a wake runs is remembered and earns one more wake
    afterwards, never a bulk pass: what a wake returns does not schedule another.
    """
    global _running, _again
    if _running:
        _again = True
        return
    _running = True
    try:
        if workspace is None:
            from infra.config import WORKSPACE

            workspace = Path(WORKSPACE)
        _again = True
        while _again:
            _again = False
            try:
                await wake(workspace)
            except Exception:
                logger.opt(exception=True).warning("carry-in drain wake failed")
    finally:
        _running = False


def _now() -> str:
    from infra.time import utc_now_iso

    return utc_now_iso()


def _write(fn, *args, scope: str, **kwargs):
    from infra.database import get_db, write_transaction

    with write_transaction(get_db(), scope=scope) as db:
        return fn(db, *args, **kwargs)


def _read(fn, *args, **kwargs):
    from infra.database import get_db

    return fn(get_db(), *args, **kwargs)


def _settle_interrupted(db) -> int:
    """End every claim no wake is running: an earlier boot's was interrupted by its restart;
    this boot's lost its own terminal write to a fault (one wake runs at a time per process)."""
    from core.turn_core import BOOT_EPOCH

    stale = facts.open_facts(db, claimed=True)
    now = _now()
    for fact in stale:
        reason = "error" if facts.claim_boot(db, fact) == BOOT_EPOCH else "interrupted"
        facts.record_terminal(db, fact, now, facts.COVERAGE, reason=reason)
    return len(stale)


def _stranded_threads(db) -> list[tuple[str, str]]:
    rows = db.execute(
        "SELECT id, user_id FROM threads WHERE id LIKE ? ESCAPE '\\'",
        (EVAL_THREAD_PREFIX.replace("_", "\\_") + "%",),
    ).fetchall()
    out = []
    for row in rows:
        prefix = str(row[0])[len(EVAL_THREAD_PREFIX):].split("-", 1)[0]
        if prefix and facts.has_terminal_with_prefix(db, prefix):
            out.append((str(row[0]), str(row[1] or "")))
    return out


async def _delete_threads(threads: list[tuple[str, str]]) -> None:
    from api.thread_deletion import delete_owned_thread

    for thread_id, user_id in threads:
        try:
            status = await delete_owned_thread(thread_id, user_id, [])
            if status != "ok":
                logger.warning("carry-in eval thread {} not deleted: {}", thread_id, status)
        except Exception:
            logger.opt(exception=True).warning("carry-in eval thread {} not deleted", thread_id)


def _owner(db, thread_id: str) -> dict | None:
    row = db.execute("SELECT user_id, project_id FROM threads WHERE id=?", (thread_id,)).fetchone()
    return None if row is None or not row[0] else {"user_id": str(row[0]), "project_id": row[1]}


def _blobs(workspace: Path, fact: CarryInFact) -> tuple[bytes | None, bytes] | None:
    """The target before its commit (None when new) and at it; None when its owner must act.

    None means the commit holds no such file or a revision over ``MAX_TARGET_BYTES``.
    A Git fault reading the history raises ``GitFault``: that is no fact about the case.
    """
    parent = facts.commit_parent(workspace, fact.target_commit)
    before = facts.committed_bytes(workspace, parent, fact.target_path) if parent else None
    after = facts.committed_bytes(workspace, fact.target_commit, fact.target_path)
    if after is None or any(len(blob) > MAX_TARGET_BYTES for blob in (before or b"", after)):
        return None
    return before, after


async def _sample(
    fact: CarryInFact, owner: dict, model: str, actions: dict, arm: _Arm, label: str,
    created: list[tuple[str, str]],
) -> bool:
    """Run one isolated fresh Turn and score it; raise when it cannot be scored."""
    from core.carry_in_scoring import sample_passes
    from eval.endpoint import open_eval_thread, run_eval_turn
    from eval.tool_allowlist import carry_in_tool_isolation
    from eval.turn_result import read_turn_result

    thread_id = f"{EVAL_THREAD_PREFIX}{fact.identity[:16]}-{label}"
    try:
        thread_id, row = await open_eval_thread(
            owner["user_id"], model, thread_id=thread_id, title="Skill re-test",
            project_id=owner["project_id"], parent_thread_id=fact.owner_thread_id,
        )
    except Exception as exc:
        raise _SampleFailed("admission", str(exc)) from exc
    created.append((thread_id, owner["user_id"]))
    refused: list[RuntimeError] = []

    async def before_dispatch(turn_id: str) -> None:
        """The last step before this sample's provider call.

        The Turn's own recorded input must hold exactly this side's delivery and
        capacity must still be exact and idle.
        """
        recorded = await run_in_db(_read, _turn_input, thread_id, turn_id)
        if not arm.carried_by(recorded):
            refused.append(_DeliveryMismatch("the Turn's recorded input does not match its side's lesson delivery"))
        elif await capacity.ready_idle(model, exclude_thread=thread_id) is None:
            refused.append(_Unavailable(f"{model} is not the one ready, idle local model"))
        if refused:
            raise refused[0]

    with carry_in_tool_isolation():
        turn_id, failure = await run_eval_turn(
            thread_id=thread_id, thread_row=row, prompt=arm.prompt, model=model,
            timeout_seconds=SAMPLE_TIMEOUT_SECONDS, after_manifest_before_dispatch=before_dispatch,
        )
    if refused:
        raise refused[0]
    if turn_id is None:
        if failure is None:
            raise _SampleFailed("handoff", "the eval route returned neither a Turn nor a failure")
        raise _SampleFailed(failure.kind, failure.detail)
    result = await read_turn_result(thread_id, turn_id)
    if result.error:
        raise _SampleFailed("provider", result.error)
    return sample_passes(actions, result.tool_calls)


async def _run_side(fact, owner, model, actions, arm, side, created, completed) -> int:
    """Samples of one side until its outcome is decided; returns the pass count."""
    passes = fails = 0
    for index in range(1, SAMPLES + 1):
        if passes >= NEEDED or fails >= NEEDED:
            break
        passed = await _sample(fact, owner, model, actions, arm, f"{side}{index}", created)
        completed[0] += 1
        passes, fails = passes + passed, fails + (not passed)
    return passes


async def _evaluate(
    fact: CarryInFact, actions: dict, owner: dict, model: str, baseline: _Arm, candidate: _Arm,
) -> None:
    """Sample and settle one claimed case; eval threads go once its terminal fact commits."""
    created: list[tuple[str, str]] = []
    completed = [0]
    terminal: dict[str, Any]
    try:
        baseline_passes = await _run_side(fact, owner, model, actions, baseline, "b", created, completed)
        baseline_failures = completed[0] - baseline_passes
        if baseline_failures < NEEDED:
            terminal = {"event": facts.RESULT, "verdict": "not_discriminating",
                        "baseline_failures": baseline_failures, "candidate_passes": 0}
        else:
            candidate_passes = await _run_side(fact, owner, model, actions, candidate, "c", created, completed)
            terminal = {
                "event": facts.RESULT,
                "verdict": "passed" if candidate_passes >= NEEDED else "failed",
                "baseline_failures": baseline_failures, "candidate_passes": candidate_passes,
            }
    except _DeliveryMismatch:
        terminal = {"event": facts.RETIRED, "reason": "delivery_mismatch"}
    except _Unavailable as lost:  # claimed identities never re-pend (spec item 6)
        logger.info("carry-in case {} lost local capacity: {}", fact.identity[:16], lost)
        terminal = {"event": facts.COVERAGE, "reason": UNAVAILABLE}
    except _SampleFailed as failed:
        logger.info("carry-in sample for {} not scored: {}", fact.identity[:16], failed)
        terminal = {"event": facts.COVERAGE, "reason": failed.reason}
    except asyncio.CancelledError:
        terminal = {"event": facts.COVERAGE, "reason": "interrupted"}
        await _settle(fact, terminal, completed[0], created)
        raise
    except Exception:  # a fault in this check's own code: never reported as a restart or a sample
        logger.opt(exception=True).warning("carry-in case {} failed in its check", fact.identity[:16])
        terminal = {"event": facts.COVERAGE, "reason": "error"}
    await _settle(fact, terminal, completed[0], created)


async def _settle(fact: CarryInFact, terminal: dict[str, Any], completed: int, created) -> None:
    attrs = {key: value for key, value in terminal.items() if key != "event"}
    if terminal["event"] != facts.RETIRED:
        attrs["samples_completed"] = completed
    await run_in_db(partial(
        _write, facts.record_terminal, fact, _now(), terminal["event"],
        scope="carry_in_terminal", **attrs,
    ))
    await _delete_threads(created)


async def wake(workspace: Path) -> bool:
    """Advance one oldest saved target or pending case; True when later work may run.

    The result informs callers and tests only: ``drain`` never runs another wake on it.

    Without one ready, idle local model, or with an unreadable guidance history, that
    case alone records why it waits and the drain stops: younger cases wait behind it, unread.
    """
    from core.turn_core import BOOT_EPOCH
    from infra.guidance_git import GitFault

    await run_in_db(partial(_write, _settle_interrupted, scope="carry_in_interrupted"))
    await _delete_threads(await run_in_db(_read, _stranded_threads))
    if await _run_saved_commit(workspace):
        return True
    oldest = await run_in_db(_read, facts.open_facts, claimed=False, limit=1)
    if not oldest:
        return False
    fact = oldest[0]
    case = await run_in_db(_read, facts.eligible_case, workspace, fact)
    owner = await run_in_db(_read, _owner, fact.owner_thread_id)
    reason = case if isinstance(case, str) else (
        "owner_missing" if owner is None else
        "not_mechanically_executable" if case.actions is None else None
    )
    try:
        blobs = None if reason is not None else await run_off_db(_blobs, workspace, fact)
    except GitFault as fault:  # the history, not the case, failed: it stays the oldest pending case
        logger.warning("carry-in case {} waits: guidance history unreadable: {}", fact.identity[:16], fault)
        await run_in_db(partial(
            _write, facts.record_waiting, fact, _now(), "history_unavailable", scope="carry_in_waiting",
        ))
        return False
    arms = None if reason is not None or blobs is None else (
        _arm(fact, case, blobs[0], deliver=False), _arm(fact, case, blobs[1], deliver=case.deliver),
    )
    if arms is not None and arms[0].prompt == arms[1].prompt:
        reason = "same_input"
    if reason is not None or arms is None:
        await run_in_db(partial(
            _write, facts.record_terminal, fact, _now(), facts.RETIRED,
            scope="carry_in_retired", reason=reason or "target_unavailable",
        ))
        return True
    model = await capacity.ready_idle()
    if model is None:
        await run_in_db(partial(
            _write, facts.record_waiting, fact, _now(), UNAVAILABLE, scope="carry_in_waiting",
        ))
        return False
    if not await run_in_db(partial(
        _write, facts.record_claim, fact, _now(), model, BOOT_EPOCH, scope="carry_in_claim",
    )):
        return True
    await _evaluate(fact, case.actions, owner, model, *arms)
    return True


async def _run_saved_commit(workspace: Path) -> bool:
    """Match and close exactly one oldest saved target off the save request path."""
    saved = await run_in_db(_read, facts.oldest_saved_commit)
    if saved is None:
        return False
    await run_in_db(partial(
        _read, facts.process_saved_commit, workspace, saved, _now(),
    ))
    return True
