"""The first-run wake and OB10's content-free onboarding events.

The wake is the CORSIA agent's first input on a new install. The events
measure time to the first governed outcome locally in DuckDB
(``TIME_TO_FIRST_OUTCOME_SQL``); nothing leaves the machine. Every event is
non-load-bearing: a failure is logged and never changes the caller's result.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone

from infra.presence_time import parse
from infra.product_events import analytics_disabled, emit_product_event
from infra.ui_labels import SETTINGS_KEYS

log = logging.getLogger(__name__)

CATEGORY = "onboarding"
FIRST_RUN_EVENT = "onboarding.first_run"
FIRST_RUN_CHIP = "CORSIA was installed. Pick a model to start your conductor."
FIRST_RUN_SUMMARY = (
    "First run of a new install: load skill conductor-bootstrap and follow "
    "references/first-run.md."
)
OUTCOME_OPERATIONS = frozenset({"mark_done", "accept"})
TASK_START = "Getting started"
TASK_PROVIDERS = "Additional provider setup"
TASK_PROJECT = "Set up your first project"
TASK_GITHUB = "Set up GitHub"
TASK_WEB_SEARCH = "Connect web search"
TASK_CONFIG = "Configuration checks"
TASK_PHONE = "Remote access"
# The one list of seeded setup tasks: title and acceptance criteria, in plan order.
FIRST_RUN_SETUP_TASKS = (
    (TASK_START, "the user has the conductor's introduction to CORSIA and to the setup"),
    (
        TASK_PROVIDERS,
        "each Claude or ChatGPT subscription the user named works, or the user chose not now",
    ),
    (
        TASK_CONFIG,
        "host access and the reviewer are explained and the person's reviewer choice is applied, "
        "Auto-approve is explained and the conductor never turns it on, MCP servers found are "
        "imported and reported, any oMLX decision is answered, and one post lists what was checked",
    ),
    (
        TASK_WEB_SEARCH,
        "one web search works, or the user chose not now "
        f"({SETTINGS_KEYS} has the key whenever they want it)",
    ),
    (
        TASK_GITHUB,
        "the agents' GitHub account reads ready and is starred, or the user chose how agents "
        "post, or chose not now",
    ),
    (
        TASK_PHONE,
        "CORSIA's private address answers from this Mac and the user has it, "
        "or the user chose not now",
    ),
    (
        TASK_PROJECT,
        "the project's conductor reports back, or the user chose not now",
    ),
)

# Seconds from the first account to the first accepted outcome; NULL until both exist.
TIME_TO_FIRST_OUTCOME_SQL = """
SELECT date_diff('second',
    (SELECT min(ts) FROM product_events
      WHERE category = 'onboarding' AND action = 'account_created'),
    (SELECT min(ts) FROM product_events
      WHERE category = 'onboarding' AND action = 'first_outcome')
) AS seconds_to_first_outcome
"""

_emitted: set[str] = set()


def first_run_message_id(conductor_id: str) -> str:
    return f"first-run:{conductor_id}"


def seed_first_run_setup_plan(
    conductor_id: str, user_id: str, *, db: sqlite3.Connection,
) -> None:
    """Create any missing visible setup tasks before the conductor's first turn."""
    from plans import public

    actor = public.PlanActor(conductor_id, user_id, None)
    result = public.ensure_current_plan(actor=actor, include_folded=True, db=db)
    plan = result["plan"]
    listed = {node["title"] for node in result["nodes"]}
    for order_key, (title, acceptance_criteria) in enumerate(FIRST_RUN_SETUP_TASKS):
        if title not in listed:
            public.add_node(
                plan["id"],
                kind="task",
                title=title,
                acceptance_criteria=acceptance_criteria,
                order_key=order_key,
                provenance="onboarding.first_run",
                db=db,
            )


