"""Server-resolved, append-only evidence for a plan node's intent."""
from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable
from typing import Any

import plans.service as service

MAX_INTENT_BYTES = 600
MAX_QUOTE_BYTES = 400
_WORD_AUTHORS = frozenset({"person", "agent"})
_WORD_ROLES = frozenset({"origin", "bears_on", "authority"})
_WS = re.compile(r"\s+")


def _normal(value: str) -> str:
    return _WS.sub(" ", value).strip()


def _limited(value: object, *, field: str, limit: int) -> str:
    if not isinstance(value, str) or not (text := value.strip()):
        raise service.PlanInputError(f"{field} is required")
    if len(text.encode()) > limit:
        raise service.PlanInputError(f"{field} exceeds the {limit}-byte limit")
    return text


def validate_intent(value: object) -> str:
    return _limited(value, field="intent", limit=MAX_INTENT_BYTES)


def _project_id(connection: sqlite3.Connection, plan_id: str) -> str | None:
    row = connection.execute(
        "SELECT t.project_id FROM plans p JOIN threads t ON t.id=p.owner_id WHERE p.id=?",
        (plan_id,),
    ).fetchone()
    return None if row is None else row["project_id"]


def _phrase(quote: str) -> str:
    return '"' + quote.replace('"', '""') + '"'


def _is_person_message(row: sqlite3.Row) -> bool:
    # User-role rows can carry an agent relay; that is not the person's words.
    return row["role"] == "user" and '"sender_kind":"agent"' not in (row["metadata"] or "").replace(" ", "")


def _is_agent_message(row: sqlite3.Row) -> bool:
    """Whether a consumed message is an agent's words, not a person's."""
    return row["role"] == "assistant" or '"sender_kind":"agent"' in (row["metadata"] or "").replace(" ", "")


def _message_quote(connection: sqlite3.Connection, plan_id: str, quote: str):
    rows = connection.execute(
        """SELECT m.id,m.content,m.role,m.metadata,m.created_at FROM messages m
           JOIN messages_fts f ON f.rowid=m.rowid
           JOIN threads t ON t.id=m.thread_id
           WHERE messages_fts MATCH ? AND t.project_id IS ?
           ORDER BY m.created_at DESC LIMIT 20""",
        (_phrase(quote), _project_id(connection, plan_id)),
    ).fetchall()
    wanted = _normal(quote)
    return next(
        (row for row in rows if _is_person_message(row) and wanted in _normal(row["content"])),
        None,
    )


def _comment_quote(connection: sqlite3.Connection, plan_id: str, quote: str):
    rows = connection.execute(
        """SELECT c.id,c.body,c.created_at FROM doc_comments c
           JOIN artifact_registrations r ON r.artifact_id=c.artifact_id
           JOIN threads t ON t.id=r.parent_thread_id
           WHERE c.author_kind='user' AND t.project_id IS ?
           ORDER BY c.created_at DESC LIMIT 20""",
        (_project_id(connection, plan_id),),
    ).fetchall()
    wanted = _normal(quote)
    return next((row for row in rows if wanted in _normal(row["body"])), None)


def _decision_quote(connection: sqlite3.Connection, plan_id: str, quote: str):
    rows = connection.execute(
        """SELECT a.id,a.external_id,a.metadata,a.updated_at FROM artifact_identities a
           JOIN threads t ON t.id=json_extract(a.metadata, '$.plan_decision.thread_id')
           WHERE a.integration='local' AND a.type='plan-decision'
             AND t.project_id IS ?
             AND json_extract(a.metadata, '$.plan_decision.answer') IS NOT NULL
           ORDER BY a.updated_at DESC LIMIT 20""",
        (_project_id(connection, plan_id),),
    ).fetchall()
    wanted = _normal(quote)
    for row in rows:
        answer = connection.execute(
            "SELECT json_extract(?, '$.plan_decision.answer')", (row["metadata"],)
        ).fetchone()[0]
        if isinstance(answer, str) and wanted in _normal(answer):
            return row
    return None


def resolve_words(
    connection: sqlite3.Connection, plan_id: str, words: Iterable[dict[str, Any]] | None,
    *, actor_id: str | None,
) -> list[dict[str, str]]:
    """Resolve caller-provided quotations to their newest authoritative source."""
    if words is None:
        return []
    if not isinstance(words, list) or not words:
        raise service.PlanInputError("intent_words must be a nonempty array")
    resolved: list[dict[str, str]] = []
    for raw in words:
        if not isinstance(raw, dict) or set(raw) - {"quote", "author", "role"}:
            raise service.PlanInputError("intent_words items require quote, author?, and role?")
        quote = _limited(raw.get("quote"), field="quote", limit=MAX_QUOTE_BYTES)
        author = raw.get("author", "person")
        role = raw.get("role", "bears_on")
        if author not in _WORD_AUTHORS:
            raise service.PlanInputError("intent word author must be person or agent")
        if role not in _WORD_ROLES:
            raise service.PlanInputError("intent word role must be origin, bears_on, or authority")
        if author == "agent":
            resolved.append({"quote": quote, "author": author, "role": role,
                             "source_kind": "message", "source_id": actor_id or "agent"})
            continue
        message = _message_quote(connection, plan_id, quote)
        if message is not None:
            resolved.append({"quote": quote, "author": author, "role": role,
                             "source_kind": "message", "source_id": message["id"]})
            continue
        comment = _comment_quote(connection, plan_id, quote)
        if comment is not None:
            resolved.append({"quote": quote, "author": author, "role": role,
                             "source_kind": "doc_comment", "source_id": comment["id"]})
            continue
        decision = _decision_quote(connection, plan_id, quote)
        if decision is not None:
            resolved.append({"quote": quote, "author": author, "role": role,
                             "source_kind": "decision_answer", "source_id": decision["external_id"]})
            continue
        raise service.PlanInputError(
            "quote was not found; quote exactly, or pass it as your own words with author: agent"
        )
    return resolved


