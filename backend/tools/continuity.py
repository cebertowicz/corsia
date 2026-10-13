"""Agent-facing edits for the durable continuity that survives compaction."""

from __future__ import annotations

import json
from functools import partial
from typing import Any

from core.continuity_types import ENTRY_FIELDS, EVIDENCE_REQUIRED_FIELDS
from infra.continuity_staging import current_checkpoint_id, stage_edit
from infra import continuity_store
from infra.continuity_store import (
    ContinuityError,
    active_entry_snapshot,
    add_entry,
    check_field_match,
    replace_entry,
    retire_entry,
    store_revision,
)
from infra.database import get_db, write_transaction
from infra.db_async import run_in_db
from tools.base import Tool
from tools.continuity_schema import (
    _FIELD_SCHEMAS,
    _OPERATIONS,
    VERDICTS,
    body_errors,
    field_body_rules,
    operation_rules,
)


def _require_context(kwargs: dict[str, Any]):
    from tools.delegation import _require_context as require_context

    return require_context(kwargs)


def _epoch(db, thread_id: str) -> int:
    row = db.execute(
        "SELECT history_epoch FROM threads WHERE id=?", (thread_id,)
    ).fetchone()
    if row is None:
        raise ContinuityError(f"thread {thread_id} does not exist")
    return int(row["history_epoch"])


def _live_operation(db, thread_id: str, operation: str, params: dict[str, Any]):
    epoch = _epoch(db, thread_id)
    field = params.get("field")
    if operation == "inspect":
        entry_id = params.get("entry_id")
        if entry_id is not None:
            entries = {
                int(entry["entry_id"]): dict(entry)
                for entry in active_entry_snapshot(db, thread_id, epoch)
            }
            target = entries.get(int(entry_id))
            if target is None:
                raise ContinuityError(f"continuity entry {entry_id} does not exist")
            if field is not None:
                check_field_match(field, int(entry_id), str(target["field"]))
            return {
                "revision": store_revision(db, thread_id, epoch),
                "entry": {
                    "entry_id": int(entry_id),
                    "field": str(target["field"]),
                    **dict(target["body"]),
                },
            }
        # Through the module, so a patch of the store's name is seen (#2815).
        entries_by_field = continuity_store.active_entries(db, thread_id, epoch)
        if field is not None:
            entries_by_field = {field: entries_by_field[field]}
        return {
            "revision": store_revision(db, thread_id, epoch),
            "entries": entries_by_field,
        }
    if operation == "add":
        entry_id = add_entry(
            db,
            thread_id=thread_id,
            history_epoch=epoch,
            field=params["field"],
            body=params["body"],
        )
        return {"operation": operation, "entry_id": entry_id, "field": params["field"]}
    if operation == "place":
        return _place(db, thread_id, epoch, params)
    if operation == "replace":
        replace_entry(
            db,
            thread_id=thread_id,
            history_epoch=epoch,
            entry_id=params["entry_id"],
            body=params["body"],
            expected_field=field,
        )
    else:
        retire_entry(
            db,
            thread_id=thread_id,
            history_epoch=epoch,
            entry_id=params["entry_id"],
            reason=params["reason"],
            source_ref=params.get("source_ref"),
            expected_field=field,
        )
    return {"operation": operation, "entry_id": params["entry_id"]}


def _place(db, thread_id: str, epoch: int, params: dict[str, Any]):
    """Mark a lesson the agent placed in-turn, so no checkpoint re-reviews it.

    ``written`` and ``already`` retire the entry only once the destination is
    read and found to carry the quoted line (#2479). ``proposed`` hands the
    placement answer to the worker, which files the proposal on the project's
    intake route and marks the entry ``proposed`` itself. ``trial`` hands it
    the same way: the worker writes the trial skill and opens its review.
    """
    from core.placement_receipt import verify
    from infra.placement_marks import current_entry, log_outcome, set_mark

    entry_id = int(params["entry_id"])
    verdict = str(params["verdict"])
    if verdict in _QUEUED_VERDICTS:
        return _queue(db, thread_id, epoch, entry_id, params["placement"], verdict)
    destination = verify(db, thread_id, params["destination"], params["quote"])
    retire_entry(
        db, thread_id=thread_id, history_epoch=epoch, entry_id=entry_id,
        reason=f"placed at {destination}", source_ref=f"placed:{destination}",
    )
    revision = int(current_entry(db, thread_id, epoch, entry_id)["revision"])
    set_mark(
        db, thread_id=thread_id, history_epoch=epoch, entry_id=entry_id,
        state=verdict, revision=revision, detail=destination,
    )
    log_outcome(
        thread_id=thread_id, entry_id=entry_id, revision=revision,
        outcome=verdict, reason="placed-in-turn", destination=destination,
    )
    return {"operation": "place", "entry_id": entry_id, "verdict": verdict,
            "destination": destination}


