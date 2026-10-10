"""Record owner-facing events for landed document writes.

Every landed write to an owned document by someone other than its owner is
reported here, once, from the ``WriteAuthority`` decided for that write: an
allowed edit (the conductor's, the user's) as ``content_edited_by_non_owner``,
and a write that landed although it was refused (a shell command) as
``ownership_bypass_write``. The reporter never decides again.
"""

from __future__ import annotations

from pathlib import Path

from doceditor import feedback_batches
from doceditor.document_scope import MAX_TEXT_BYTES
from infra.database import write_transaction
from registry import models
from registry.identity_bridge import OWNER_REGISTRATION_STATES
from registry.registration_events import _append_registration_event, _now
from registry.shared_document_messages import bypass_notice
from registry.shared_edit_classification import capped_unified_diff, classify_edit
from registry.shared_target import SharedTarget
from registry.shared_principals import UI_PRINCIPAL, WriteAuthority, owner_label

# A write whose prior bytes are unknown (a shell command) cannot be classified.
_UNCLASSIFIED = "unknown"


def _registration_owner(db, artifact_id: str):
    marks = ",".join("?" for _ in OWNER_REGISTRATION_STATES)
    return db.execute(
        f"""SELECT parent_thread_id,relationship_state FROM artifact_registrations
            WHERE artifact_id=? AND relationship_state IN ({marks})
            ORDER BY CASE relationship_state WHEN 'owner-pinned' THEN 0 ELSE 1 END,
                     updated_at DESC LIMIT 1""",
        (artifact_id, *OWNER_REGISTRATION_STATES),
    ).fetchone()


def _record_event(
    artifact: dict,
    *,
    path: Path,
    fallback_owner: str | None,
    event_type: str,
    actor_thread_id: str | None,
    reason: str,
    metadata: dict,
    summary: str,
    db,
    passive: bool = True,
) -> None:
    owner = _registration_owner(db, artifact["id"])
    owner_id = str(owner["parent_thread_id"]) if owner else fallback_owner
    state = str(owner["relationship_state"]) if owner else None
    _append_registration_event(
        db,
        artifact_id=artifact["id"],
        event_type=event_type,
        actor_thread_id=actor_thread_id,
        authority=None,
        reason=reason,
        source_path=str(path),
        affected_thread_id=owner_id,
        old_owner_thread_id=owner_id,
        new_owner_thread_id=owner_id,
        old_relationship_state=state,
        new_relationship_state=state,
        metadata=metadata,
        now=_now(),
    )
    if passive:
        models.record_artifact_change(
            artifact_id=artifact["id"],
            internal_event=event_type,
            external_id=artifact.get("external_id"),
            summary=summary,
            provenance=metadata,
            actor_thread_id=actor_thread_id,
            db=db,
        )


def _actor_display(db, actor: str) -> tuple[str, str, str]:
    if actor == UI_PRINCIPAL:
        return "user", UI_PRINCIPAL, "The user"
    row = db.execute("SELECT title FROM threads WHERE id=?", (actor,)).fetchone()
    title = str(row["title"] or "").strip() if row else ""
    return "agent", actor, title or actor


def _feedback_text(before: bytes, after: bytes) -> bool:
    if len(before) > MAX_TEXT_BYTES or len(after) > MAX_TEXT_BYTES:
        return False
    try:
        before.decode("utf-8")
        after.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def prepare_owner_report(
    authority: WriteAuthority | None, artifact: dict | None, *, path: Path,
    before: bytes | None, after: bytes, db, classification: str | None = None,
) -> dict:
    """Prepare the classification, diff and feedback for one allowed edit, before BEGIN.

    Returns an empty payload when the owner hears nothing of this write or its
    prior bytes are unknown.
    """
    from doceditor.feedback_edit_preparation import prepare_edit

    if authority is None or not authority.reaches_owner or not authority.allowed or before is None:
        return {}
    classification = classification or classify_edit(path, before, after)
    owner = _registration_owner(db, artifact["id"]) if artifact else None
    payload = {"classification": classification, "diff": capped_unified_diff(path, before, after),
               "feedback": None, "owner": dict(owner) if owner else None}
    recipient = str(owner["parent_thread_id"] if owner else authority.owner or "").strip()
    if classification == "content" and recipient and artifact is not None and _feedback_text(before, after):
        kind, author, display = _actor_display(db, authority.actor)
        kwargs = dict(artifact_id=str(artifact["id"]), recipient_id=recipient, author_kind=kind,
                      author_id=author, actor_display=display, path=path, before=before, after=after)
        payload["feedback"] = (kwargs, prepare_edit(db, **kwargs))
    return payload


