"""Settle a rule-started worker once, as one ``worker.result`` or ``worker.no_result`` (R24).

A started worker settles once its last turn has ended and nothing is queued for it. A
completed turn's final message is checked once against its workflow's declared result
(``core.workflow_result``): a payload that passes is ``returned``, one that fails is
``invalid`` (it keeps the request identity, design §4.2). A stopped or failed turn, or no
final message, returned nothing: ``no_result``, which releases the identity.

The event is admitted first, under an id the start fixes, so a settle a restart cut short
admits it again as a duplicate and then settles the row, which settles once
(``worker_starts.end``). The event goes on the request's own artifact, so rule 0 reaches
its registered owner; never to the rule owner as a delegated reply. When nothing usable
came back, the rule owner is woken once unless one of its own ``run_script`` rules took
the event. A reason the owner reads is classified; raw error text stays in the log.
Each settled start emits one product event, here and nowhere else.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from loguru import logger

from core.workflow_result import ResultInvalid, check_result
from events import worker_starts
from events.connectors import worker as worker_events
from events.rule_outcomes import INVALID, NO_RESULT, QUEUED, REFUSED, RETURNED
from infra.database import write_transaction
from infra.db_async import run_in_db
from infra.product_events import emit_product_event

#: The largest checked payload an event carries; intake's own limit for all facts is 64 KB.
MAX_RESULT = 48 * 1024
#: How a refusal reads in the owner's ``worker.no_result`` reason.
REFUSAL_PREFIX = "refused: "
NO_RESULT_NEXT = "The run ends without a result; a later event starts a new one."
_ENDED = ("completed", "cancelled", "error")
#: A turn error the owner may read as is: a short code, never free text.
_ERROR_CODE = re.compile(r"[A-Za-z][\w.:-]{0,63}\Z")


@dataclass(frozen=True)
class Settle:
    """One worker's settlement: its row's outcome and the event that reports it."""

    decision_id: int
    owner: str
    outcome: str
    event_type: str
    event_id: str
    facts: dict[str, Any]
    trace: tuple[str, ...]
    waited: bool = False  # whether the start waited for a free Claude Code slot
    staging_path: str | None = None


def last_turn(db: sqlite3.Connection, worker: str) -> Mapping[str, Any] | None:
    """The worker's last turn once it has ended and nothing waits to run, else ``None``."""
    if db.execute(
        "SELECT 1 FROM messages WHERE thread_id=? AND input_order IS NOT NULL "
        "AND delivery_state='queued' LIMIT 1", (worker,),
    ).fetchone() is not None:
        return None
    turn = db.execute(
        "SELECT id, status, error FROM active_turns WHERE thread_id=? "
        "ORDER BY turn_order DESC LIMIT 1", (worker,),
    ).fetchone()
    return turn if turn is not None and turn["status"] in _ENDED else None


def final_message(db: sqlite3.Connection, worker: str, turn_id: str) -> str | None:
    row = db.execute(
        "SELECT content FROM messages WHERE thread_id=? AND role='assistant' "
        "AND json_valid(metadata) AND json_extract(metadata,'$.turn_id')=? "
        "AND length(trim(content))>0 ORDER BY seq DESC, rowid DESC LIMIT 1",
        (worker, turn_id),
    ).fetchone()
    return None if row is None else str(row["content"])


