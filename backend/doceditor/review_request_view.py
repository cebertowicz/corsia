"""Document review list projection; the service owns all permission rules."""
from __future__ import annotations

from doceditor import review_requests as reviews
from infra.accounts import get_user


def _thread_names(db, rows: list[dict]) -> dict[str, str]:
    ids = {row["reviewer_thread_id"] for row in rows}
    ids.update(row["requester_id"] for row in rows if row["requester_kind"] == "agent")
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    return {
        row["id"]: row["title"] or row["id"]
        for row in db.execute(f"SELECT id,title FROM threads WHERE id IN ({placeholders})", tuple(ids))
    }


def _requester_name(db, row, actor: reviews.ReviewActor, names: dict[str, str]) -> str:
    requester_id = row["requester_id"]
    if row["requester_kind"] == "agent":
        return names.get(requester_id, requester_id)
    if actor.kind == "user" and actor.id == requester_id:
        return actor.display
    user = get_user(requester_id, db=db)
    return user["name"] if user else requester_id


def document_reviews(db, artifact_id: str, actor: reviews.ReviewActor) -> dict:
    """Read the accessible requests and optionally the viewing thread's roster.

    A roster failure is distinct from an empty roster and leaves request rows
    available. The helper checks the viewing thread's user before any roster
    read, after the artifact access check here.
    """
    artifact = reviews._artifact(db, artifact_id, actor)
    rows = reviews.for_document(db, artifact_id)
    names = _thread_names(db, rows)
    result: dict[str, object] = {"reviews": [
        {
            **row, "reviewer_name": names.get(row["reviewer_thread_id"], row["reviewer_thread_id"]),
            "requester_name": _requester_name(db, row, actor, names),
            "may_cancel": row["outcome"] == "open" and reviews.may_cancel(db, artifact, row, actor),
        }
        for row in rows
    ]}
    if not actor.thread_id:
        return result
    try:
        eligible = reviews.reviewer_eligibility(db, artifact, actor)
    except reviews.ReviewRosterError:
        result["reviewers_error"] = "roster_error"
        return result
    requested = {row["reviewer_thread_id"] for row in rows if row["outcome"] == "open"}
    result["reviewers"] = [
        {**row, "requested": row["id"] in requested} for row in eligible.choices
    ]
    return result
