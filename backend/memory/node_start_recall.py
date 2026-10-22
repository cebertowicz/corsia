"""Earlier words from the person that match a task when it starts.

The lookup is advisory, bounded, and synchronous.  It never changes a plan
operation's outcome: failures are logged and simply omit the optional block.
"""

from __future__ import annotations


from datetime import UTC, datetime
from functools import lru_cache
import json
import re
import sqlite3
from time import perf_counter


from core.correction_notes import local_clock
from core.corrections_recording import (
    decision_answer_arrived_at_sql,
    is_owner_authored_message,
    owner_message_sql_predicate,
)
from infra.checkpoint_queries import current_installed_through_seq

from llm.locality import cloud_off_project_ids
from memory.checkpoint_search import caller_user_id
from memory.node_start_recall_telemetry import Hit, log_lookup, log_lookup_error
from memory.node_text import node_text as _node_text
from memory.task_match import NodeText, TaskTerms, first_term_at, match, task_terms

MAX_HITS = 3
SOURCE_CANDIDATES = 100
RULE_FIELDS = frozenset({"active_constraints", "approvals_and_preferences"})
RESULT_KEY = "prior_words"
_NODE_OPERATIONS = frozenset({"start", "activate", "add", "insert"})
_STARTED_HOWS = frozenset({"started", "auto_selected"})
_HEADER = "Earlier words from the person that match this task, newest first; where they differ, the newer one governs."
_MATCH_FUNCTION = "prior_words_match"
_OWNER_FUNCTION = "prior_words_owner"



def related(
    db: sqlite3.Connection,
    text: NodeText,
    *,
    caller_thread_id: str,
    trigger: str,
) -> list[str]:
    """Return prior person words for ``text`` or no lines on any failure."""
    started = perf_counter()
    try:
        reason, hits = _related(db, text, caller_thread_id)
    except Exception as exc:  # noqa: BLE001 - advisory context must not fail its caller
        log_lookup_error(trigger, exc, started, caller_thread_id)
        return []
    log_lookup(reason, trigger, hits, started, caller_thread_id)
    return [_HEADER, *(hit.line for hit in hits)] if hits else []


def attach_to_result(
    db: sqlite3.Connection, operation: str, result: object, *, caller_thread_id: str, checkpoint_generation_id: str | None = None) -> None:
    """Attach ``prior_words`` to a plan result that starts a task or raises a decision."""
    result = getattr(result, "result", result)
    if not isinstance(result, dict):
        return
    try:
        subject = _subject(db, operation, result)
    except Exception as exc:  # noqa: BLE001 - advisory context must not fail its caller
        log_lookup_error(operation, exc, perf_counter(), caller_thread_id)
        return
    if subject is None:
        return
    text, trigger = subject
    lines = related(db, text, caller_thread_id=caller_thread_id, trigger=trigger)
    if lines:
        result.update({RESULT_KEY: lines})
    node = result.get("node") if isinstance(result.get("node"), dict) else None
    node_started = (node is not None and node.get("kind") == "task") or _started_node_id(result.get("next")) == text.node_id
    if not node_started or text.node_id is None:
        return
    from plans.trial_lessons import attach_trial_lessons_advisory

    attach_trial_lessons_advisory(
        db, result, thread_id=caller_thread_id, node_id=text.node_id,
        checkpoint_generation_id=checkpoint_generation_id,
    )


def related_for_next(db: sqlite3.Connection, block: dict, *, caller_thread_id: str) -> list[str]:
    """Return prior words for the task a wake's ``next`` block reports started."""
    node_id = _started_node_id(block)
    if node_id is None:
        return []
    try:
        text = _node_text(db, node_id)
    except Exception as exc:  # noqa: BLE001 - advisory context must not fail its caller
        log_lookup_error("wake", exc, perf_counter(), caller_thread_id)
        return []
    return [] if text is None else related(db, text, caller_thread_id=caller_thread_id, trigger="wake")


def _subject(db, operation: str, result: dict) -> tuple[NodeText, str] | None:
    if operation in _NODE_OPERATIONS and isinstance(result.get("node"), dict):
        node_id = result["node"].get("id")
        text = _node_text(db, str(node_id)) if node_id else None
        return None if text is None else (text, operation)
    if operation == "raise" and isinstance(result.get("decision"), dict):
        decision = result["decision"]
        return NodeText(str(decision.get("title") or ""), intent=str(decision.get("context") or "")), "decision_raise"
    node_id = _started_node_id(result.get("next"))
    text = _node_text(db, node_id) if node_id else None
    return None if text is None else (text, "close_next")


