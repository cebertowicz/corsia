"""The C1 carry-in drain: one bounded pending case per wake on the application loop.

``core.carry_in_facts`` owns the append-only facts this reads. Each wake
settles a claim an earlier boot left open, reaps finished eval threads, then
reads only the oldest pending case. It runs only on the one ready local oMLX
Native catalog row while ``infra.carry_in_capacity`` proves it idle; otherwise that oldest case
shows ``no_ready_local_model`` and waits for the next save or restart. The
same check, on the claimed model only, runs again just before every sample's
provider call; failing it ends the claimed case in coverage, never a call.
Samples are ordinary fresh eval Turns (``eval.endpoint.run_eval_turn``) under
record-only tool isolation, so they cannot change anything they act on.
"""

from __future__ import annotations

import asyncio
import json
from functools import partial
from pathlib import Path
from typing import Any

from loguru import logger

from core import carry_in_facts as facts
from core.carry_in_facts import CarryInFact, EligibleCase
from infra import carry_in_capacity as capacity
from infra.db_async import run_in_db, run_off_db

SAMPLES = 3
NEEDED = 2
SAMPLE_TIMEOUT_SECONDS = 900.0
MAX_TARGET_BYTES = 200_000
EVAL_THREAD_PREFIX = "eval-c1-"
UNAVAILABLE = "no_ready_local_model"

_running = False
_again = False


class _ManifestMismatch(RuntimeError):
    """The fresh Turn's recorded context does not carry the lesson version under test."""


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
    stale = facts.open_facts(db, claimed=True)
    now = _now()
    for fact in stale:
        facts.record_terminal(db, fact, now, facts.COVERAGE, reason="interrupted")
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


def _manifest_carries(db, manifest_id: str | None, lesson_id: str, version: str) -> bool:
    from core.guidance_manifest import SKILLS_INDEX_SOURCE_ID

    if not manifest_id:
        return False
    row = db.execute("SELECT entries_json FROM context_manifests WHERE id=?", (manifest_id,)).fetchone()
    if row is None:
        return False
    for entry in json.loads(row[0]):
        if entry.get("source_id") == SKILLS_INDEX_SOURCE_ID:
            return {"id": lesson_id, "behavioral_sha256": version} in (entry.get("lessons") or [])
    return False


def _blobs(workspace: Path, fact: CarryInFact) -> tuple[bytes | None, bytes] | None:
    """The target before its commit (None when new) and at it; None when unusable."""
    from infra.guidance_git import GitFault

    try:
        parent = facts.commit_parent(workspace, fact.target_commit)
        before = facts.committed_bytes(workspace, parent, fact.target_path) if parent else None
        after = facts.committed_bytes(workspace, fact.target_commit, fact.target_path)
    except GitFault:
        return None
    if after is None or any(len(blob) > MAX_TARGET_BYTES for blob in (before or b"", after)):
        return None
    return before, after


def sample_prompt(case: dict[str, Any], target_path: str, blob: bytes | None) -> str:
    """The situation and the exact target revision; never the right call or its actions."""
    lines = [str(case["what_happened"]).strip(), ""]
    if blob is None:
        lines.append(f"There is no file at `{target_path}` yet.")
    else:
        text = blob.decode("utf-8", errors="replace")
        fence = "`" * max(3, _longest_backtick_run(text) + 1)
        lines += [f"`{target_path}` reads:", fence, text.rstrip("\n"), fence]
    return "\n".join(lines)


def _longest_backtick_run(text: str) -> int:
    longest = run = 0
    for char in text:
        run = run + 1 if char == "`" else 0
        longest = max(longest, run)
    return longest


