"""Transient scheduling for durable carry-in facts."""
from __future__ import annotations

import json
import threading
from collections.abc import Callable, Sequence
from pathlib import Path

from loguru import logger

from core.lesson_carry_in import (
    PENDING_EVENT,
    RESULT_EVENT,
    RETIREMENT_EVENT,
    CarryInFact,
    _fresh_scope,
    _matching_facts,
    drain_facts,
    record_pending,
)
from core.lesson_carry_in_contract import LocalTraceAdapter, TraceAdapter, rotated_cases

_DRAIN_LOCK = threading.Lock()


def record_committed_save(workspace: Path, paths: Sequence[Path], commit: str) -> None:
    """Start the non-blocking drain after an already-committed guidance save."""
    threading.Thread(
        target=_record_save_drain, args=(workspace, tuple(paths), commit), daemon=True,
        name="corsia-carry-in",
    ).start()


def schedule_committed_save(workspace: Path, paths: Sequence[Path], commit: str) -> None:
    """Keep a scheduling failure out of the completed guidance save."""
    try:
        record_committed_save(workspace, paths, commit)
    except Exception:
        logger.opt(exception=True).warning("carry-in scheduling failed")


def _owner_user_id(db, thread_id: str) -> str | None:
    row = db.execute("SELECT user_id FROM threads WHERE id=?", (thread_id,)).fetchone()
    return None if row is None or row[0] is None else str(row[0])


def _drain_committed_facts(
    db, workspace: Path, paths: Sequence[Path], commit: str,
    *, adapter_factory: Callable[..., TraceAdapter] | None = None,
) -> None:
    """Advance committed target edits through the owned local adapter."""
    facts = _matching_facts(db, workspace, paths, commit)
    for fact in facts:
        user_id = _owner_user_id(db, fact.owner_thread_id)
        if user_id is None:
            record_pending(db, fact)
            continue
        scope = _fresh_scope(db, workspace, fact.target_path)
        adapter = LocalTraceAdapter(user_id, scope) if adapter_factory is None else adapter_factory(user_id)
        drain_facts(db, workspace, [fact], adapter)


def _pending_facts(db) -> list[CarryInFact]:
    """Rebuild unfinished facts from their existing outcome-event attributes."""
    rows = db.execute(
        """SELECT pending.agent_thread_id,pending.attributes FROM outcome_events pending
           WHERE pending.event_name=? AND pending.retracted_at IS NULL AND NOT EXISTS (
             SELECT 1 FROM outcome_events terminal WHERE terminal.subject_key=pending.subject_key
               AND terminal.retracted_at IS NULL AND terminal.event_name IN (?, ?)
           )""",
        (PENDING_EVENT, RESULT_EVENT, RETIREMENT_EVENT),
    ).fetchall()
    facts: dict[str, CarryInFact] = {}
    for row in rows:
        try:
            attrs = json.loads(row["attributes"])
            fact = CarryInFact(
                str(attrs["lesson_id"]), str(row["agent_thread_id"]), str(attrs["target_commit"]),
                str(attrs["target_path"]), str(attrs["case_ref"]), str(attrs.get("role", "edit")),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        facts[fact.subject_key] = fact
    return list(facts.values())


def _drain_pending(workspace: Path) -> None:
    """Use existing startup/catalog-ready triggers for the bounded pending drain."""
    from infra.database import get_db
    from core import lesson_carry_in_contract as contract

    with _DRAIN_LOCK:
        db = get_db()
        try:
            facts = rotated_cases(_pending_facts(db), cap=contract.CASES_PER_EDIT_CAP, edit_number=0)
            for fact in facts:
                user_id = _owner_user_id(db, fact.owner_thread_id)
                if user_id is not None:
                    drain_facts(db, workspace, [fact], LocalTraceAdapter(user_id, _fresh_scope(db, workspace, fact.target_path)))
            db.commit()
        except Exception:
            logger.opt(exception=True).warning("pending carry-in drain failed")
        finally:
            db.close()


def schedule_pending_drain(workspace: Path) -> None:
    """Start no new lifecycle: use the existing bounded drain trigger."""
    threading.Thread(target=_drain_pending, args=(workspace,), daemon=True, name="corsia-carry-in").start()


def _record_save_drain(workspace: Path, paths: Sequence[Path], commit: str) -> None:
    from infra.database import get_db

    with _DRAIN_LOCK:
        db = None
        try:
            db = get_db()
            _drain_committed_facts(db, workspace, paths, commit)
            db.commit()
        except Exception:
            logger.opt(exception=True).warning("carry-in pending fact could not be recorded")
        finally:
            if db is not None:
                db.close()