def _started_node_id(block: object) -> str | None:
    selected = block.get("selected") if isinstance(block, dict) else None
    if not isinstance(selected, dict) or selected.get("how") not in _STARTED_HOWS:
        return None
    return str(selected["id"])


def _related(db, text: NodeText, caller_thread_id: str) -> tuple[str, list[Hit]]:
    terms = task_terms(text)
    if len(terms.terms) < 2 or not terms.title_terms:
        return "no_terms", []
    user_id = caller_user_id(db, caller_thread_id)
    caller = db.execute("SELECT project_id,local_only FROM threads WHERE id=?", (caller_thread_id,)).fetchone()
    if user_id is None or caller is None:
        return "unresolved_caller", []
    cutoff = _start_cutoff(db, text.node_id)
    cloud_off = cloud_off_project_ids(db=db)
    _ensure_functions(db)

    candidates = [*_message_candidates(db, terms, user_id, caller_thread_id, caller, cloud_off, cutoff),
                  *_comment_candidates(db, terms, user_id, caller, cloud_off, cutoff),
                  *_decision_candidates(db, terms, user_id, caller, cloud_off, cutoff),
                  *_rule_candidates(db, terms, user_id, caller_thread_id, caller, cloud_off, cutoff)]
    if not candidates:
        return "below_floor", []
    # Scores are primary, then newest sources, then direct person words ahead of a rule.
    candidates.sort(
        key=lambda hit: (-hit.matched.shared, -hit.matched.title_shared, _newest_first(hit.created_at), hit.kind == "rule"),
    )
    kept: list[Hit] = []
    rules = 0
    for hit in candidates:
        if hit.kind == "rule" and rules >= 1:
            continue
        kept.append(hit)
        rules += hit.kind == "rule"
        if len(kept) == MAX_HITS:
            break
    kept.sort(key=lambda hit: _newest_first(hit.created_at))
    return "delivered", kept


def _start_cutoff(db, node_id: str | None) -> str | None:
    if not node_id:
        return None
    row = db.execute("SELECT updated_at FROM plan_nodes WHERE id=?", (node_id,)).fetchone()
    return str(row["updated_at"]) if row and row["updated_at"] else None


def _ensure_functions(db) -> None:
    """Add the matcher and owner check to ``db`` once; SQLite refuses to redefine one under an open statement."""
    try:
        db.execute(f"SELECT {_MATCH_FUNCTION}('[[],[]]','')").fetchone()
    except sqlite3.OperationalError as exc:
        if "no such function" not in str(exc):
            raise
        db.create_function(_MATCH_FUNCTION, 2, _sql_match, deterministic=True)
        db.create_function(_OWNER_FUNCTION, 3, _sql_owner, deterministic=True)


@lru_cache(maxsize=8)
def _decoded(encoded: str) -> TaskTerms:
    terms, title_terms = json.loads(encoded)
    return TaskTerms(tuple(terms), frozenset(title_terms))


def _encoded(terms: TaskTerms) -> str:
    return json.dumps([list(terms.terms), sorted(terms.title_terms)])


def _sql_match(encoded: str, words: str | None) -> bool:
    """``match()`` as SQL, so each reader's ``LIMIT`` counts only rows it accepts."""
    return match(_decoded(encoded), words or "") is not None


def _sql_owner(role: str | None, content: str | None, metadata: str | None) -> bool:
    return is_owner_authored_message(role=role, content=content, metadata=metadata)


def _fts_title(terms: TaskTerms) -> str:
    """An FTS query for user messages whose content holds a title term.

    Only narrows the scan: every message ``match()`` accepts has a word starting with a title term.
    """
    title = " OR ".join(f'"{term}"*' for term in terms.terms if term in terms.title_terms)
    return f"role:user AND content:({title})"


def _positional(clause: str, params: dict[str, str]) -> tuple[str, list[str]]:
    """Bind a named-placeholder clause by name so it can sit among ``?`` markers."""
    names = re.findall(r":(\w+)", clause)
    return re.sub(r":\w+", "?", clause), [params[name] for name in names]


def _matching(column: str, terms: TaskTerms) -> tuple[str, list[object]]:
    """The predicate keeping rows whose ``column`` ``match()`` accepts."""
    return f"AND {_MATCH_FUNCTION}(?,{column})", [_encoded(terms)]