#: The ``place`` verdicts the worker carries out, and the destination each takes.
_QUEUED_VERDICTS = {"proposed": "proposal", "trial": "trial"}


def _queue(db, thread_id: str, epoch: int, entry_id: int, placement: Any, verdict: str):
    """Select the entry with its answer, as a checkpoint review would (#2479).

    The worker's queue is the ``selected`` mark, so an in-turn proposal or
    trial takes the same route as a checkpoint's: checked here, carried out by
    the worker.
    """
    from core.placement_answer import stage
    from infra.placement_marks import (
        OUTSTANDING_CAP,
        current_entry,
        log_outcome,
        outstanding_count,
        set_mark,
    )

    live = current_entry(db, thread_id, epoch, entry_id)
    if live is None or live["state"] != "active":
        raise ContinuityError(f"continuity entry {entry_id} is not active")
    entry = {"entry_id": entry_id, "history_epoch": epoch, "body": json.loads(live["body"])}
    stored = stage(db, thread_id, entry, placement, None, require=_QUEUED_VERDICTS[verdict])
    if outstanding_count(db) >= OUTSTANDING_CAP:
        raise ContinuityError(
            "the placement queue is full; keep the entry, and a later "
            "checkpoint review offers it again"
        )
    revision = int(live["revision"])
    set_mark(
        db, thread_id=thread_id, history_epoch=epoch, entry_id=entry_id,
        state="selected", revision=revision,
        detail=json.dumps(
            {"checkpoint": None, "answer": stored},
            ensure_ascii=False, sort_keys=True,
        ),
    )
    log_outcome(
        thread_id=thread_id, entry_id=entry_id, revision=revision,
        outcome="selected", reason=f"{verdict}-in-turn",
    )
    return {"operation": "place", "entry_id": entry_id, "verdict": verdict,
            "hold": stored.get("hold"), "placement": stored["placement"]}


def _trial_text(result: dict[str, Any], *, staged: bool) -> str:
    """What a trial answer becomes, and for a held one the exact way to offer it."""
    from core.lesson_admission import FIXES
    from core.lesson_files import HOLD_REASONS

    entry_id, hold = result["entry_id"], result.get("hold")
    lead = f"Staged entry {entry_id} verdict select" if staged else f"Entry {entry_id} queued"
    if hold is None:
        answer = result["placement"]
        return (
            f"{lead}: CORSIA writes the trial skill {answer['skill_name']}, opens your "
            f"review task for {answer['trial']['review_after'][:10]} and retires the "
            "entry. It is not in this conversation's skills list; it appears when the "
            "list is next rebuilt."
        )
    return (
        f"{lead} as a captured hypothesis, not offered as guidance: {HOLD_REASONS[hold]}. "
        "CORSIA keeps it in a file with a review task and the entry stays. To offer "
        f"it, {FIXES[hold]}, then call place with verdict trial for entry {entry_id}."
    )


def _result_text(result: dict[str, Any], *, staged: bool) -> str:
    if "entries" in result or "entry" in result:
        return json.dumps(result, indent=2, sort_keys=True)
    operation = result["operation"]
    entry_id = result["entry_id"]
    if operation == "review" and result.get("hold"):
        return _trial_text(result, staged=True)
    if operation == "review":
        return f"Staged entry {entry_id} verdict {result['verdict']}."
    if operation == "place" and result["verdict"] == "trial":
        return _trial_text(result, staged=False)
    if operation == "place" and result["verdict"] == "proposed":
        return (
            f"Entry {entry_id} queued; CORSIA files the proposal and marks "
            "it proposed."
        )
    if operation == "place":
        return (
            f"Entry {entry_id} marked {result['verdict']} at "
            f"{result['destination']} and retired."
        )
    if staged:
        if operation == "add":
            return f"Staged as entry {entry_id} in {result['field']}."
        noun = "replacement" if operation == "replace" else "retirement"
        return f"Staged entry {entry_id} {noun}."
    if operation == "add":
        return f"Recorded as entry {entry_id} in {result['field']}."
    verb = "replaced" if operation == "replace" else "retired"
    return f"Entry {entry_id} {verb}."


_NEXT_ACTIONS_REFUSAL = "Error: next_actions takes no new entries. {} Drop a finished one here."
_NEXT_STEP_HOME = (  # (turn, checkpoint): a checkpoint cannot reach the plan
    "A next step belongs on the plan: use plan_update to add or change it.",
    "Name the step in progress and the exact next action in your narrative.",
)


