"""Persist and reconcile the app-path first-run opening before model selection."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Any

OPENING_REFERENCE = "skills/conductor-bootstrap/references/first-run-opening.md"
OPENING_PREFIX = "Hi {name}."
IMPORT_DECISION_TITLE = "Bring over what your other tools know"


def opening_message(name: str | None = None) -> str:
    """Return the packaged opening, with its optional person-facing prefix."""
    from infra.system_package import system_resource_source

    text = system_resource_source(OPENING_REFERENCE).read_text(encoding="utf-8").strip()
    return f"{OPENING_PREFIX.format(name=name)}\n\n{text}" if name else text


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
    """Whether setup statically found importable context before model selection.

    The consent itself must wait for the selected model, because it names where the
    person’s data goes. The pre-pick plan can nevertheless show that this step
    exists from the stat-only discovery result.
    """
    agents_found = facts.get("agents_found")
    if isinstance(agents_found, (list, tuple, set, frozenset)) and any(
        isinstance(tool, str) and tool for tool in agents_found
    ):
        return True
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
            1, "The other subscription",
            f"the {product} subscription is connected, or the user chose not now",
            vendor=vendor,
        ))
    if _has_import_step(facts):
        steps.append(_step(
            2, "Bring over from your other tools",
            "the user chose whether to bring over context from their other tools",
        ))
    if connected_app_count < 2:
        steps.append(_step(
            3, "Where your work lives",
            "the user named the apps where their work lives and every app child is settled, or chose not now",
        ))
    if facts.get("web_search_ready") is not True:
        steps.append(_step(
            4, "Web search",
            "web search works, or the user chose not now",
        ))
    steps.extend((
        _step(
            5, "About you",
            "CORSIA knows enough about the user to help, or the user chose not now",
        ),
        *(() if email_calendar_ready else (_step(
            6, "Email and calendar",
            "the user chose how to connect email and calendar, or chose not now",
        ),)),
        _step(
            7, "How CORSIA gets better",
            "the user chose whether to use or share community improvements, or chose not now",
        ),
        _step(
            8, "Your files, and when agents ask first",
            "the user has been told how file access and approval work",
        ),
        _step(
            9, "The hand-off",
            "the user has the final setup question and knows what CORSIA can take off their plate",
        ),
    ))
    return tuple(steps)


def _connected_app_facts(db: sqlite3.Connection | None = None) -> dict[str, Any]:
    """Read local, already-authorized app/account state without a provider call."""
    from connections.app_reach import list_apps
    from infra.database import get_db

    connection = db or get_db()
    try:
        # ``list_apps`` is the canonical state projection: pending, expired, and
        # disconnected entries remain visible there but are not usable connections.
        count = sum(app.get("state") == "connected" for app in list_apps(db=connection))
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
        raise PermissionError("setup does not belong to this signed-in user")

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
            # the plan reconciliation and cached snapshot.
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
    existing = {
        (row["title"], row["description"])
        for row in db.execute(
            "SELECT title, description FROM plan_nodes WHERE plan_id=?", (listing["plan"]["id"],)
        )
    }
    desired = {
        (step["title"], f"first-run-app:{step['step_number']}:{step.get('vendor', 'common')}")
        for step in descriptors
    }
    for index, step in enumerate(descriptors):
        key = f"first-run-app:{step['step_number']}:{step.get('vendor', 'common')}"
        identity = (step["title"], key)
        if identity in existing:
            continue
        public.add_node(
            listing["plan"]["id"],
            kind="phase" if step["step_number"] == 3 else "task",
            title=step["title"],
            acceptance_criteria=step["acceptance_criteria"],
            description=key,
            order_key=step["step_number"] * 10 + index,
            provenance="onboarding.app_first_run",
            db=db,
        )
    if reconcile:
        nodes = db.execute(
            "SELECT id, title, description, state, archived_at, revision FROM plan_nodes WHERE plan_id=?",
            (listing["plan"]["id"],),
        )
        for node in nodes:
            description = node["description"]
            identity = (node["title"], description)
            if (
                isinstance(description, str)
                and description.startswith("first-run-app:")
                and identity not in desired
                and node["state"] == "pending"
                and node["archived_at"] is None
            ):
                public.mutate(
                    listing["plan"]["id"], "archive_node",
                    {"node_id": node["id"], "expected_revision": node["revision"]},
                    actor=actor, provenance="onboarding.app_first_run",
                    mutation_id=f"first-run-app-reconcile:{conductor_id}:{node['id']}", db=db,
                )
        for order_key, step in enumerate(descriptors):
            description = f"first-run-app:{step['step_number']}:{step.get('vendor', 'common')}"
            node = db.execute(
                "SELECT id, revision, order_key FROM plan_nodes "
                "WHERE plan_id=? AND title=? AND description=? AND archived_at IS NULL",
                (listing["plan"]["id"], step["title"], description),
            ).fetchone()
            if node is not None and node["order_key"] != order_key:
                public.mutate(
                    listing["plan"]["id"], "reorder",
                    {"node_id": node["id"], "order_key": order_key, "expected_revision": node["revision"]},
                    actor=actor, provenance="onboarding.app_first_run",
                    mutation_id=f"first-run-app-order:{conductor_id}:{node['id']}:{node['revision']}", db=db,
                )


def _pre_pick_facts(*, db: sqlite3.Connection) -> dict[str, Any]:
    """The stat-only facts safe to use before a model can receive any import."""
    from onboarding import agent_import
    from onboarding.project_discovery import Roots

    return {
        "agents_found": agent_import.found_tools(Roots.for_user()),
        **_connected_app_facts(db=db),
    }


def seed_initial(
    conductor_id: str, user_id: str, *, db: sqlite3.Connection, opening_name: str | None = None,
) -> None:
    """Persist the visible greeting and only the facts-safe pre-pick setup tasks."""
    from infra.queries import insert_message

    opening_id = f"first-run-opening:{conductor_id}"
    wake_id = _first_run_message_id(conductor_id)
    prior = db.execute(
        "SELECT 1 FROM messages WHERE thread_id=? AND NOT (id=? OR (id=? AND delivery_state='queued')) LIMIT 1",
        (conductor_id, opening_id, wake_id),
    ).fetchone()
    if prior is not None:
        # A restored/reused conductor already has genuine history. It keeps that
        # history rather than receiving a new scripted greeting or setup plan.
        return
    metadata = {
        "for_user": True,
        "first_run_opening": True,
        # This server-authored opening predates the first model Turn. The
        # portable-history writer's existing unbound semantic-row contract
        # preserves it as a stateless scripted segment instead of inventing a Turn.
        "turn_unbound_reason": "server_scripted",
    }
    if db.execute("SELECT 1 FROM messages WHERE id=?", (opening_id,)).fetchone() is None:
        insert_message(
            conductor_id, "assistant", opening_message(opening_name), metadata,
            msg_id=opening_id, db=db,
        )
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
    """Reconcile final tasks and cache their shared snapshot on the held wake."""
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
