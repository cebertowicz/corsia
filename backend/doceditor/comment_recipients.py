"""Comment-thread participants, tags and recipient notification state."""
from __future__ import annotations

from core.agent_roster_queries import agent_rows
from infra.queries import get_thread_for_user


class TagValidationError(ValueError):
    """A tag is outside the authenticated viewer's visible roster."""


def validate_tags(db, *, user_id: str, thread_id: str | None, agent_ids: list[str]) -> None:
    """Validate tag IDs against the authenticated viewer's visible roster."""
    if not agent_ids:
        return
    if not thread_id or get_thread_for_user(thread_id, user_id, db=db) is None:
        raise TagValidationError("A valid viewing thread_id is required to tag agents")
    _, visible = agent_rows(thread_id, db)
    available = {row["id"] for row in visible}
    if any(agent_id not in available for agent_id in agent_ids):
        raise TagValidationError("Tagged agents must be in the viewing thread's visible roster")


def validate_mentions(body: str, agent_ids: list[str], mentions: list[dict]) -> None:
    """Validate positions in UTF-16 units, including character boundaries."""
    boundaries = {0}
    offset = 0
    for character in body:
        offset += 2 if ord(character) > 0xFFFF else 1
        boundaries.add(offset)
    selected = set(agent_ids)
    for mention in mentions:
        start, end = mention["start"], mention["end"]
        if (mention["agent_id"] not in selected or type(start) is not int or type(end) is not int
                or start not in boundaries or end not in boundaries or start >= end):
            raise TagValidationError("Mention must reference a selected agent and valid UTF-16 body offsets")


def trim_mentions(body: str, agent_ids: list[str], mentions: list[dict]) -> list[dict]:
    """Translate submitted UTF-16 offsets to the existing stripped body."""
    validate_mentions(body, agent_ids, mentions)
    prefix = body[:len(body) - len(body.lstrip())]
    removed = sum(2 if ord(character) > 0xFFFF else 1 for character in prefix)
    adjusted = [{**mention, "start": mention["start"] - removed, "end": mention["end"] - removed}
                for mention in mentions]
    validate_mentions(body.strip(), agent_ids, adjusted)
    return adjusted


def save_mentions(db, comment: dict, mentions: list[dict]) -> None:
    db.executemany(
        "INSERT INTO doc_comment_mentions(comment_id,agent_id,start_offset,end_offset) VALUES (?,?,?,?)",
        [(comment["id"], mention["agent_id"], mention["start"], mention["end"]) for mention in mentions],
    )


def root_id(comment: dict) -> str:
    return str(comment.get("parent_id") or comment["id"])


def save_tags(db, comment: dict, agent_ids: list[str]) -> None:
    for agent_id in set(agent_ids):
        row = db.execute("SELECT title FROM threads WHERE id=?", (agent_id,)).fetchone()
        name = (row["title"] or "").strip() or agent_id
        db.execute(
            "INSERT INTO doc_comment_tags(comment_id,agent_id,agent_name) VALUES (?,?,?)",
            (comment["id"], agent_id, name),
        )


def tags_for_comments(db, rows: list[dict]) -> None:
    """Attach tag IDs to flat comment rows without per-row queries."""
    if not rows:
        return
    by_id = {row["id"]: row for row in rows}
    for row in rows:
        row["tagged_agent_ids"] = []
        row["tagged_agents"] = []
        row["tagged_agent_mentions"] = []
    marks = ",".join("?" for _ in by_id)
    for tag in db.execute(f"SELECT comment_id,agent_id,agent_name FROM doc_comment_tags WHERE comment_id IN ({marks}) ORDER BY agent_id", tuple(by_id)):
        row = by_id[tag["comment_id"]]
        row["tagged_agent_ids"].append(tag["agent_id"])
        row["tagged_agents"].append({"id": tag["agent_id"], "name": tag["agent_name"]})
    for mention in db.execute(f"SELECT comment_id,agent_id,start_offset,end_offset FROM doc_comment_mentions WHERE comment_id IN ({marks}) ORDER BY start_offset,end_offset,agent_id", tuple(by_id)):
        by_id[mention["comment_id"]]["tagged_agent_mentions"].append({
            "agent_id": mention["agent_id"], "start": mention["start_offset"], "end": mention["end_offset"],
        })


