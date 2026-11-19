"""A person's yes on a review ask covers the exact draft they answered on.

When the person answers an ask, the answer records a digest of the asked
file's bytes at that moment. An agent closing a person's gate step on an
``approved`` answer is refused once the file no longer matches: the yes was
given on another draft, so the agent asks again on this one. ``approved with
changes`` expects the draft to change and is not bound. An ask with no file
(an external object) records nothing and binds nothing. A file that should be
there but cannot be read is logged: at answer time the yes binds nothing; at
close time the step is refused as unreadable, never as changed.
"""

from __future__ import annotations

import hashlib
import logging
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
#: Why closing on a yes is refused when the draft it was given on cannot be read now.
DRAFT_UNREADABLE = (
    "The person approved {title}, but its file can't be read now, so their yes can't be "
    "checked against it. Restore the file, or ask them again on the current draft "
    "(block_node owner_kind user, ask_artifact_id), and close this step after they answer."
)
_APPROVED = "approved"
log = logging.getLogger(__name__)


def digest(db, artifact_id: str) -> str | None:
    """The sha256 of an artifact's file as it is now, or None when it has no readable file.

    The file is found the way every registered document is: its ``workspace://`` identity
    first, then its stored path, then its external id.
    """
    from infra import config
    from infra.workspace_paths import resolve_registered_path

    row = db.execute(
        "SELECT path, external_id FROM artifact_identities WHERE id=?", (artifact_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        stored = resolve_registered_path(row["external_id"], row["path"])
    except ValueError:
        log.warning("draft binding: document %s is outside this workspace", artifact_id)
        return None
    raw = stored or row["external_id"]
    if not raw:
        return None
    try:
        return hashlib.sha256((Path(config.WORKSPACE) / raw).read_bytes()).hexdigest()
    except OSError as error:
        # An identity with no stored path may name no file at all (an external object);
        # a resolved path that cannot be read is a document gone missing.
        if stored:
            log.warning("draft binding: document %s can't be read: %s", artifact_id, error)
        return None


def answered(db, artifact_id: str) -> dict:
    """The draft fact an answer on ``artifact_id`` records; empty when there is no readable file."""
    sha = digest(db, artifact_id)
    return {} if sha is None else {DRAFT_KEY: {"artifact_id": artifact_id, "sha256": sha}}


def refuse_changed(db, step) -> None:
    """Refuse closing a step on an ``approved`` answer whose draft has changed since.

    Reads the same latest answer the gate's other refusals read
    (``gate_handoff_record.latest_answer``), so they never disagree on what the person said.
    """
    from plans import gate_handoff_record

    detail = gate_handoff_record.latest_answer(db, step) or {}
    draft = detail.get(DRAFT_KEY)
    if detail.get("verdict") != _APPROVED or not isinstance(draft, dict):
        return
    now = digest(db, str(draft["artifact_id"]))
    if now == draft["sha256"]:
        return
    title = db.execute(
        "SELECT title FROM artifact_identities WHERE id=?", (draft["artifact_id"],),
    ).fetchone()
    name = f'"{title["title"]}"' if title is not None and title["title"] else "the document"
    raise service.PlanError((DRAFT_UNREADABLE if now is None else DRAFT_CHANGED).format(title=name))