def append(connection: sqlite3.Connection, node_id: str, words: Iterable[dict[str, str]], *, revision: int) -> None:
    for word in words:
        connection.execute(
            """INSERT INTO plan_node_intent_words
            (node_id,author,source_kind,source_id,quote,role,requirements_revision,added_at)
            VALUES (?,?,?,?,?,?,?,?)""",
            (node_id, word["author"], word["source_kind"], word["source_id"], word["quote"],
             word["role"], revision, service._now()),
        )


def bounded_text(value: str, limit: int) -> str:
    """Keep one Unicode string within a byte limit without splitting a character."""
    return value.encode()[:limit].decode(errors="ignore")


def document_comment_words(items: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    """Project loaded person document comments into bounded origin words."""
    return [
        {
            "author": "person", "role": "origin", "source_kind": "doc_comment",
            "source_id": str(item["id"]),
            "quote": bounded_text(str(item["body"]), MAX_QUOTE_BYTES),
        }
        for item in items if item["kind"] == "comment"
    ]


def document_comment_intent(items: Iterable[dict[str, Any]]) -> str:
    """Make one bounded task reading from loaded person document comments."""
    comments = [str(item["body"]) for item in items if item["kind"] == "comment"]
    return validate_intent(bounded_text(
        "User document feedback: " + "\n".join(comments), MAX_INTENT_BYTES,
    ))


def has_person_origin(connection: sqlite3.Connection, turn_id: str | None) -> bool:
    if turn_id is None:
        return False
    return any(_is_person_message(row) for row in connection.execute(
        "SELECT role,metadata FROM messages WHERE active_turn_id=?", (turn_id,)
    ))


def capture_origin(connection: sqlite3.Connection, plan_id: str, node_id: str, *, turn_id: str | None,
                   actor_id: str | None, revision: int) -> bool:
    """Append every input consumed by this turn and report whether one is human-authored."""
    if turn_id is None:
        return False
    rows = connection.execute(
        """SELECT id,content,role,metadata FROM messages
           WHERE active_turn_id=? ORDER BY input_order,id""", (turn_id,)
    ).fetchall()
    person = [row for row in rows if _is_person_message(row)]
    if person:
        append(connection, node_id, (
            {"quote": row["content"], "author": "person", "role": "origin",
             "source_kind": "message", "source_id": row["id"]}
            for row in person
        ), revision=revision)
        return True
    agent = next((row for row in rows if _is_agent_message(row)), None)
    if agent is not None:
        append(connection, node_id, [{"quote": agent["content"], "author": "agent", "role": "origin",
                                      "source_kind": "message", "source_id": agent["id"]}], revision=revision)
    return False


def canonical(connection: sqlite3.Connection, node_id: str):
    row = service._node_row(connection, node_id)
    seen: set[str] = set()
    while row["intent_node_id"] is not None:
        if row["id"] in seen:
            raise service.PlanError("intent node reference cycle")
        seen.add(row["id"])
        row = service._node_row(connection, row["intent_node_id"])
    return row


def inherited_node_id(connection: sqlite3.Connection, plan_id: str) -> str | None:
    """Return the attached parent task's canonical intent for a worker plan."""
    owner = connection.execute("SELECT owner_kind,owner_id FROM plans WHERE id=?", (plan_id,)).fetchone()
    if owner is None or owner["owner_kind"] != "thread":
        return None
    parent = connection.execute(
        """SELECT id FROM plan_nodes WHERE spawned_thread_id=? AND archived_at IS NULL
           ORDER BY updated_at DESC LIMIT 1""", (owner["owner_id"],)
    ).fetchone()
    return None if parent is None else canonical(connection, parent["id"])["id"]


def detail(connection: sqlite3.Connection, node: sqlite3.Row) -> dict[str, Any]:
    result = {
        "intent": node["intent"],
        "intent_words": newest(connection, node["id"]),
        "requirements_revision": node["requirements_revision"],
    }
    if node["intent_node_id"] is not None:
        source = canonical(connection, node["id"])
        result["canonical_intent"] = {
            "node_id": source["id"], "title": source["title"], "intent": source["intent"],
            "intent_words": newest(connection, source["id"]),
        }
    return result


def newest(connection: sqlite3.Connection, node_id: str, *, limit: int = 3) -> list[dict[str, Any]]:
    """Return recent words while retaining the task's origin or authority."""
    rows = connection.execute(
        """SELECT author,role,quote,added_at FROM plan_node_intent_words
           WHERE node_id=? AND removed_at IS NULL ORDER BY added_at DESC,rowid DESC""",
        (node_id,),
    ).fetchall()
    anchor = next((row for row in rows if row["role"] in {"authority", "origin"}), None)
    selected = ([anchor] if anchor is not None else []) + [
        row for row in rows if row is not anchor
    ][:limit - (1 if anchor is not None else 0)]
    return [{"author": row["author"], "role": row["role"], "quote": row["quote"].encode()[:240].decode(errors="ignore"),
             "added_at": row["added_at"]} for row in selected]