def report_owner_write(
    authority: WriteAuthority | None,
    artifact: dict,
    *,
    path: Path,
    before: bytes | None,
    after: bytes | None,
    thread_id: str,
    db,
    prepared: dict | None = None,
    source: str = "write",
) -> str | None:
    """Report one landed write to the document's owner, inside the caller's transaction.

    The one reporter for every write path. An allowed edit by anyone but the
    owner becomes ``content_edited_by_non_owner`` with its classification, diff
    and feedback batch; a refused write that landed anyway becomes
    ``ownership_bypass_write``, and the returned notice tells the writer to
    suggest instead. Returns None when the writer needs no notice.
    """
    if authority is None or not authority.reaches_owner:
        return None
    if prepared is None and after is not None:
        prepared = prepare_owner_report(authority, artifact, path=path, before=before, after=after, db=db)
    elif prepared:
        owner = _registration_owner(db, artifact["id"])
        if (dict(owner) if owner else None) != prepared["owner"]:
            from doceditor.feedback_edit_preparation import FeedbackPreparationChanged
            raise FeedbackPreparationChanged("The document owner changed during publication preparation")
    payload = prepared or {}
    classification = payload.get("classification") or _UNCLASSIFIED
    metadata = {"actor": authority.actor, "thread_id": thread_id, "classification": classification,
                "diff": payload.get("diff"), "path": authority.path_rel, "source": source}
    feedback = payload.get("feedback")
    if feedback is not None:
        kwargs, edit = feedback
        feedback_batches.record_edit(db, **kwargs, prepared=edit)
    allowed = authority.allowed
    _record_event(
        artifact, path=path, fallback_owner=authority.owner,
        event_type="content_edited_by_non_owner" if allowed else "ownership_bypass_write",
        actor_thread_id=authority.actor, reason=classification if allowed else f"{source} write",
        metadata=metadata,
        summary=(f"`{authority.path_rel}` was edited by a non-owner ({classification})." if allowed
                 else f"`{authority.path_rel}` was changed outside the file tools by thread {thread_id}."),
        db=db, passive=feedback is None,
    )
    return None if allowed else bypass_notice(authority.path_rel, owner_label(str(authority.owner), db=db))


def report_plain_write(
    authority: WriteAuthority | None, path: Path, before: bytes | None, *, thread_id: str, db,
) -> str | None:
    """Report a landed plain write (a file-tool write or save outside ``shared/``) in its own transaction."""
    if authority is None or not authority.reaches_owner or authority.artifact_id is None:
        return None
    artifact = models.get_artifact(authority.artifact_id, db=db)
    if artifact is None:
        return None
    after = path.read_bytes()
    prepared = prepare_owner_report(authority, artifact, path=path, before=before, after=after, db=db)
    with write_transaction(db, scope="report_plain_write") as connection:
        return report_owner_write(authority, artifact, path=path, before=before, after=after,
                                  thread_id=thread_id, db=connection, prepared=prepared)


def record_post_write_events(
    shared: SharedTarget,
    artifact: dict,
    *,
    before: bytes | None,
    after: bytes,
    actor_thread_id: str,
    db,
    prepared: dict | None = None,
) -> None:
    """Record a governed shared write's outside-tool change and owner report before it commits."""
    relative = shared.authority.path_rel if shared.authority else shared.path.as_posix()
    if shared.outside_change:
        _record_event(
            artifact,
            path=shared.path,
            fallback_owner=shared.owner_thread_id,
            event_type="ownership_bypass_write",
            actor_thread_id=None,
            reason="outside CORSIA",
            metadata={"source": "outside CORSIA", "path": relative},
            summary=f"`{relative}` changed outside CORSIA before its latest durable write.",
            db=db,
        )
    if prepared is None:
        prepared = prepare_owner_report(shared.authority, artifact, path=shared.path, before=before,
                                        after=after, db=db, classification=shared.classification)
    report_owner_write(shared.authority, artifact, path=shared.path, before=before, after=after,
                       thread_id=actor_thread_id, db=db, prepared=prepared)
