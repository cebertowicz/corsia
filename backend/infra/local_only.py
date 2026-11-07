"""Local-only thread policy at model-admission and agent-send boundaries.

Each thread row stores its own ``local_only``. A new thread inherits it from its
parents, its creating agent, and their projects' cloud-off policies; turning it
on or off writes it down the family (``spawned_by`` / ``parent_thread_id``) in
one statement.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from llm.locality import locality_refusal
from prompts.cloud_off import thread_local_only_text

LOCAL_ONLY_REFUSAL_CODE = "local_only_requires_local_model"
_ANCESTOR_DEPTH = 64

#: The thread and every thread under it through the family links.
_FAMILY_SQL = (
    "WITH RECURSIVE family(id,user_id) AS ("
    "SELECT id,user_id FROM threads WHERE id=? UNION "
    "SELECT thread.id,thread.user_id FROM threads AS thread JOIN family "
    "ON (thread.spawned_by=family.id OR thread.parent_thread_id=family.id) "
    "AND thread.user_id=family.user_id) "
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
    """Whether a parent or creator, or its project, requires Local only."""
    found = [thread_id for thread_id in dict.fromkeys(ids) if thread_id]
    if not found:
        return False
    row = db.execute(
        "SELECT MAX(CASE WHEN t.local_only OR COALESCE(p.cloud_models_off, 0) THEN 1 ELSE 0 END) "
        "FROM threads t LEFT JOIN project_inference_policies p ON p.project_id = t.project_id "
        f"WHERE t.id IN ({','.join('?' * len(found))})", found,
    ).fetchone()
    return bool(row[0])


def local_only_sources(
    thread_ids: list[str], *, db: sqlite3.Connection,
) -> dict[str, tuple[str, str | None]]:
    """The nearest Local-only ancestor (id, title) holding each thread's setting.

    The one answer to "is this setting locked to a parent?" for the thread list
    and the turn-off refusal. Turning Local only on writes it down the family, so
    only a thread that is itself Local only and has a parent link can have one:
    the walk starts from those, keeps the same user's threads, never names the
    thread itself, and stops after ``_ANCESTOR_DEPTH`` links.
    """
    if not thread_ids:
        return {}
    placeholders = ",".join("?" for _ in thread_ids)
    ancestors = db.execute(
        "WITH RECURSIVE ancestors(root_id, user_id, id, depth) AS ("
        "SELECT child.id, child.user_id, parent.id, 1 FROM threads AS child "
        "JOIN threads AS parent ON parent.id IN (child.spawned_by, child.parent_thread_id) "
        f"WHERE child.id IN ({placeholders}) AND child.local_only=1 "
        "UNION "
        "SELECT ancestors.root_id, ancestors.user_id, parent.id, ancestors.depth + 1 FROM ancestors "
        "JOIN threads AS child ON child.id=ancestors.id "
        "JOIN threads AS parent ON parent.id IN (child.spawned_by, child.parent_thread_id) "
        f"WHERE ancestors.depth < {_ANCESTOR_DEPTH}"
        "), ranked AS ("
        "SELECT ancestors.root_id, ancestor.id, ancestor.title, "
        "ROW_NUMBER() OVER (PARTITION BY ancestors.root_id ORDER BY ancestors.depth ASC) AS rank "
        # CROSS JOIN keeps the walk's rows driving, so each ancestor is one primary-key read.
        "FROM ancestors CROSS JOIN threads AS ancestor ON ancestor.id=ancestors.id "
        "WHERE ancestor.local_only=1 AND ancestor.user_id=ancestors.user_id "
        "AND ancestor.id<>ancestors.root_id"
        ") SELECT root_id, id, title FROM ranked WHERE rank=1",
        thread_ids,
    ).fetchall()
    return {row["root_id"]: (row["id"], row["title"]) for row in ancestors}


def add_local_only_payload_fields(rows: list[dict], *, db: sqlite3.Connection) -> None:
    """Add effective Local-only fields to thread payload rows in one batch."""
    if not rows:
        return
    # The column says whether the thread is Local only; the source says whether
    # a parent holds it there, by the same walk that refuses turning it off.
    inherited = local_only_sources([str(row["id"]) for row in rows], db=db)

    from llm.locality import cloud_off_project_ids, locality_cause

    cloud_off_projects = cloud_off_project_ids(db=db)
    for row in rows:
        source = inherited.get(row["id"])
        from_project = row.get("project_id") in cloud_off_projects
        cause = locality_cause(
            thread_local_only=bool(row.get("local_only")), cloud_off=from_project,
        )
        row["local_only_effective"] = bool(cause)
        row["local_only_cause"] = "inherited" if source else {
            "thread_local_only": "own", "project_cloud_off": "project",
        }.get(cause)
        row["local_only_inherited_from_thread_id"] = source[0] if source else None
        row["local_only_inherited_from_title"] = source[1] if source else None


def write_family_local_only(thread_id: str, local_only: bool, now: str, *, db: sqlite3.Connection) -> None:
    """Set ``local_only`` on a thread and every family descendant, in the caller's transaction."""
    db.execute(
        _FAMILY_SQL + "UPDATE threads SET local_only=?,updated_at=? "
        "WHERE id IN (SELECT id FROM family) AND local_only<>?",
        (thread_id, int(local_only), now, int(local_only)),
    )



def send_needs_hold(sender: dict, recipient: dict, *, db: sqlite3.Connection | None = None,
                    emit_hold_event: bool = True) -> bool:
    """Whether a message from this sender to this recipient waits for the person to approve it.

    True for a Local only sender (its own switch or its project's) and a recipient bound to a cloud
    model, or to none yet. Every recipient thread is read by its own bound model, so only its
    locality decides; a person's chat on a cloud model is still cloud-bound. A pending switch
    decides over the current model, because queued inputs run on it.
    """
    from infra.database import get_db
    from llm.locality import project_cloud_models_off

    if not sender.get("local_only") and not sender.get("project_id"):
        return False
    conn: sqlite3.Connection = get_db() if db is None else db
    try:
        if not sender.get("local_only") and not project_cloud_models_off(sender.get("project_id"), db=conn):
            return False
        recipient_model = recipient.get("pending_model") or recipient.get("model")
        if isinstance(recipient_model, str) and recipient_model.strip() and not _is_cloud(recipient_model.strip(), db=conn):
            return False
    finally:
        if db is None:
            conn.close()
    if emit_hold_event:
        _emit("local_only.send_held", sender.get("id"), {"recipient_thread_id": recipient.get("id")})
    return True


def _emit(action: str, thread_id: str | None, properties: dict) -> None:
    from infra.product_events import emit_product_event

    emit_product_event("session", action, thread_id=thread_id, properties=properties)