def _owned_threads(alias: str) -> str:
    return f"{alias}.user_id=? AND {alias}.purged_at IS NULL"


def _source_scope(alias: str, timestamp: str, caller: sqlite3.Row, cloud_off: set[str], cutoff: str | None) -> tuple[str, list[object]]:
    """Return the locality and start-window predicates every source reader applies before its limit."""
    return (
        f"""AND (COALESCE({alias}.local_only, 0)=0 OR ?=1)
              AND ({alias}.project_id IS NULL OR {alias}.project_id=?
                   OR {alias}.project_id NOT IN (SELECT value FROM json_each(?)))
              AND (? IS NULL OR {timestamp}<=?)""",
        [int(bool(caller["local_only"])), caller["project_id"], json.dumps(sorted(cloud_off)), cutoff, cutoff],
    )


def source_reaches_thread(db: sqlite3.Connection, source_thread_id: str, recipient_thread_id: str) -> bool:
    """Whether the person's words in one thread may reach another thread, by the rule every source reader applies."""
    user_id = caller_user_id(db, recipient_thread_id)
    recipient = db.execute("SELECT project_id,local_only FROM threads WHERE id=?", (recipient_thread_id,)).fetchone()
    if user_id is None or recipient is None:
        return False
    source_scope, params = _source_scope("t", "t.id", recipient, cloud_off_project_ids(db=db), None)
    return db.execute(
        f"SELECT 1 FROM threads t WHERE t.id=? AND {_owned_threads('t')} {source_scope}",
        [source_thread_id, user_id, *params],
    ).fetchone() is not None


def _message_candidates(db, terms, user_id, caller_thread_id, caller, cloud_off, cutoff) -> list[Hit]:
    owner_filter, owner_params = _positional(*owner_message_sql_predicate("m"))
    source_scope, source_params = _source_scope("t", "m.created_at", caller, cloud_off, cutoff)
    caller_cut = current_installed_through_seq(caller_thread_id, db=db) or 0
    matching, match_params = _matching("m.content", terms)
    rows = db.execute(
        f"""SELECT m.id,m.thread_id,m.content,m.created_at,t.title
            FROM messages_fts JOIN messages m ON m.rowid=messages_fts.rowid
            JOIN threads t ON t.id=m.thread_id
            WHERE messages_fts MATCH ? AND {owner_filter} AND m.delivery_state='delivered'
              AND {_owned_threads('t')} {source_scope}
              AND (m.thread_id<>? OR m.seq IS NOT NULL AND m.seq<=?)
              AND {_OWNER_FUNCTION}(m.role,m.content,m.metadata) {matching}
            ORDER BY m.created_at DESC LIMIT ?""",
        [_fts_title(terms), *owner_params, user_id, *source_params,
         caller_thread_id, caller_cut, *match_params, SOURCE_CANDIDATES],
    ).fetchall()
    hits = []
    for row in rows:
        found = match(terms, str(row["content"]))
        if found:
            line = f'{local_clock(str(row["created_at"]))} · message in “{row["title"]}” · “{_excerpt(found)}” → get_thread_history(thread_id="{row["thread_id"]}")'
            hits.append(Hit(found, str(row["created_at"]), "message", line))
    return hits


def _comment_candidates(db, terms, user_id, caller, cloud_off, cutoff) -> list[Hit]:
    from core.correction_targets import comment_target_sql

    source_scope, source_params = _source_scope("t", "c.created_at", caller, cloud_off, cutoff)
    matching, match_params = _matching("c.body", terms)
    rows = db.execute(
        f"""SELECT c.id,c.artifact_id,c.body,c.created_at,ai.title,ai.path
            FROM doc_comments c JOIN threads t ON t.id=({comment_target_sql('c')})
            LEFT JOIN artifact_identities ai ON ai.id=c.artifact_id
            WHERE c.author_kind='user' AND {_owned_threads('t')} {source_scope} {matching}
            ORDER BY c.created_at DESC,c.id DESC LIMIT ?""",
        [user_id, *source_params, *match_params, SOURCE_CANDIDATES],
    ).fetchall()
    hits = []
    for row in rows:
        found = match(terms, str(row["body"]))
        if found:
            about = row["title"] or row["path"] or row["artifact_id"]
            line = f'{local_clock(str(row["created_at"]))} · comment on “{about}” · “{_excerpt(found)}”'
            hits.append(Hit(found, str(row["created_at"]), "comment", line))
    return hits


