"""Comment-thread participants, tags and recipient notification state."""
from __future__ import annotations

import json
import sqlite3

from core.agent_roster_queries import agent_rows
from doceditor import writer_line
from doceditor.comment_action_errors import CommentActionError
from infra.accounts import get_user, list_users
from infra.auth import is_owner_sentinel
from infra.queries import get_thread_for_user
from registry.models import get_artifact


LEGACY_RECIPIENTS_SKIP_LOCALITY = True


class TagValidationError(CommentActionError):
    """A tag the comment cannot carry; ``kind`` names which known cause it is."""


def taggable_agents(db, *, user_id: str, thread_id: str) -> list[dict]:
    """The viewer's visible roster, limited to agents that can receive feedback."""
    from doceditor.feedback_delivery import recipient_problem
    from doceditor.review_requests import ROSTER_ERROR, ReviewRosterError

    if not thread_id or get_thread_for_user(thread_id, user_id, db=db) is None:
        raise TagValidationError("no_viewing_thread", "A valid viewing thread_id is required to tag agents")
    try:
        _, visible = agent_rows(thread_id, db)
    except sqlite3.OperationalError as exc:
        # The roster read reports an unreadable roster this way; reviewer choice reads it the same.
        raise ReviewRosterError(ROSTER_ERROR) from exc
    return [{"id": row["id"], "name": row["title"] or row["id"],
             "responsibility": row["agent_responsibility"]}
            for row in visible if row["id"] != thread_id and recipient_problem(db, row["id"]) is None]


def validate_tags(db, *, user_id: str, thread_id: str | None, agent_ids: list[str]) -> None:
    """Validate tag IDs against the authenticated viewer's eligible roster."""
    if not agent_ids:
        return
    available = {row["id"] for row in taggable_agents(db, user_id=user_id, thread_id=thread_id or "")}
    if any(agent_id not in available for agent_id in agent_ids):
        raise TagValidationError("tag_unavailable", "Tagged agents must be in the viewing thread's visible roster")


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
            raise TagValidationError("mention_position", "Mention must reference a selected agent and valid UTF-16 body offsets")


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


def locality_disposition(
    db, comment: dict, recipient_id: str, sender, owner_id: str | None, *, emit: bool = True,
) -> tuple[str | None, bool]:
    """Return the locality refusal and whether the pre-tag recipient path is exempt; `emit` counts a refusal."""
    from infra.local_only import send_needs_hold

    root = db.execute("SELECT author_kind,author_thread_id FROM doc_comments WHERE id=?",
                      (comment.get("parent_id"),)).fetchone() if comment.get("parent_id") else None
    legacy_recipients = {owner_id} if owner_id else set()
    if root and root["author_kind"] == "agent" and root["author_thread_id"]:
        legacy_recipients.add(str(root["author_thread_id"]))
    legacy_exempt = LEGACY_RECIPIENTS_SKIP_LOCALITY and recipient_id in legacy_recipients
    tagged = bool(db.execute("SELECT 1 FROM doc_comment_tags WHERE comment_id=? AND agent_id=?",
                             (comment["id"], recipient_id)).fetchone()) and comment.get("event") != "reopened"
    if legacy_exempt and not tagged:
        return None, True
    recipient = db.execute("SELECT id,model,pending_model,title FROM threads WHERE id=?", (recipient_id,)).fetchone()
    name = (recipient["title"] if recipient else None) or recipient_id
    if sender is None:
        # The writing agent's row is gone, so its locality can't be read: fail closed.
        return (f"The agent that wrote this comment no longer exists, so its Local only setting can't be checked "
                f"and the comment wasn't delivered to {name}."), legacy_exempt
    if recipient and send_needs_hold(dict(sender), dict(recipient), db=db, emit_hold_event=False):
        refusal = (f"{name} is cloud-bound or has no model selected, and Local only applies to this chat or project, "
                   "so the comment wasn't delivered. Send it with delegate_send, which holds it for the person's approval, "
                   "or send it to a local agent.")
        if emit and not legacy_exempt:
            from doceditor import live_events
            from infra.write_effects import after_commit

            # Not local_only.send_held: that event means held for the person's approval, and this is not delivered.
            refused = {"comment_id": comment["id"], "reply": bool(comment.get("parent_id"))}
            after_commit(db, lambda: live_events.emit("comment_local_only_refused", **refused))
        return refusal, legacy_exempt
    return None, legacy_exempt


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


def _person_display_name(db, rows: list[dict]) -> str | None:
    """Who a tag of the person names: the account that owns the document being commented on.

    Read once per call, never per row, and only when a row carries the mark: three key lookups.
    A workspace or project document (an owner sentinel) has no owning account, so it names the
    first account, as the conductor backfill does for an ownerless member. Their display name is
    the one their own comments carry as ``author``; no name is returned for an account that is gone.
    """
    tagged = next((row for row in rows if row.get("tags_person")), None)
    if tagged is None:
        return None
    # Every caller passes one artifact's rows, so one name covers the listing.
    artifact = get_artifact(str(tagged["artifact_id"]), db=db)
    if artifact is None:
        return None
    owner = artifact.get("parent_thread_id")
    if is_owner_sentinel(owner):
        first = next(iter(list_users(db=db)), None)
        return (get_user(str(first["id"]), db=db) or {}).get("name") if first else None
    account = db.execute("SELECT user_id FROM threads WHERE id=?", (str(owner),)).fetchone()
    return (get_user(str(account["user_id"]), db=db) or {}).get("name") if account else None