def reset_action(db, comment: dict, agent_id: str) -> None:
    db.execute("""INSERT INTO doc_comment_recipient_state(root_id,agent_id,notifications)
        VALUES (?,?,0) ON CONFLICT(root_id,agent_id) DO UPDATE SET notifications=0""", (root_id(comment), agent_id))


def leave(db, comment: dict, agent_id: str) -> None:
    root = root_id(comment)
    from registry.models import get_artifact

    artifact = get_artifact(comment["artifact_id"], db=db)
    involved = artifact and artifact.get("parent_thread_id") == agent_id
    if not involved:
        involved = db.execute(
            """SELECT 1 FROM doc_comments WHERE (id=? OR parent_id=?) AND author_thread_id=?
               UNION ALL
               SELECT 1 FROM doc_comment_tags t
                 JOIN doc_comments c ON c.id=t.comment_id
                 JOIN doc_feedback_batch_items i ON i.comment_id=c.id
                 JOIN doc_feedback_batches b ON b.id=i.batch_id AND b.recipient_id=t.agent_id
               WHERE (c.id=? OR c.parent_id=?) AND t.agent_id=? AND b.state='delivered'
                 AND b.delivery_outcome='node' LIMIT 1""",
            (root, root, agent_id, root, root, agent_id),
        ).fetchone()
    if not involved:
        raise ValueError("You are not on this comment thread")
    db.execute("""INSERT INTO doc_comment_recipient_state(root_id,agent_id,left_thread,notifications)
        VALUES (?,?,1,0) ON CONFLICT(root_id,agent_id) DO UPDATE SET left_thread=1,notifications=0""", (root_id(comment), agent_id))


def reinstate(db, comment: dict, agent_id: str) -> None:
    db.execute("""INSERT INTO doc_comment_recipient_state(root_id,agent_id,left_thread)
        VALUES (?,?,0) ON CONFLICT(root_id,agent_id) DO UPDATE SET left_thread=0""", (root_id(comment), agent_id))


def filter_left_replies(db, items: list[dict], agent_id: str, *, person_owner: bool) -> list[dict]:
    """Do not deliver held replies after their recipient has left the thread."""
    roots = {root_id(item) for item in items if item.get("kind") == "comment"}
    if not roots:
        return items
    left_roots = {
        root for root in roots if db.execute(
            "SELECT 1 FROM doc_comment_recipient_state WHERE root_id=? AND agent_id=? AND left_thread=1",
            (root, agent_id),
        ).fetchone()
    }
    def keep(item: dict) -> bool:
        if (person_owner or item.get("kind") != "comment" or not item.get("parent_id")
                or root_id(item) not in left_roots):
            return True
        return bool(item.get("event") != "reopened" and db.execute(
            "SELECT 1 FROM doc_comment_tags WHERE comment_id=? AND agent_id=?", (item["id"], agent_id),
        ).fetchone())

    return [item for item in items if keep(item)]


def prepare_notification(db, items: list[dict], agent_id: str) -> None:
    """Count one delivered batch per comment thread and attach any due hint."""
    roots = set()
    for item in items:
        if item.get("kind") != "comment":
            continue
        root = root_id(item)
        tagged = item.get("event") != "reopened" and db.execute(
            "SELECT 1 FROM doc_comment_tags WHERE comment_id=? AND agent_id=?",
            (item["id"], agent_id),
        ).fetchone()
        if tagged:
            reinstate(db, item, agent_id)
        if root in roots:
            continue
        roots.add(root)
        row = db.execute(
            "SELECT notifications,left_thread FROM doc_comment_recipient_state WHERE root_id=? AND agent_id=?",
            (root, agent_id),
        ).fetchone()
        count = row["notifications"] if row else 0
        if count == 3 and not (row and row["left_thread"]):
            item["leave_hint"] = f'To leave this thread, use doc_comment_update(comment_id="{root}", leave=true).'
        db.execute("""INSERT INTO doc_comment_recipient_state(root_id,agent_id,notifications)
            VALUES (?,?,1) ON CONFLICT(root_id,agent_id)
            DO UPDATE SET notifications=notifications+1""", (root, agent_id))