def _decision_candidates(db, terms, user_id, caller, cloud_off, cutoff) -> list[Hit]:
    from plans.public import DECISION_TYPE

    answer = "json_extract(ai.metadata,'$.plan_decision.answer.text')"
    answered_at = decision_answer_arrived_at_sql("ai")
    source_scope, source_params = _source_scope("t", answered_at, caller, cloud_off, cutoff)
    matching, match_params = _matching(answer, terms)
    rows = db.execute(
        f"""SELECT ai.external_id,json_extract(ai.metadata,'$.plan_decision.title') AS title,
                   {answer} AS answer,{answered_at} AS answered_at
            FROM artifact_identities ai
            JOIN threads t ON t.id=json_extract(ai.metadata,'$.plan_decision.thread_id')
            WHERE ai.integration='local' AND ai.type=? AND ai.state='active'
              AND json_extract(ai.metadata,'$.plan_decision.state')='answered'
              AND json_type(ai.metadata,'$.plan_decision.answer.text')='text'
              AND {_owned_threads('t')} {source_scope} {matching}
            ORDER BY answered_at DESC LIMIT ?""",
        [DECISION_TYPE, user_id, *source_params, *match_params, SOURCE_CANDIDATES],
    ).fetchall()
    hits = []
    for row in rows:
        text, created_at = str(row["answer"]), str(row["answered_at"])
        found = match(terms, text)
        if found:
            line = f'{local_clock(created_at)} · answer to decision “{row["title"] or row["external_id"]}” · “{_excerpt(found)}”'
            hits.append(Hit(found, created_at, "decision", line))
    return hits


def _rule_candidates(db, terms, user_id, caller_thread_id, caller, cloud_off, cutoff) -> list[Hit]:
    # Both rule fields are statement entries (core.continuity_types), so the statement is the rule's text.
    statement = "json_extract(ce.body,'$.statement')"
    source_scope, source_params = _source_scope("t", "ce.updated_at", caller, cloud_off, cutoff)
    fields = sorted(RULE_FIELDS)
    matching, match_params = _matching(statement, terms)
    rows = db.execute(
        f"""WITH top AS (
              SELECT ce.thread_id,ce.history_epoch,ce.entry_id,ce.revision,ce.updated_at,{statement} AS statement
              FROM continuity_entries ce JOIN threads t ON t.id=ce.thread_id
              WHERE {_owned_threads('t')} AND ce.state='active'
                AND ce.field IN (?,?)
                AND EXISTS (SELECT 1 FROM json_each(ce.body,'$.source_refs')
                            WHERE json_extract(value,'$.kind')='user')
                AND NOT (ce.thread_id=? AND ce.history_epoch=t.history_epoch)
                {source_scope} {matching}
              ORDER BY ce.updated_at DESC LIMIT ?)
            SELECT top.*,checkpoints.id AS checkpoint_id
            FROM top LEFT JOIN checkpoint_entry_search entry_index
              ON entry_index.thread_id=top.thread_id AND entry_index.history_epoch=top.history_epoch
             AND entry_index.entry_id=top.entry_id AND entry_index.revision=top.revision
            LEFT JOIN checkpoints ON checkpoints.generation_order=entry_index.last_generation_order
            ORDER BY top.updated_at DESC""",
        [user_id, *fields, caller_thread_id, *source_params, *match_params, SOURCE_CANDIDATES],
    ).fetchall()
    hits = []
    for row in rows:
        found = match(terms, str(row["statement"] or ""))
        if found:
            pointer = f' → get_checkpoints(checkpoint_id="{row["checkpoint_id"]}")' if row["checkpoint_id"] else ""
            line = f'{local_clock(str(row["updated_at"]))} · rule an agent recorded from their words (thread {str(row["thread_id"])[:8]}) · “{_excerpt(found)}”{pointer}'
            hits.append(Hit(found, str(row["updated_at"]), "rule", line))
    return hits


def _excerpt(found: Match) -> str:
    text = " ".join(found.span.split())
    if len(text) <= 280:
        return text
    first = first_term_at(found.matched_terms, text)
    start = max(0, min(first - 120, len(text) - 277))
    end = min(len(text), start + 277)
    prefix = "…" if start else ""
    suffix = "…" if end < len(text) else ""
    return prefix + text[start:end].strip() + suffix


def _newest_first(created_at: str) -> float:
    try:
        value = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        return -value.replace(tzinfo=value.tzinfo or UTC).timestamp()
    except ValueError:
        return 0