async def _sample(
    fact: CarryInFact, owner: dict, model: str, actions: dict, prompt: str, label: str,
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
            owner["user_id"], model, thread_id=thread_id, title="Lesson regression check",
            project_id=owner["project_id"], parent_thread_id=fact.owner_thread_id,
        )
    except Exception as exc:
        raise _SampleFailed("admission", str(exc)) from exc
    created.append((thread_id, owner["user_id"]))
    refused: list[RuntimeError] = []

    async def before_dispatch(manifest_id: str | None) -> None:
        """The last step before this sample's provider call: lesson in context, capacity still exact and idle."""
        if not await run_in_db(_read, _manifest_carries, manifest_id, fact.lesson_id, fact.behavioral_sha256):
            refused.append(_ManifestMismatch("context manifest does not carry the lesson version under test"))
        elif await capacity.ready_idle(model, exclude_thread=thread_id) is None:
            refused.append(_Unavailable(f"{model} is not the one ready, idle local model"))
        if refused:
            raise refused[0]

    with carry_in_tool_isolation():
        turn_id, error = await run_eval_turn(
            thread_id=thread_id, thread_row=row, prompt=prompt, model=model,
            timeout_seconds=SAMPLE_TIMEOUT_SECONDS, after_manifest_before_dispatch=before_dispatch,
        )
    if refused:
        raise refused[0]
    if turn_id is None:
        detail = error or "no Turn"
        reason = "timeout" if "timed out" in detail else (
            "provider" if detail.startswith("Eval turn errored") else "admission"
        )
        raise _SampleFailed(reason, detail)
    result = await read_turn_result(thread_id, turn_id)
    if result.error:
        raise _SampleFailed("provider", result.error)
    return sample_passes(actions, result.tool_calls)


async def _run_side(fact, owner, model, actions, prompt, side, created, completed) -> int:
    """Samples of one side until its outcome is decided; returns the pass count."""
    passes = fails = 0
    for index in range(1, SAMPLES + 1):
        if passes >= NEEDED or fails >= NEEDED:
            break
        passed = await _sample(fact, owner, model, actions, prompt, f"{side}{index}", created)
        completed[0] += 1
        passes, fails = passes + passed, fails + (not passed)
    return passes


async def _evaluate(
    fact: CarryInFact, case: EligibleCase, owner: dict, model: str,
    before: bytes | None, after: bytes,
) -> None:
    """Sample and settle one claimed case; eval threads go once its terminal fact commits."""
    created: list[tuple[str, str]] = []
    completed = [0]
    terminal: dict[str, Any]
    try:
        actions = case.actions
        assert actions is not None
        baseline_passes = await _run_side(
            fact, owner, model, actions, sample_prompt(case.case, fact.target_path, before),
            "b", created, completed,
        )
        baseline_failures = completed[0] - baseline_passes
        if baseline_failures < NEEDED:
            terminal = {"event": facts.RESULT, "verdict": "not_discriminating",
                        "baseline_failures": baseline_failures, "candidate_passes": 0}
        else:
            candidate_passes = await _run_side(
                fact, owner, model, actions, sample_prompt(case.case, fact.target_path, after),
                "c", created, completed,
            )
            terminal = {
                "event": facts.RESULT,
                "verdict": "passed" if candidate_passes >= NEEDED else "failed",
                "baseline_failures": baseline_failures, "candidate_passes": candidate_passes,
            }
    except _ManifestMismatch:
        terminal = {"event": facts.RETIRED, "reason": "manifest_mismatch"}
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
    """Advance only the oldest pending case; True when a later wake may find runnable work.

    The result informs callers and tests only: ``drain`` never runs another wake on it.

    Without one ready, idle local model that case alone records ``no_ready_local_model``
    and the drain stops: younger cases wait behind it, unread.
    """
    await run_in_db(partial(_write, _settle_interrupted, scope="carry_in_interrupted"))
    await _delete_threads(await run_in_db(_read, _stranded_threads))
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
    blobs = None if reason is not None else await run_off_db(_blobs, workspace, fact)
    if reason is not None or blobs is None:
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
    if not await run_in_db(partial(_write, facts.record_claim, fact, _now(), model, scope="carry_in_claim")):
        return True
    await _evaluate(fact, case, owner, model, *blobs)
    return True