def start_first_run_task(conductor_id: str, user_id: str) -> None:
    """Start "Getting started" once the seeded plan has committed.

    The conductor's own ``start``, in its own transaction (a plan mutation
    prepares its notices from committed state); it queues no input, drives no
    thread, and drops the notices. A replay under the fixed mutation id is a
    no-op, and a node already past ``pending`` is left alone. Any failure is
    logged and swallowed: the account exists, and it still gets its events.
    """
    from infra.database import get_db
    from plans import public

    try:
        db = get_db()
        try:
            actor = public.PlanActor(conductor_id, user_id, None)
            listing = public.ensure_current_plan(actor=actor, include_folded=True, db=db)
            node = next(
                (item for item in listing["nodes"] if item["title"] == TASK_START), None,
            )
            if node is None or node["state"] != "pending":
                return
            public.mutate(
                listing["plan"]["id"], "start",
                {"node_id": node["id"], "expected_revision": node["revision"]},
                actor=actor, provenance="onboarding.first_run",
                mutation_id=f"first-run-start:{conductor_id}", db=db,
            )
        finally:
            db.close()
    except Exception:  # noqa: BLE001 - the account is committed; an unstarted task only reads "Not started"
        log.warning("first-run task not started for %s", conductor_id, exc_info=True)


def enqueue_first_run_wake(
    conductor_id: str, *, first_run_path: str = "script", db: sqlite3.Connection,
) -> bool:
    """Queue the first-run wake inside the caller's transaction.

    Only a conductor with no prior conversation gets it; its scripted opening
    is allowed. A restored conductor's real history is left alone. The id makes a retry
    a duplicate. With no model bound the input waits under
    ``no_model_selected`` until the user's first model choice drives it.
    """
    from events.delivery import enqueue_event_wake

    if db.execute(
        "SELECT 1 FROM messages WHERE thread_id = ? AND id != ? LIMIT 1",
        (conductor_id, f"first-run-opening:{conductor_id}"),
    ).fetchone():
        return False
    enqueue_event_wake(
        conductor_id, FIRST_RUN_EVENT, FIRST_RUN_SUMMARY,
        chip=FIRST_RUN_CHIP, message_id=first_run_message_id(conductor_id),
        metadata={"first_run_path": first_run_path}, db=db,
    )
    return True


_HELD_WAKE = "FROM messages WHERE id=? AND thread_id=? AND delivery_state='queued' AND delivery_error=?"


def held_first_run_path(thread_id: str, *, db: sqlite3.Connection) -> str | None:
    """The held wake's path, defaulting legacy wakes without metadata to ``script``."""
    from infra.send_target import NO_MODEL_SELECTED
    import json

    row = db.execute(
        f"SELECT metadata {_HELD_WAKE}",
        (first_run_message_id(thread_id), thread_id, NO_MODEL_SELECTED),
    ).fetchone()
    if row is None:
        return None
    try:
        path = json.loads(row["metadata"] or "{}").get("first_run_path")
    except (TypeError, ValueError):
        path = None
    return path if path in {"app", "script"} else "script"


def fill_held_first_run_wake(thread_id: str, content: str, *, db: sqlite3.Connection) -> bool:
    """Replace the held wake's content with its inline form, in the model-choice write.

    Only a wake no model has read changes: once the choice adopts it, it is
    history like any other input. The chip reads the row's metadata, so what
    the person sees is unchanged.
    """
    from infra.send_target import NO_MODEL_SELECTED

    return db.execute(
        f"UPDATE messages SET content=? WHERE id IN (SELECT id {_HELD_WAKE})",
        (content, first_run_message_id(thread_id), thread_id, NO_MODEL_SELECTED),
    ).rowcount == 1


def _has_marked_handoff(
    conductor_id: str, boundary: datetime, *, db: sqlite3.Connection,
) -> bool:
    """Whether a canonical marked update was persisted at or after ``boundary``."""
    rows = db.execute(
        "SELECT created_at FROM thread_updates WHERE thread_id=? AND kind='marked'",
        (conductor_id,),
    )
    for row in rows:
        try:
            if parse(row["created_at"]) >= boundary:
                return True
        except (TypeError, ValueError):
            continue
    return False


def first_run_pending(conductor_id: str, *, db: sqlite3.Connection) -> bool:
    """Whether the first-run wake has no durable, post-wake conductor handoff."""
    wake = db.execute(
        "SELECT created_at FROM messages WHERE id=? AND thread_id=? LIMIT 1",
        (first_run_message_id(conductor_id), conductor_id),
    ).fetchone()
    if wake is None:
        return False
    try:
        boundary = parse(wake["created_at"])
    except (TypeError, ValueError) as error:
        log.warning("first-run recovery wake timestamp failed: %s", type(error).__name__)
        return True
    try:
        if _has_marked_handoff(conductor_id, boundary, db=db):
            return False
        from plans import public

        return not public.has_genuine_decision_raised_at_or_after(
            db, owner_thread_id=conductor_id, boundary=boundary,
        )
    except sqlite3.DatabaseError as error:
        log.warning("first-run recovery contact read failed: %s", type(error).__name__)
        return True