def verdict(turn: Mapping[str, Any], text: str | None, schema: dict[str, Any],
            check: Mapping[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    """The start's outcome and the facts that say it, for one ended turn."""
    if turn["status"] == "cancelled":
        return NO_RESULT, {"reason": "the agent was stopped"}
    if turn["status"] != "completed":
        error = str(turn["error"] or "")
        if _ERROR_CODE.match(error):
            return NO_RESULT, {"reason": f"the agent's turn failed ({error})"}
        logger.warning("rule-started agent turn {} failed: {}", turn["id"], error)
        return NO_RESULT, {"reason": "the agent's turn failed"}
    if text is None:
        return NO_RESULT, {"reason": "the agent returned no final message"}
    try:
        result = check_result(text, schema)
        signs = None
        if check and check.get("result_context"):
            result = _validate_declared_result(result, check["result_context"])
            signs = result["injection_signs"]
            result = result["payload"]
        payload = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    except ResultInvalid as exc:
        return INVALID, {"reason": "invalid", "detail": str(exc)[:500]}
    except Exception:  # noqa: BLE001 - a CORSIA defect must release, not consume, the identity
        logger.exception("CORSIA could not validate the worker result")
        return NO_RESULT, {"reason": f"Couldn’t validate the workflow result. {NO_RESULT_NEXT}"}
    if len(payload.encode()) > MAX_RESULT:
        return INVALID, {"reason": "invalid", "detail": f"the result is over {MAX_RESULT} bytes"}
    return RETURNED, {"result": payload, **({"injection_signs": signs} if signs is not None else {})}


def _validate_declared_result(payload: dict, context: Mapping[str, Any]) -> dict:
    """The connector owns validation; identity never comes back from this call."""
    from events.connector_workflows import resolve_shipped_workflow

    resolved = resolve_shipped_workflow(context["origin"], context["name"])
    if resolved is None or resolved.error or context["record"] is None:
        raise RuntimeError("the shipped result contract or captured record is unavailable")
    return resolved.declaration.result.validate(payload, context["record"])


def ready(db: sqlite3.Connection, decision_ids: set[int] | None = None) -> list[Settle]:
    """Every started worker whose last turn has ended, with what settles it."""
    settles = []
    for row in worker_starts.with_workers(db):
        if decision_ids is not None and row["id"] not in decision_ids:
            continue
        turn = last_turn(db, row["worker_thread_id"])
        if turn is None:
            continue
        check = json.loads(row["run_input"] or "{}")
        outcome, said = verdict(turn, final_message(db, row["worker_thread_id"], turn["id"]),
                                check.get("schema") or {}, check)
        settles.append(settlement(row, check, outcome, said))
    return settles


def settlement(row: Mapping[str, Any], check: Mapping[str, Any], outcome: str,
               said: Mapping[str, Any]) -> Settle:
    """The event one start settles with: the envelope (source, item and version, workflow,
    run, validity) around the payload or the reason."""
    kind = worker_events.RESULT if outcome == RETURNED else worker_events.NO_RESULT
    context = check.get("result_context")
    facts = {
        worker_events.REQUESTER_FACT: row["thread_id"], "rule_id": row["rule_id"],
        "request_event": row["event_id"], "request_identity": row["request_identity"],
        "workflow": check.get("workflow"), "run_id": str(row["id"]),
        "source": f"{row['connector']}.{row['event_type']}", "item": row["artifact_external_id"],
        "item_version": check.get("item_version"), "artifact_integration": row["artifact_integration"],
        "artifact_type": row["artifact_type"], "valid": outcome == RETURNED, **said,
    }
    if context:
        facts.update(source=context["source"], kind=context["kind"], **context["item"],
                     injection_signs=said.get("injection_signs"))
    clean_facts = {k: v for k, v in facts.items() if v is not None or (context and k == "injection_signs")}
    return Settle(row["id"], row["thread_id"], outcome, f"{worker_events.NAMESPACE}.{kind}",
                  f"worker:{row['id']}:{kind}", clean_facts,
                  tuple(check.get("trace") or ()), bool(check.get("waited")),
                  context["staging_path"] if context else None)


async def settle(item: Settle) -> None:
    """Admit the start's event, then settle its row once, record it, and tell the owner if
    nothing came back."""
    from events.intake import admit_event

    admitted = await admit_event(event_type=item.event_type, facts=item.facts, origin="platform",
                                 producer=worker_events.NAMESPACE, event_id=item.event_id,
                                 trace=item.trace)
    reason = item.facts.get("reason")
    if admitted.refusal is not None:
        logger.error("worker start {}: its event was refused: {}", item.decision_id, admitted.refusal.code)
        reason = f"its result event was refused ({admitted.refusal.code})"
    settled = await run_in_db(_end, item, reason, admitted.refusal is not None)
    if settled is None:
        return
    outcome, woke = settled
    emit_product_event("event_rule", "start_settled", thread_id=item.owner, properties={
        "rule_id": item.facts.get("rule_id"), "workflow": item.facts.get("workflow"),
        "outcome": outcome, "waited_for_capacity": item.waited,
    })
    if woke:
        from automation import script_runner

        await script_runner._drive_owner(item.owner)  # the one owner drive after a model-free wake


def _end(item: Settle, reason: str | None, event_refused: bool,
         db: sqlite3.Connection | None = None) -> tuple[str, bool] | None:
    """Settle the row and, when nothing usable came back, wake its owner once: the outcome
    and whether it woke, or ``None`` when the row was already settled."""
    from events.delivery import enqueue_event_wake
    from events.worker_labels import labelled_summary

    outcome = NO_RESULT if event_refused and item.outcome != REFUSED else item.outcome
    # A refused start's row says only the cause: its outcome already reads "did not start".
    stored = reason.removeprefix(REFUSAL_PREFIX) if reason and outcome == REFUSED else reason
    with write_transaction(db, scope="worker_settle") as conn:
        if not worker_starts.end(conn, item.decision_id, outcome, stored):
            return None
        if outcome == RETURNED or _owner_took(conn, item):
            return outcome, False
        workflow = item.facts.get("workflow") or "a workflow"
        said = (f"A rule couldn’t start {workflow}: {stored}" if outcome == REFUSED
                else f"The agent a rule started for {workflow} returned no result: {reason}")
        enqueue_event_wake(
            item.owner, item.event_type, labelled_summary(said, item.facts),
            item.facts,
            message_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"corsia:{item.event_id}:wake")), db=conn,
        )
        return outcome, True


def _owner_took(conn: sqlite3.Connection, item: Settle) -> bool:
    """Whether one of the owner's own rules queued a run with the event (R24)."""
    return conn.execute(
        "SELECT 1 FROM event_route_decisions WHERE event_id=? AND thread_id=? AND outcome=? LIMIT 1",
        (item.event_id, item.owner, QUEUED),
    ).fetchone() is not None
