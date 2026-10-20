"""Local-only thread policy at model-admission and agent-send boundaries.

Each thread row stores its own effective ``local_only``. A new thread copies it
from its parents and its creating agent; turning it on or off writes it down the
family (``spawned_by`` / ``parent_thread_id``) in one statement, so every read
is the plain column.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from llm.locality import locality_refusal
from prompts.cloud_off import thread_local_only_text

LOCAL_ONLY_REFUSAL_CODE = "local_only_requires_local_model"
LOCAL_ONLY_SEND_NEEDS_CONFIRM_CODE = "local_only_send_needs_confirm"

#: The thread and every thread under it through the family links.
_FAMILY_SQL = (
    "WITH RECURSIVE family(id) AS (SELECT ? UNION "
    "SELECT thread.id FROM threads AS thread JOIN family "
    "ON thread.spawned_by=family.id OR thread.parent_thread_id=family.id) "
)


@dataclass(eq=False)
class LocalityRefused(Exception):
    """A model binding the thread's Local only setting refuses.

    Not a ``ValueError``: no layer's generic error handling may reinterpret it.
    Each audience renders its own text from these fields.
    """

    cause: str
    selector: str
    ready_local_selectors: tuple[str, ...]
    spawn: bool = False

    code = LOCAL_ONLY_REFUSAL_CODE

    def __str__(self) -> str:
        return thread_local_only_text(self.selector, list(self.ready_local_selectors), spawn=self.spawn)


def _is_cloud(selector: str, *, db: sqlite3.Connection) -> bool:
    from llm.resolution import catalog_row

    row = catalog_row(selector, db=db) or {"litellm_model": selector}
    return locality_refusal(row, thread_local_only=True, project_id=None, db=db) is not None


def _ready_local_selectors() -> tuple[str, ...]:
    from llm.model_status import catalog_base_rows

    return tuple(sorted({
        str(status["litellm_model"])
        for status in catalog_base_rows()
        if status.get("is_local") is True
        and status.get("status") == "ready"
        and status.get("litellm_model")
    }))


def require_local_only_model(
    model: str | None,
    *,
    thread_local_only: bool,
    db: sqlite3.Connection,
    spawn: bool = False,
    thread_id: str | None = None,
) -> None:
    """Raise ``LocalityRefused`` when a Local-only thread would bind a cloud model."""
    if not thread_local_only or not isinstance(model, str) or not model.strip():
        return
    selector = model.strip()
    if not _is_cloud(selector, db=db):
        return
    refusal = LocalityRefused("thread_local_only", selector, _ready_local_selectors(), spawn)
    _emit("local_only.refused", thread_id, {"cause": refusal.cause, "spawn": spawn})
    raise refusal


def inherited_local_only(ids: tuple[str | None, ...], *, db: sqlite3.Connection) -> bool:
    """Whether any of a new thread's parents or its creating agent is Local only."""
    found = [thread_id for thread_id in dict.fromkeys(ids) if thread_id]
    if not found:
        return False
    row = db.execute(
        f"SELECT MAX(local_only) FROM threads WHERE id IN ({','.join('?' * len(found))})", found,
    ).fetchone()
    return bool(row[0])


def local_only_ancestor(thread: sqlite3.Row, *, db: sqlite3.Connection) -> bool:
    """Whether any family ancestor of ``thread`` is still Local only."""
    row = db.execute(
        "WITH RECURSIVE ancestors(id) AS (SELECT ? UNION SELECT ? UNION "
        "SELECT parent.id FROM threads AS parent JOIN threads AS thread "
        "ON parent.id=thread.spawned_by OR parent.id=thread.parent_thread_id "
        "JOIN ancestors ON thread.id=ancestors.id) "
        "SELECT 1 FROM threads WHERE local_only=1 AND id IN (SELECT id FROM ancestors) LIMIT 1",
        (thread["spawned_by"], thread["parent_thread_id"]),
    ).fetchone()
    return row is not None