def emit(action: str, **properties: object) -> bool:
    """Emit one onboarding event and report confirmed persistence."""
    return emit_product_event(CATEGORY, action, properties=dict(properties))


def _already_emitted(action: str) -> bool | None:
    """Return whether an event exists, or ``None`` when analytics is unreadable."""
    if action in _emitted:
        return True
    from infra import analytics_db

    try:
        row = analytics_db.get_db().execute(
            "SELECT 1 FROM product_events WHERE category = ? AND action = ? LIMIT 1",
            (CATEGORY, action),
        ).fetchone()
    except Exception:  # noqa: BLE001 - analytics must not break onboarding
        log.warning("onboarding %s emission lookup failed", action, exc_info=True)
        return None
    if row:
        _emitted.add(action)
    return bool(row)


def _account_started_at(db: sqlite3.Connection) -> datetime | None:
    row = db.execute("SELECT MIN(created_at) AS at FROM users").fetchone()
    return _parse(row["at"]) if row and row["at"] else None


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)


def _seconds_since_account(db: sqlite3.Connection, now: datetime) -> int | None:
    started = _account_started_at(db)
    return None if started is None else max(0, int((now - started).total_seconds()))


def _emit_first(action: str, **properties: object) -> None:
    if analytics_disabled():
        return
    emitted = _already_emitted(action)
    if emitted is not False:
        return
    if emit(action, **properties):
        _emitted.add(action)


def _is_corsia_agent(thread_id: str, db: sqlite3.Connection) -> bool:
    from infra.corsia_agent import get_corsia_agent

    return get_corsia_agent(db=db) == thread_id


def _guarded(action: str, read) -> None:
    """Run ``read(db)`` on its own connection; emit ``action`` with what it returns.

    ``read`` returns the event's properties, or None to emit nothing. Nothing
    here raises: measurement never breaks the path that calls it.
    """
    if analytics_disabled() or action in _emitted:
        return
    from infra.database import get_db

    try:
        db = get_db()
        try:
            properties = read(db)
        finally:
            db.close()
    except Exception:  # noqa: BLE001 - property reads must not break onboarding
        log.warning("onboarding %s failed", action, exc_info=True)
        return
    if properties is not None:
        _emit_first(action, **properties)


def note_first_model_selected(thread_id: str, model: str) -> None:
    """``first_model_selected`` when the CORSIA agent gets its first model."""

    def read(db: sqlite3.Connection) -> dict | None:
        if not _is_corsia_agent(thread_id, db):
            return None
        source = model.split("/", 1)[0].split("@", 1)[0]
        return {"source_kind": source,
                "seconds_since_account": _seconds_since_account(db, _utcnow())}

    _guarded("first_model_selected", read)


def note_turn_completed(thread_id: str) -> None:
    """``first_turn_completed`` on the CORSIA agent's first finished turn."""

    def read(db: sqlite3.Connection) -> dict | None:
        if not _is_corsia_agent(thread_id, db):
            return None
        return {"seconds_since_account": _seconds_since_account(db, _utcnow())}

    _guarded("first_turn_completed", read)


def note_decision_answered(node_id: str, option_id: str) -> None:
    """``first_decision_answered`` with the option letter, for the conductor's decision."""

    def read(db: sqlite3.Connection) -> dict | None:
        from plans import public

        owner_id = public.decision_owner_id(db, node_id=node_id)
        if owner_id is None or not _is_corsia_agent(owner_id, db):
            return None
        return {"option": str(option_id)[:8]}

    _guarded("first_decision_answered", read)


def note_close(operation: str, project_id: str | None) -> None:
    """``first_outcome`` on the first accepted close in a project made after the account."""
    if operation not in OUTCOME_OPERATIONS or not project_id:
        return

    def read(db: sqlite3.Connection) -> dict | None:
        project = db.execute(
            "SELECT created_at FROM projects WHERE id = ?", (project_id,),
        ).fetchone()
        started = _account_started_at(db)
        if project is None or started is None or _parse(project["created_at"]) < started:
            return None
        return {"operation": operation,
                "seconds_since_account": _seconds_since_account(db, _utcnow())}

    _guarded("first_outcome", read)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