def _run(
    thread_id: str,
    operation: str,
    params: dict[str, Any],
    checkpoint_id: str | None = None,
) -> str:
    """Apply one transaction, returning individual refusals as tool results."""
    if operation == "add" and params.get("field") == "next_actions":
        return _NEXT_ACTIONS_REFUSAL.format(_NEXT_STEP_HOME[bool(checkpoint_id)])
    db = get_db()
    try:
        if checkpoint_id and operation == "place":
            return "Error: place records an in-turn placement; use review here"
        if not checkpoint_id and operation == "review":
            return "Error: review runs only inside a checkpoint"
        with write_transaction(db, scope="continuity_edit") as connection:
            if checkpoint_id:
                result = stage_edit(
                    connection,
                    checkpoint_id=checkpoint_id,
                    thread_id=thread_id,
                    operation=operation,
                    params=params,
                )
            else:
                result = _live_operation(connection, thread_id, operation, params)
        return _result_text(result, staged=bool(checkpoint_id))
    except ContinuityError as exc:
        return f"Error: {exc}"
    finally:
        db.close()


def _execution_error(operation: Any, params: dict[str, Any]) -> str | None:
    if operation not in _OPERATIONS:
        return f"Error: unknown operation {operation!r}"
    if operation == "add" and not params.get("field"):
        return "Error: add requires field"
    if operation == "inspect" and params.get("entry_id") is not None:
        entry_id = params["entry_id"]
        if isinstance(entry_id, bool) or not isinstance(entry_id, int) or entry_id < 1:
            return "Error: inspect entry_id must be a positive integer"
    if operation in {"replace", "drop", "review", "place"} and params.get(
        "entry_id"
    ) is None:
        return f"Error: {operation} requires entry_id"
    if operation in VERDICTS and params.get("verdict") not in VERDICTS[operation]:
        return (
            f"Error: {operation} verdict must be one of "
            + ", ".join(VERDICTS[operation])
        )
    if operation == "place":
        return _place_error(params)
    if operation in {"add", "replace"} and not isinstance(params.get("body"), dict):
        return f"Error: {operation} requires body"
    if operation == "drop" and not str(params.get("reason") or "").strip():
        return "Error: drop requires reason"
    return None


def _place_error(params: dict[str, Any]) -> str | None:
    """``written``/``already`` name the file and its line; the others the answer."""
    if params["verdict"] in _QUEUED_VERDICTS:
        if params.get("destination") is not None or params.get("quote") is not None:
            return "Error: destination and quote go only with verdict written or already"
        if params.get("placement") is None:
            return f"Error: verdict {params['verdict']} requires placement"
        return None
    if params.get("placement") is not None:
        return "Error: placement goes only with verdict proposed or trial"
    if not str(params.get("destination") or "").strip():
        return "Error: place requires destination"
    if not str(params.get("quote") or "").strip():
        return "Error: place requires quote, the line in destination that carries the lesson"
    return None


class ContinuityEditTool(Tool):
    """Record and retire the continuity that survives compaction."""

    @property
    def name(self) -> str:
        return "continuity_edit"

    @property
    def description(self) -> str:
        return (
            "Record and retire this thread's durable continuity — the state that "
            "survives compaction. Call this as work happens: record a constraint, "
            "approval, or decision when made; drop it with a reason when it stops "
            "applying. Inspect first rather than restating existing entries. Do not "
            "record transient work-in-progress; the structured plan owns live task "
            f"state. Fields: {', '.join(ENTRY_FIELDS)}. Evidence is required when "
            "retiring: " + ", ".join(sorted(EVIDENCE_REQUIRED_FIELDS)) + "."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": dict(_FIELD_SCHEMAS),
            "required": ["operation"],
            "allOf": operation_rules() + field_body_rules(),
        }

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        """Add the one check the advertised schema cannot enforce in-product.

        Which parameters an operation takes, and which body keys a named field
        needs, are conditional clauses in :attr:`parameters` that both
        :meth:`Tool._validate` and an ordinary JSON Schema validator apply.
        What neither applies is the body's ``oneOf``, because
        :meth:`Tool._validate` does not implement that keyword -- so the body
        goes to the store's own validator rather than to a restatement of it.
        A body a model sent as JSON text is already decoded by the registry
        (``Tool.decode_string_arguments``, #1653).
        """
        errors = super().validate_params(params)
        operation = params.get("operation")
        # A body that is not an object is already refused by its schema type.
        if operation not in _OPERATIONS or not isinstance(params.get("body"), (dict, type(None))):
            return errors
        return errors + body_errors(str(operation), params)

    async def execute(self, **kwargs: Any) -> str:
        operation = kwargs.get("operation")
        error = _execution_error(operation, kwargs)
        if error is not None:
            return error
        context = _require_context(kwargs)
        if isinstance(context, str):
            return context
        thread_id, _ = context
        params = {key: value for key, value in kwargs.items() if not key.startswith("_")}
        checkpoint_id = kwargs.get("_checkpoint_id") or current_checkpoint_id()
        result = await run_in_db(
            partial(_run, thread_id, operation, params, checkpoint_id)
        )
        if params.get("verdict") in _QUEUED_VERDICTS and not result.startswith("Error"):
            from core import placement_worker

            placement_worker.poke()
        return result
