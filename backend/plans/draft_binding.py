"""A person's yes on a review ask covers the exact draft they answered on.

When the person answers an ask, the answer records a digest of the asked
file's bytes at that moment. An agent closing a person's gate step on an
``approved`` answer is refused once the file no longer matches: the yes was
given on another draft, so the agent asks again on this one. ``approved with
changes`` expects the draft to change and is not bound. A file the platform
cannot read (no stored path, an external object) records nothing and binds
nothing.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from plans import service

#: The answer's record of the draft it was given on.
DRAFT_KEY = "draft"
#: Why closing on a yes given to an earlier draft is refused.
DRAFT_CHANGED = (
    "The person approved an earlier draft of {title}; it changed after their yes. "
    "Ask them again on this draft (block_node owner_kind user, ask_artifact_id), "
    "and close this step after they answer."
)
_APPROVED = "approved"
_ANSWERED_BY_PERSON = "user.ask_cleared"


def digest(db, artifact_id: str) -> str | None:
    """The sha256 of an artifact's file as it is now, or None when it has no readable file."""
    from infra import config

    row = db.execute("SELECT path FROM artifact_identities WHERE id=?", (artifact_id,)).fetchone()
    if row is None or not row["path"]:
        return None
    try:
        return hashlib.sha256((Path(config.WORKSPACE) / row["path"]).read_bytes()).hexdigest()
    except OSError:
        return None


def answered(db, artifact_id: str) -> dict:
    """The draft fact an answer on ``artifact_id`` records; empty when there is no readable file."""
    sha = digest(db, artifact_id)
    return {} if sha is None else {DRAFT_KEY: {"artifact_id": artifact_id, "sha256": sha}}


def refuse_changed(db, step) -> None:
    """Refuse closing a step on an ``approved`` answer whose draft has changed since.

    Reads the latest person answer across the step's rounds (same parent, same step id).
    """
    rounds = [row["id"] for row in db.execute(
        "SELECT id FROM plan_nodes WHERE parent_id IS ? AND workflow_step=?",
        (step["parent_id"], step["workflow_step"]),
    )]
    row = db.execute(
        "SELECT payload_json FROM plan_node_events WHERE event='hold_cleared' AND provenance=?"
        " AND node_id IN (SELECT value FROM json_each(?)) ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (_ANSWERED_BY_PERSON, json.dumps(rounds)),
    ).fetchone()
    detail = (json.loads(row["payload_json"] or "{}").get("detail") or {}) if row is not None else {}
    draft = detail.get(DRAFT_KEY)
    if detail.get("verdict") != _APPROVED or not isinstance(draft, dict):
        return
    if digest(db, str(draft["artifact_id"])) != draft["sha256"]:
        title = db.execute(
            "SELECT title FROM artifact_identities WHERE id=?", (draft["artifact_id"],),
        ).fetchone()
        name = f'"{title["title"]}"' if title is not None and title["title"] else "the document"
        raise service.PlanError(DRAFT_CHANGED.format(title=name))