def add_local_only_payload_fields(rows: list[dict], *, db: sqlite3.Connection) -> None:
    """Add effective Local-only fields to thread payload rows in one batch."""
    if not rows:
        return
    thread_ids = [str(row["id"]) for row in rows]
    placeholders = ",".join("?" for _ in thread_ids)
    ancestors = db.execute(
        "WITH RECURSIVE roots(id, user_id) AS ("
        f"SELECT id, user_id FROM threads WHERE id IN ({placeholders})"
        "), ancestors(root_id, id, depth) AS ("
        "SELECT roots.id, parent.id, 1 FROM roots JOIN threads AS child ON child.id=roots.id "
        "JOIN threads AS parent ON parent.id=child.spawned_by OR parent.id=child.parent_thread_id "
        "UNION "
        "SELECT ancestors.root_id, parent.id, ancestors.depth + 1 FROM ancestors "
        "JOIN threads AS child ON child.id=ancestors.id "
        "JOIN threads AS parent ON parent.id=child.spawned_by OR parent.id=child.parent_thread_id "
        "WHERE ancestors.depth < 64"
        "), ranked AS ("
        "SELECT ancestors.root_id, ancestor.id, ancestor.title, ancestors.depth, "
        "ROW_NUMBER() OVER (PARTITION BY ancestors.root_id ORDER BY ancestors.depth ASC) AS rank "
        "FROM ancestors JOIN roots ON roots.id=ancestors.root_id "
        "JOIN threads AS ancestor ON ancestor.id=ancestors.id "
        "WHERE ancestor.local_only=1 AND ancestor.user_id=roots.user_id"
        ") SELECT root_id, id, title FROM ranked WHERE rank=1",
        thread_ids,
    ).fetchall()
    inherited = {row["root_id"]: (row["id"], row["title"]) for row in ancestors}

    from llm.locality import cloud_off_project_ids

    cloud_off_projects = cloud_off_project_ids(db=db)
    for row in rows:
        own_setting = bool(row.get("local_only"))
        source = None if own_setting else inherited.get(row["id"])
        row["local_only_effective"] = bool(
            own_setting or source or row.get("project_id") in cloud_off_projects
        )
        row["local_only_inherited_from_thread_id"] = source[0] if source else None
        row["local_only_inherited_from_title"] = source[1] if source else None


def write_family_local_only(thread_id: str, local_only: bool, now: str, *, db: sqlite3.Connection) -> None:
    """Set ``local_only`` on a thread and every family descendant, in the caller's transaction."""
    db.execute(
        _FAMILY_SQL + "UPDATE threads SET local_only=?,updated_at=? "
        "WHERE id IN (SELECT id FROM family) AND local_only<>?",
        (thread_id, int(local_only), now, int(local_only)),
    )


def send_confirmation_refusal(
    sender: dict,
    recipient: dict,
    *,
    no_sensitive_content: object,
    db: sqlite3.Connection,
) -> str | None:
    """Require a local-only sender to confirm a cloud-bound recipient is safe."""
    if not sender.get("local_only"):
        return None
    # Every recipient thread is read by its own bound model, so only its
    # locality decides; a person's chat on a cloud model is still cloud-bound.
    # A pending switch decides over the current model: queued inputs run on it.
    recipient_model = recipient.get("pending_model") or recipient.get("model")
    recipient_name = str(recipient.get("title") or recipient.get("id") or "This recipient")
    if not isinstance(recipient_model, str) or not recipient_model.strip():
        reason = "has no selected model and may adopt a cloud model"
    elif _is_cloud(recipient_model.strip(), db=db):
        reason = f"runs on a cloud model ({recipient_model})"
    else:
        return None
    confirmed = no_sensitive_content is True
    _emit(
        "local_only.send_confirmed" if confirmed else "local_only.send_needs_confirm",
        sender.get("id"), {"recipient_thread_id": recipient.get("id")},
    )
    if confirmed:
        return None
    return (
        f"{LOCAL_ONLY_SEND_NEEDS_CONFIRM_CODE}: {recipient_name} {reason}. Remove anything "
        "sensitive from the message (keys, secrets, personal data, private file contents), "
        "then call again with no_sensitive_content: true (on `corsia delegate send`, "
        "add --no-sensitive-content)."
    )


def send_confirmation_error(
    sender: dict,
    recipient: dict,
    no_sensitive_content: object,
) -> str | None:
    """Read model locality and return the send confirmation refusal, if any."""
    if not sender.get("local_only"):
        return None
    from infra.database import get_db

    db = get_db()
    try:
        return send_confirmation_refusal(
            sender, recipient, no_sensitive_content=no_sensitive_content, db=db,
        )
    finally:
        db.close()


def _emit(action: str, thread_id: str | None, properties: dict) -> None:
    from infra.product_events import emit_product_event

    emit_product_event("session", action, thread_id=thread_id, properties=properties)
