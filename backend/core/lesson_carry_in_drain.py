"""Transient scheduling for durable carry-in facts."""
from __future__ import annotations

import json
import threading
from collections.abc import Callable, Sequence
from pathlib import Path

from loguru import logger

from core.lesson_carry_in import (
    CarryInFact,
    _case_for_fact,
    _fresh_scope,
    _matching_facts,
    _target_snapshot,
    drain_facts,
    is_pending,
    record_pending,
    record_retirement,
    record_source_skipped,
)
from core.lesson_carry_in_contract import PENDING_EVENT, LocalTraceAdapter, TraceAdapter, rotated_cases

NO_USER = "carry-in owner has no user"

_DRAIN_LOCK = threading.Lock()
_THREADS_LOCK = threading.RLock()
_DRAIN_THREADS: set[threading.Thread] = set()
_DRAINS_STOPPING = False


def _run_drain(
    target: Callable[..., None], args: tuple[object, ...], thread: threading.Thread,
) -> None:
    """Own one drain's connection and unregister it before its thread exits."""
    try:
        target(*args)
    finally:
        # Every drain opens its pooled connection from its own thread. Release it
        # before teardown may replace or close the database pool.
        from infra.database import close_thread_db

        close_thread_db()
        with _THREADS_LOCK:
            _DRAIN_THREADS.discard(thread)


def _schedule_drain(target: Callable[..., None], args: tuple[object, ...]) -> None:
    """Start a nonblocking drain while retaining it for explicit shutdown."""
    holder: dict[str, threading.Thread] = {}

    def run() -> None:
        _run_drain(target, args, holder["thread"])

    thread = threading.Thread(target=run, args=(), daemon=True, name="corsia-carry-in")
    holder["thread"] = thread
    with _THREADS_LOCK:
        if _DRAINS_STOPPING:
            return
        _DRAIN_THREADS.add(thread)
        try:
            thread.start()
        except BaseException:
            _DRAIN_THREADS.discard(thread)
            raise


def join_drain_threads() -> None:
    """Wait for every drain that was already scheduled before database teardown."""
    while True:
        with _THREADS_LOCK:
            threads = tuple(thread for thread in _DRAIN_THREADS if thread is not threading.current_thread())
        if not threads:
            return
        for thread in threads:
            thread.join()


def shutdown_drain_threads() -> None:
    """Permanently stop new drains, then join existing drains before storage shutdown."""
    global _DRAINS_STOPPING
    with _THREADS_LOCK:
        _DRAINS_STOPPING = True
    join_drain_threads()


def record_committed_save(workspace: Path, paths: Sequence[Path], commit: str) -> None:
    """Start the non-blocking drain after an already-committed guidance save."""
    _schedule_drain(_record_save_drain, (workspace, tuple(paths), commit))


def _owner_user_id(db, thread_id: str) -> str | None:
    row = db.execute("SELECT user_id FROM threads WHERE id=?", (thread_id,)).fetchone()
    return None if row is None or row[0] is None else str(row[0])


def _drain_committed_facts(
    db, workspace: Path, paths: Sequence[Path], commit: str,
    *, adapter_factory: Callable[..., TraceAdapter] | None = None,
) -> None:
    """Commit every matching case as pending, then advance each through the local adapter."""
    from infra.database import write_transaction

    facts, skipped = _matching_facts(db, workspace, paths, commit)
    users = {fact.subject_key: _owner_user_id(db, fact.owner_thread_id) for fact in facts}
    with write_transaction(db, scope="carry_in_pending") as transaction:
        for source, reason in skipped:
            record_source_skipped(transaction, source, reason)
        for fact in facts:
            record_pending(transaction, fact)
            if users[fact.subject_key] is None:
                record_retirement(transaction, fact, NO_USER)
    for fact in facts:
        user_id = users[fact.subject_key]
        if user_id is None:
            continue
        scope = _fresh_scope(db, workspace, fact.target_path)
        adapter = LocalTraceAdapter(user_id, scope) if adapter_factory is None else adapter_factory(user_id)
        drain_facts(db, workspace, [fact], adapter)


def _pending_facts(db) -> list[CarryInFact]:
    """Rebuild unfinished facts from their existing outcome-event attributes."""
    rows = db.execute(
        """SELECT attributes FROM outcome_events
           WHERE event_name=? AND retracted_at IS NULL
           ORDER BY observed_ts, id""",
        (PENDING_EVENT,),
    ).fetchall()
    facts: dict[str, CarryInFact] = {}
    for row in rows:
        try:
            attrs = json.loads(row["attributes"])
            fact = CarryInFact(
                str(attrs["lesson_id"]), str(attrs["owner_thread_id"]), str(attrs["target_commit"]),
                str(attrs["target_path"]), str(attrs["case_ref"]), str(attrs.get("role", "edit")),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        facts[fact.subject_key] = fact
    return [fact for fact in facts.values() if is_pending(db, fact)]


def _drainable_pending_facts(db, workspace: Path, facts: Sequence[CarryInFact]) -> list[CarryInFact]:
    """Retire facts that can no longer run so one bounded drain cannot starve later work."""
    from infra.database import write_transaction

    drainable: list[CarryInFact] = []
    retired: list[tuple[CarryInFact, str]] = []
    for fact in facts:
        if _case_for_fact(workspace, fact) is None:
            retired.append((fact, "carry-in case is unavailable"))
        elif _target_snapshot(workspace, fact)[1] == "target_changed_after_commit":
            retired.append((fact, "target changed after recorded commit"))
        elif _owner_user_id(db, fact.owner_thread_id) is None:
            retired.append((fact, NO_USER))
        else:
            drainable.append(fact)
    if retired:
        with write_transaction(db, scope="carry_in_retirement") as transaction:
            for fact, reason in retired:
                record_retirement(transaction, fact, reason)
    return drainable


def _pending_event_count(db) -> int:
    """Count unresolved facts for a bounded drain window, never drain attempts."""
    return len(_pending_facts(db))


def _drain_pending(workspace: Path) -> None:
    """Use existing startup/catalog-ready triggers for the bounded pending drain."""
    from infra.database import get_db, write_transaction
    from core import lesson_carry_in_contract as contract

    with _DRAIN_LOCK:
        db = get_db()
        try:
            cap = contract.CASES_PER_EDIT_CAP
            facts = rotated_cases(
                _drainable_pending_facts(db, workspace, _pending_facts(db)),
                cap=cap,
                edit_number=0 if cap is None or cap <= 0 else _pending_event_count(db) // cap,
            )
            for fact in facts:
                user_id = _owner_user_id(db, fact.owner_thread_id)
                if user_id is not None:
                    drain_facts(db, workspace, [fact], LocalTraceAdapter(user_id, _fresh_scope(db, workspace, fact.target_path)))
        except Exception:
            logger.opt(exception=True).warning("pending carry-in drain failed")
        finally:
            db.close()


def schedule_pending_drain(workspace: Path) -> None:
    """Use the existing lifecycle trigger to start a nonblocking bounded drain."""
    _schedule_drain(_drain_pending, (workspace,))


def _record_save_drain(workspace: Path, paths: Sequence[Path], commit: str) -> None:
    from infra.database import get_db

    with _DRAIN_LOCK:
        db = None
        try:
            db = get_db()
            _drain_committed_facts(db, workspace, paths, commit)
        except Exception:
            logger.opt(exception=True).warning("carry-in pending fact could not be recorded")
        finally:
            if db is not None:
                db.close()