def tags_for_comments(db, rows: list[dict]) -> None:
    """Attach tag IDs to flat comment rows without per-row queries.

    ``tagged_agents`` and ``tagged_person`` are what a reader sees; ``tagged_agent_ids`` names who
    a comment is delivered to, which is never the person (their tag is the comment's own
    ``tags_person`` mark, not a row of this table).
    """
    if not rows:
        return
    by_id = {row["id"]: row for row in rows}
    for row in rows:
        row["tagged_agent_ids"] = []
        row["tagged_agents"] = []
        row["tagged_agent_mentions"] = []
        row["tagged_person"] = None
    person_name = _person_display_name(db, rows)
    if person_name:
        for row in rows:
            if row.get("tags_person"):
                row["tagged_person"] = person_name
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


def _withheld_authors(db, rows, recipient_id: str, *, reader: dict | None = None) -> set[str]:
    """Agent authors whose own comment to this recipient would be refused under Local only."""
    from infra.local_only import send_needs_hold

    authors = {str(row["author_thread_id"]) for row in rows
               if row["author_kind"] == "agent" and row["author_thread_id"]}
    if not authors:
        return set()
    rows = db.execute(
        'SELECT a.value AS author,t.id,t.local_only,t.project_id,p.cloud_models_off FROM json_each(?) a '
        'LEFT JOIN threads t ON t.id=a.value LEFT JOIN project_inference_policies p ON p.project_id=t.project_id',
        (json.dumps(list(authors)),),
    ).fetchall()
    withheld = {row['author'] for row in rows if row['id'] is None}
    candidates = [dict(row) for row in rows if row['id'] and (row['local_only'] or row['cloud_models_off'])]
    if candidates:
        recipient = db.execute('SELECT id,model,pending_model,title FROM threads WHERE id=?', (recipient_id,)).fetchone()
        recipient = {**dict(recipient), **(reader or {})} if recipient else None
        withheld.update(sender['id'] for sender in candidates if recipient is None
                        or send_needs_hold(sender, recipient, db=db, emit_hold_event=False))
    return withheld


def visible_comment_threads(db, threads: list[dict], recipient_id: str, *, reader: dict | None = None) -> list[dict]:
    """Filter Local-only comments after anchoring, retaining the context of visible replies."""
    rows = [item for root in threads for item in [root, *root.get('replies', [])]]
    withheld = _withheld_authors(db, rows, recipient_id, reader=reader)
    visible = []
    for root in threads:
        replies = [reply for reply in root.get('replies', []) if reply.get('author_thread_id') not in withheld]
        if root.get('author_thread_id') in withheld:
            if not replies:
                continue
            root = {**root, 'author': '(not shown: this agent is Local only)',
                    'body': '(not shown: this agent is Local only)'}
            if root.get('replacement') is not None:
                root['replacement'] = '(not shown: this agent is Local only)'
        visible.append({**root, 'replies': replies})
    return visible


def thread_context(db, item: dict, recipient_id: str) -> str:
    """Render earlier comments as bounded context for a tagged reply, never a Local only agent's text."""
    from plans.public import WORDS_MAX_BYTES
    from prompts.plan_tools import bounded_text

    root = root_id(item)
    rows = db.execute(
        """SELECT id,author,author_kind,author_thread_id,body,anchor_text,created_at FROM doc_comments
           WHERE id=? OR parent_id=? ORDER BY created_at,id""", (root, root),
    ).fetchall()
    withheld = _withheld_authors(db, rows, recipient_id)
    root_row = next((row for row in rows if row["id"] == root), None)
    header = ["Thread so far (context, not new feedback):"]
    if root_row and root_row["anchor_text"]:
        header.append(f'Highlighted: "{root_row["anchor_text"]}"')
    prior = []
    for row in rows:
        if row["id"] == item["id"]:
            break
        hidden = row["author_kind"] == "agent" and str(row["author_thread_id"]) in withheld
        prior.append(f'- {row["author"]}: ' + ("(not shown: this agent is Local only)" if hidden else row["body"]))
    measure = lambda value: len(value.encode("utf-8"))
    def marker(count: int) -> str:
        return f'… {count} earlier comments not shown; read them all with doc_comments_list(comment_id="{root}")'

    selected: list[str] = []
    for index in range(len(prior) - 1, -1, -1):
        candidate = [prior[index], *selected]
        dropped = index
        lines = header + ([marker(dropped)] if dropped else []) + candidate
        if measure("\n".join(lines)) <= WORDS_MAX_BYTES:
            selected = candidate
            continue
        dropped = index + 1
        while selected and measure("\n".join(header + [marker(dropped)] + selected)) > WORDS_MAX_BYTES:
            selected.pop(0)
            dropped += 1
        return bounded_text("\n".join(header + [marker(dropped)] + selected), WORDS_MAX_BYTES,
                            measure=measure, marker=marker(dropped))
    return bounded_text("\n".join(header + selected), WORDS_MAX_BYTES,
                        measure=measure, marker=marker(len(prior)))


def prepare_notification(db, items: list[dict], agent_id: str) -> None:
    """Count one delivered batch per comment thread and attach any due hint."""
    roots = set()
    context_roots = set()
    in_line = writer_line.counts(db, (root_id(item) for item in items if item.get("kind") == "comment"))
    for item in items:
        if item.get("kind") != "comment":
            continue
        root = root_id(item)
        tagged = not item.get("suppress_tag") and item.get("event") != "reopened" and db.execute(
            "SELECT 1 FROM doc_comment_tags WHERE comment_id=? AND agent_id=?",
            (item["id"], agent_id),
        ).fetchone()
        if tagged:
            reinstate(db, item, agent_id)
            if waiting := in_line.get(root, 0):
                item["line_hint"] = f"{waiting} {'writer' if waiting == 1 else 'writers'} in line on this thread."
            if item.get("parent_id") and root not in context_roots:
                item["thread_context"] = thread_context(db, item, agent_id)
                context_roots.add(root)
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
