"""Delivery helpers for claimed document-feedback batches."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from doceditor import comment_recipients, image_regions
from infra.passive_changes import append_thread_context_note
from infra.send_types import SendRejected
from infra.thread_inputs import thread_admission_state
from llm.resolution import ModelUnavailableError, resolve_execution_target
from plans.public import (
    create_feedback_node,
    feedback_display,
    feedback_item_counts,
    load_feedback_items,
)
from registry import models as registry_models

DeliveryOutcome = Literal["node", "ui", "empty"]


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """The durable outcome of one claimed batch-delivery attempt."""

    outcome: DeliveryOutcome | None = None
    node_id: str | None = None
    reason: str | None = None
    woken_threads: tuple[str, ...] = ()


def _value(batch: Any, key: str) -> Any:
    return batch[key] if hasattr(batch, "keys") else getattr(batch, key)


def recipient_problem(db, recipient_id: str) -> str | None:
    """Single admission check shared by feedback delivery and review requests."""
    if not recipient_id:
        return "no_owner"
    try:
        row = thread_admission_state(db, recipient_id)
    except SendRejected as exc:
        detail = exc.detail.lower()
        if "archiv" in detail:
            return "owner_archived"
        if "purg" in detail or "delet" in detail or exc.status_code == 404:
            return "owner_unavailable"
        return "owner_admission_refused"
    model = str(row["model"] or "")
    if not model:
        return None
    try:
        target = resolve_execution_target(model, db=db)
    except (ModelUnavailableError, ValueError):
        return "owner_unaddressable"
    if not target.available:
        return "owner_unaddressable"
    return None


def _count_phrases(items: list[dict[str, Any]]) -> tuple[str, str]:
    edits, ordinary, reopened = feedback_item_counts(items)
    parts = []
    if edits:
        parts.append(f"{edits} edit{'s' if edits != 1 else ''}")
    if ordinary:
        parts.append(f"{ordinary} comment{'s' if ordinary != 1 else ''}")
    if reopened:
        parts.append(f"{reopened} re-opened comment{'s' if reopened != 1 else ''}")
    return ", ".join(parts), " and ".join(parts)


def queued_note(
    db,
    recipient_id: str,
    batch: Any,
    *,
    items: list[dict[str, Any]] | None = None,
) -> None:
    """Append the fixed non-preemptive recipient note for one delivered batch."""
    loaded = items if items is not None else load_feedback_items(db, batch)
    author, document = feedback_display(db, batch, loaded)
    queued_phrase, _ = _count_phrases(loaded)
    append_thread_context_note(
        db,
        thread_id=recipient_id,
        summary=(
            f"Feedback from {author} on {document} ({queued_phrase}) is queued in your plan; "
            "nothing to do now."
        ),
        details={"batch_id": _value(batch, "id"), "artifact_id": _value(batch, "artifact_id")},
        actor_thread_id=(
            str(_value(batch, "author_id"))
            if _value(batch, "author_kind") == "agent"
            else None
        ),
    )


def undelivered_note(
    db,
    batch: Any,
    reason: str,
    *,
    items: list[dict[str, Any]] | None = None,
) -> None:
    """Tell an agent author that its batch stayed visible and retryable."""
    if _value(batch, "author_kind") != "agent":
        return
    loaded = items if items is not None else load_feedback_items(db, batch)
    _, undelivered_phrase = _count_phrases(loaded)
    readable = reason.replace("_", " ")
    artifact_id = str(_value(batch, "artifact_id"))
    artifact = registry_models.get_artifact(artifact_id, db=db)
    document = (artifact.get("path") or artifact.get("title") or artifact_id) if artifact else artifact_id
    append_thread_context_note(
        db,
        thread_id=str(_value(batch, "author_id")),
        summary=(reason if reason.startswith("Comment not delivered: ") else
                 f"{undelivered_phrase} on {document} not delivered: {readable}. Retry with "
                 f'doc_feedback_send(artifact_id="{artifact_id}") once the owner can receive it.'),
        details={"batch_id": _value(batch, "id"), "reason": reason},
    )


def deliver_claimed(db, batch: Any) -> DeliveryResult:
    """Deliver a batch already conditionally claimed by its state owner."""
    items = load_feedback_items(db, batch)
    if not items:
        return DeliveryResult(outcome="empty")
    recipient = str(_value(batch, "recipient_id"))
    if recipient == "ui":
        return DeliveryResult(outcome="ui")
    artifact = registry_models.get_artifact(str(_value(batch, "artifact_id")), db=db)
    before_filter = items
    items = comment_recipients.filter_left_replies(
        db, items, recipient,
        person_owner=_value(batch, "author_kind") == "user" and bool(artifact)
        and artifact.get("parent_thread_id") == recipient,
    )
    removed = [item for item in before_filter if item not in items]
    for item in removed:
        db.execute("DELETE FROM doc_feedback_batch_items WHERE batch_id=? AND comment_id=?",
                   (_value(batch, "id"), item["id"]))
    if removed:
        db.execute("UPDATE doc_feedback_batches SET item_count=item_count-? WHERE id=?",
                   (len(removed), _value(batch, "id")))
    if not items:
        return DeliveryResult(outcome="empty")
    problem = recipient_problem(db, recipient)
    if problem is None and _value(batch, "author_kind") == "agent":
        sender = db.execute("SELECT id,local_only,project_id FROM threads WHERE id=?",
                            (_value(batch, "author_id"),)).fetchone()
        owner_id = artifact.get("parent_thread_id") if artifact else None
        refused = []
        refusal = None
        for item in items:
            if item.get("kind") != "comment":
                continue
            item_refusal, legacy_exempt = comment_recipients.locality_disposition(
                db, item, recipient, sender, owner_id,
            )
            if item_refusal and legacy_exempt:
                item["suppress_tag"] = True
            elif item_refusal:
                refusal = item_refusal
                refused.append(item)
        if refused:
            remaining = [item for item in items if item not in refused]
            document = (artifact.get("path") or artifact.get("title") or str(_value(batch, "artifact_id"))) if artifact else str(_value(batch, "artifact_id"))
            comment_ids = ", ".join(str(item["id"]) for item in refused)
            problem = f"Comment not delivered: {document}, comment {comment_ids}. {refusal}"
            if remaining:
                undelivered_note(db, batch, problem, items=refused)
                for item in refused:
                    db.execute("DELETE FROM doc_feedback_batch_items WHERE batch_id=? AND comment_id=?",
                               (_value(batch, "id"), item["id"]))
                db.execute("UPDATE doc_feedback_batches SET item_count=item_count-? WHERE id=?",
                           (len(refused), _value(batch, "id")))
                items = remaining
                problem = None
    if problem is not None:
        undelivered_note(db, batch, problem, items=items)
        return DeliveryResult(reason=problem)
    image_regions.annotate_delivery(
        db, items, path=artifact["path"] if artifact else None, reader_thread_id=recipient,
        batch_id=str(_value(batch, "id")),
    )
    comment_recipients.prepare_notification(db, items, recipient)
    created = create_feedback_node(db, recipient, batch, items=items)
    # A reply that joined the task its owner is working woke that owner; it is not queued.
    if not created.woke:
        queued_note(db, recipient, batch, items=items)
    return DeliveryResult(
        outcome="node",
        node_id=created.node_id,
        woken_threads=created.selected_threads,
    )
