"""Own the app-path landing plan and cached setup facts."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Any

from onboarding.setup_strings import AGENT_IMPORT

# The import decision is identified by its rendered title, so it is read from the one string source.
IMPORT_DECISION_TITLE = AGENT_IMPORT["title"]


class SetupOwnerError(Exception):
    """The signed-in person does not own the still-held app setup."""


def _is_unavailable(value: object) -> bool:
    return isinstance(value, Mapping) and "unavailable" in value


def _paid_missing_sources(facts: Mapping[str, Any]) -> tuple[str, ...]:
    missing = facts.get("sources_missing")
    paid = facts.get("pays_for")
    if _is_unavailable(missing) or _is_unavailable(paid):
        return ()
    if not isinstance(missing, Mapping) or not isinstance(paid, (list, tuple, set, frozenset)):
        return ()
    paid_sources = {value for value in paid if isinstance(value, str)}
    return tuple(source for source in ("claude_code", "codex") if source in paid_sources and source in missing)


def _has_import_step(facts: Mapping[str, Any]) -> bool:
    """Whether import applies at this setup phase.

    Before a model is bound, stat-only discovery makes the provisional plan visible.
    Once a model is bound, only its provider-specific import decision card can keep
    the step applicable; an unavailable setup read keeps the provisional answer, so
    a failed read never removes the step before the agent's live read settles it.
    """
    if "bound_model" not in facts or _is_unavailable(facts.get("setup")):
        agents_found = facts.get("agents_found")
        return isinstance(agents_found, (list, tuple, set, frozenset)) and any(
            isinstance(tool, str) and tool for tool in agents_found
        )
    setup = facts.get("setup")
    if not isinstance(setup, Mapping):
        return False
    decisions = setup.get("setup_decisions")
    return isinstance(decisions, list) and any(
        isinstance(decision, Mapping) and decision.get("title") == IMPORT_DECISION_TITLE
        for decision in decisions
    )


def _step(step_number: int, title: str, acceptance_criteria: str, *, vendor: str | None = None) -> dict[str, Any]:
    step = {
        "step_number": step_number,
        "title": title,
        "acceptance_criteria": acceptance_criteria,
    }
    if vendor is not None:
        step["vendor"] = vendor
    return step


def applicable_steps(
    facts: Mapping[str, Any], *, connected_app_count: int | None = None,
    email_calendar_ready: bool | None = None,
) -> tuple[dict[str, Any], ...]:
    """Return the app-path's applicable setup tasks in first-run-app.md order.

    This is deliberately pure: callers supply the facts snapshot that the first
    instruction, plan, and completion all share. ``None`` means use the values
    persisted in that snapshot, with conservative defaults when unavailable.
    """
    if connected_app_count is None:
        value = facts.get("connected_app_count")
        connected_app_count = value if isinstance(value, int) and value >= 0 else 0
    if email_calendar_ready is None:
        email_calendar_ready = facts.get("email_calendar_ready") is True

    steps: list[dict[str, Any]] = []
    for vendor in _paid_missing_sources(facts):
        product = "Claude" if vendor == "claude_code" else "ChatGPT"
        steps.append(_step(
            1, "Add your second subscription",
            f"you connected your {product} subscription, or chose not now",
            vendor=vendor,
        ))
    if _has_import_step(facts):
        steps.append(_step(
            2, "Bring over what your other tools know",
            "you chose whether to bring over what your other tools know",
        ))
    if connected_app_count < 2:
        steps.append(_step(
            3, "Where your work lives",
            "you named the apps where you work and each connection is finished or skipped, or you chose not now",
        ))
    if facts.get("web_search_ready") is not True:
        steps.append(_step(
            4, "Web search",
            "web search works, or you chose not now",
        ))
    steps.extend((
        _step(
            5, "About you",
            "CORSIA knows enough about you to help, or you chose not now",
        ),
        *(() if email_calendar_ready else (_step(
            6, "Email and calendar",
            "you chose how to connect email and calendar, or chose not now",
        ),)),
        _step(
            7, "How CORSIA gets better",
            "you chose whether to use or share community improvements, or chose not now",
        ),
        _step(
            8, "Your files, and when agents ask first",
            "CORSIA explained how file access and approval work",
        ),
        _step(
            9, "Hand over to your agents",
            "CORSIA asked what you'd like help with next",
        ),
    ))
    return tuple(steps)


def _connected_app_facts(db: sqlite3.Connection | None = None) -> dict[str, Any]:
    """Read local, already-authorized app/account state without a provider call."""
    from connections.app_reach import list_apps
    from infra.database import get_db

    connection = db or get_db()
    try:
        # ``list_apps`` is the canonical state projection. An app is already set up
        # once its row exists, approved or still awaiting the person's approval;
        # only a proposal whose review window lapsed (``expired``) does not count.
        count = sum(app.get("state") != "expired" for app in list_apps(db=connection))
        ready = False
        for row in connection.execute(
            "SELECT capabilities_json FROM connected_accounts WHERE state='connected'"
        ):
            try:
                capabilities = json.loads(row["capabilities_json"])
            except (TypeError, ValueError):
                continue
            if isinstance(capabilities, list) and {"mail", "calendar"}.issubset(capabilities):
                ready = True
                break
        return {"connected_app_count": count, "email_calendar_ready": ready}
    finally:
        if db is None:
            connection.close()


async def read_app_facts(model: str) -> dict[str, Any]:
    """Read one current app-path snapshot, including the local plan-skip facts."""
    from infra.db_async import run_in_db
    from onboarding import first_run_wake

    facts = await first_run_wake.setup_facts(model, "app")
    return {**facts, **await run_in_db(_connected_app_facts)}


def _held_app_conductor(*, db: sqlite3.Connection) -> str | None:
    """The conductor only while its queued first-run wake still names the app path."""
    from infra.corsia_agent import get_corsia_agent
    from infra.onboarding_events import held_first_run_path

    conductor_id = get_corsia_agent(db=db)
    return conductor_id if conductor_id and held_first_run_path(conductor_id, db=db) == "app" else None


async def complete_app_setup(
    user_id: str, *, step: str | None, local_ai: bool | None,
    picks: dict[str, bool] | None, handoff: dict[str, Any] | None,
) -> bool:
    """Complete and seed only a still-held app setup; otherwise leave its caller unchanged.

    A script/legacy setup, or an app wake already consumed by model selection, is
    not an app-seeding target. Returning ``False`` lets the setup route keep its
    normal progress-record update without creating a plan or reading app facts.
    """
    from infra import setup_progress
    from infra.database import get_db, write_transaction
    from infra.db_async import run_in_db

    conductor_id = await run_in_db(lambda: _held_app_conductor(db=get_db()))
    if conductor_id is None:
        return False
    if not await run_in_db(lambda: is_first_run_owner(user_id, db=get_db())):
        raise SetupOwnerError("setup does not belong to this signed-in user")

    # The current handoff/picks must be committed before their fact projection.
    await run_in_db(lambda: setup_progress.update(
        step=step, local_ai=local_ai, picks=picks, handoff=handoff,
    ))
    facts = await read_app_facts("")

    def finish() -> None:
        with write_transaction(None, scope="complete_app_setup") as db:
            setup_progress.update(completed=True, db=db)
            # Model selection can consume the wake while facts are being read.
            # Completion stays successful; only a still-held app wake receives
            # plan reconciliation and cached snapshot for the selected first turn.
            if _held_app_conductor(db=db) == conductor_id:
                cache_completed_facts(conductor_id, user_id, facts, db=db)

    await run_in_db(finish)
    return True


def _seed_descriptors(
    conductor_id: str, user_id: str, descriptors: tuple[dict[str, Any], ...], *, db: sqlite3.Connection,
    reconcile: bool = False,
) -> None:
    """Idempotently create the supplied visible tasks through the public plan writer."""
    from plans import public

    actor = public.PlanActor(conductor_id, user_id, None)
    listing = public.ensure_current_plan(actor=actor, include_folded=True, db=db)
    # The public listing omits workflow fields; scope these reads to the seeded workflow.
    existing = {
        row["workflow_step"] for row in db.execute(
            "SELECT workflow_step FROM plan_nodes WHERE plan_id=? AND workflow_ref='first-run-app'",
            (listing["plan"]["id"],),
        )
    }
    desired = {f"{step['step_number']}:{step.get('vendor', 'common')}" for step in descriptors}
    for index, step in enumerate(descriptors):
        workflow_step = f"{step['step_number']}:{step.get('vendor', 'common')}"
        if workflow_step in existing:
            continue
        public.add_node(
            listing["plan"]["id"],
            kind="phase" if step["step_number"] == 3 else "task",
            title=step["title"],
            acceptance_criteria=step["acceptance_criteria"],
            workflow_ref="first-run-app",
            workflow_step=workflow_step,
            difficulty="routine",
            order_key=step["step_number"] * 10 + index,
            provenance="onboarding.app_first_run",
            db=db,
        )
    if reconcile:
        nodes = db.execute(
            "SELECT id, workflow_step, state, archived_at, revision "
            "FROM plan_nodes WHERE plan_id=? AND workflow_ref='first-run-app'",
            (listing["plan"]["id"],),
        )
        for node in nodes:
            if (
                node["workflow_step"] not in desired
                and node["state"] == "pending" and node["archived_at"] is None
            ):
                public.mutate(
                    listing["plan"]["id"], "archive_node",
                    {"node_id": node["id"], "expected_revision": node["revision"]},
                    actor=actor, provenance="onboarding.app_first_run",
                    mutation_id=f"first-run-app-reconcile:{conductor_id}:{node['id']}", db=db,
                )
        next_open_position = 0
        for step in descriptors:
            workflow_step = f"{step['step_number']}:{step.get('vendor', 'common')}"
            node = db.execute(
                "SELECT id, revision, order_key FROM plan_nodes "
                "WHERE plan_id=? AND workflow_ref=? AND workflow_step=? "
                "AND archived_at IS NULL AND folded=0 "
                "AND state NOT IN ('accepted', 'accepted_with_followup', 'failed', 'closed')",
                (listing["plan"]["id"], "first-run-app", workflow_step),
            ).fetchone()
            if node is None:
                continue
            if node["order_key"] != next_open_position:
                public.mutate(
                    listing["plan"]["id"], "reorder",
                    {"node_id": node["id"], "order_key": next_open_position, "expected_revision": node["revision"]},
                    actor=actor, provenance="onboarding.app_first_run",
                    mutation_id=f"first-run-app-order:{conductor_id}:{node['id']}:{node['revision']}", db=db,
                )
            next_open_position += 1


def _pre_pick_facts(*, db: sqlite3.Connection) -> dict[str, Any]:
    """The stat-only facts safe to use before a model can receive any import."""
    from onboarding import agent_import
    from onboarding.project_discovery import Roots

    return {
        "agents_found": agent_import.found_tools(Roots.for_user()),
        **_connected_app_facts(db=db),
    }


def seed_initial(conductor_id: str, user_id: str, *, db: sqlite3.Connection) -> None:
    """Create the stat-safe landing plan without showing the scripted opening."""
    if db.execute("SELECT 1 FROM messages WHERE thread_id=? LIMIT 1", (conductor_id,)).fetchone() is not None:
        return
    initial = applicable_steps(_pre_pick_facts(db=db))
    _seed_descriptors(
        conductor_id, user_id,
        tuple(step for step in initial if step["step_number"] != 4),
        db=db,
    )


def _first_run_message_id(conductor_id: str) -> str:
    from infra.onboarding_events import first_run_message_id

    return first_run_message_id(conductor_id)


def cache_completed_facts(
    conductor_id: str, user_id: str, facts: Mapping[str, Any], *, db: sqlite3.Connection,
) -> None:
    """Reconcile final setup rows and cache their shared snapshot on the held wake."""
    _seed_descriptors(conductor_id, user_id, applicable_steps(facts), db=db, reconcile=True)
    row = db.execute(
        "SELECT metadata FROM messages WHERE id=? AND thread_id=? AND delivery_state='queued'",
        (_first_run_message_id(conductor_id), conductor_id),
    ).fetchone()
    if row is None:
        raise ValueError("first-run wake is unavailable for completed app setup")
    try:
        metadata = json.loads(row["metadata"] or "{}")
    except (TypeError, ValueError) as error:
        raise ValueError("first-run wake metadata is unreadable") from error
    if not isinstance(metadata, dict):
        raise ValueError("first-run wake metadata is unreadable")
    metadata["app_setup_facts"] = dict(facts)
    db.execute(
        "UPDATE messages SET metadata=? WHERE id=? AND thread_id=?",
        (json.dumps(metadata), _first_run_message_id(conductor_id), conductor_id),
    )


def reconcile_rendered_app_facts(conductor_id: str, facts: Mapping[str, Any]) -> None:
    """Reconcile a held app wake whose final setup snapshot was never cached."""
    from infra.database import write_transaction
    from infra.queries import get_thread

    with write_transaction(None, scope="reconcile_rendered_app_facts") as db:
        # Rendering can race with selection consuming the wake. Only repair the
        # still-held app first run, matching completion's existing guard.
        if _held_app_conductor(db=db) != conductor_id:
            return
        thread = get_thread(conductor_id, db=db)
        user_id = thread["user_id"] if thread is not None else None
        if not isinstance(user_id, str) or not user_id:
            return
        _seed_descriptors(
            conductor_id, user_id, applicable_steps(facts), db=db, reconcile=True,
        )


def read_cached_app_facts(
    thread_id: str, db: sqlite3.Connection | None = None,
) -> dict[str, Any] | None:
    """Return the completion snapshot cached on this thread's held first-run wake."""
    from infra.database import get_db

    connection = db or get_db()
    try:
        row = connection.execute(
            "SELECT metadata FROM messages WHERE id=? AND thread_id=?",
            (_first_run_message_id(thread_id), thread_id),
        ).fetchone()
        if row is None:
            return None
        try:
            metadata = json.loads(row["metadata"] or "{}")
        except (TypeError, ValueError):
            return None
        facts = metadata.get("app_setup_facts") if isinstance(metadata, dict) else None
        return dict(facts) if isinstance(facts, Mapping) else None
    finally:
        if db is None:
            connection.close()


def is_first_run_owner(user_id: str, *, db: sqlite3.Connection) -> bool:
    """Whether this signed-in person owns the sole app first-run conductor."""
    from infra.corsia_agent import get_corsia_agent
    from infra.auth import owns_thread

    conductor_id = get_corsia_agent(db=db)
    return bool(conductor_id) and owns_thread(conductor_id, user_id, db=db)
