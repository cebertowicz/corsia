"""Agent-facing edits for the durable continuity that survives compaction."""

from __future__ import annotations

import json
from functools import partial
from typing import Any

from core.continuity_types import ENTRY_FIELDS, EVIDENCE_REQUIRED_FIELDS
from core import lesson_gate
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
import tools.continuity_learning as continuity_learning
import tools.continuity_runtime as continuity_runtime
from tools.base import Tool
from tools.continuity_place import _place, _place_error, _queued, _trial_text
from tools.continuity_schema import (
    _FIELD_SCHEMAS,
    _OPERATIONS,
    VERDICTS,
    body_errors,
    field_body_rules,
    operation_rules,
)


def _live_operation(db, thread_id: str, operation: str, params: dict[str, Any]):
    learning = continuity_learning.live_operation(db, thread_id, operation, params)
    if learning is not None:
        return learning
    epoch = continuity_runtime.epoch(db, thread_id)
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


def _result_text(result: dict[str, Any], *, staged: bool) -> str:
    learning = continuity_learning.result_text(result)
    if learning is not None:
        return learning
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
    if operation == "place" and result["verdict"] == "written" and "placement" in result:
        return f"Entry {entry_id} queued; CORSIA writes the lesson line and retires the entry."
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


def _run(
    thread_id: str,
    operation: str,
    params: dict[str, Any],
    checkpoint_id: str | None = None,
) -> str:
    """Apply one transaction, returning individual refusals as tool results."""
    if operation == "add" and params.get("field") == "next_actions":
        return continuity_runtime.next_actions_refusal(checkpoint_id)
    db = get_db()
    try:
        if checkpoint_id and operation in {"place", "triage", "outcome"}:
            return f"Error: {operation} records an in-turn fact; use it outside a checkpoint"
        if not checkpoint_id and operation == "review":
            return "Error: review runs only inside a checkpoint"
        gate = None
        if operation == "place":  # read first; a refusal is its own short write
            gate = lesson_gate.check_place(db, thread_id, continuity_runtime.epoch(db, thread_id), params)
            if gate.refusal is not None:
                lesson_gate.record_refusal(db, thread_id, str(params["verdict"]), gate)
                return gate.refusal
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
                if gate is not None:
                    lesson_gate.record_accepted(connection, thread_id, str(params["verdict"]), gate)
        return _result_text(result, staged=bool(checkpoint_id))
    except (ContinuityError, ValueError) as exc:
        return f"Error: {exc}"
    finally:
        db.close()


def _execution_error(operation: Any, params: dict[str, Any]) -> str | None:
    if operation not in _OPERATIONS:
        return f"Error: unknown operation {operation!r}"
    learning = continuity_learning.execution_error(operation, params)
    if learning is not None:
        return learning
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
        assert isinstance(operation, str)
        context = continuity_runtime.require_context(kwargs)
        if isinstance(context, str):
            return context
        thread_id, _ = context
        params = {key: value for key, value in kwargs.items() if not key.startswith("_")}
        checkpoint_id = kwargs.get("_checkpoint_id") or current_checkpoint_id()
        result = await run_in_db(
            partial(_run, thread_id, operation, params, checkpoint_id)
        )
        if operation == "place" and _queued(params) and not result.startswith("Error"):
            from core import placement_worker

            placement_worker.poke()
        return result
