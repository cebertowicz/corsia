"""Earlier words from the person that match a task when it starts.

The lookup is advisory, bounded, and synchronous.  It never changes a plan
operation's outcome: failures are logged and simply omit the optional block.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import sqlite3
from time import perf_counter

from loguru import logger as log

from core.correction_notes import local_clock
from core.corrections_recording import is_owner_authored_message, owner_message_sql_predicate
from infra.checkpoint_entry_index import NO_REVISION
from infra.product_events import emit_product_event
from llm.locality import cloud_off_project_ids
from memory.checkpoint_entry_rows import decoded_entries, entry_rank_rows_matching, hydrate_entry_rows
from memory.checkpoint_search import caller_user_id
from memory.task_match import Match, NodeText, TaskTerms, floor, match, task_terms

MAX_HITS = 3
SOURCE_CANDIDATES = 100
RULE_FIELDS = frozenset({"active_constraints", "approvals_and_preferences"})
RESULT_KEY = "prior_words"
_NODE_OPERATIONS = frozenset({"start", "activate", "add", "insert"})
_STARTED_HOWS = frozenset({"started", "auto_selected"})
_HEADER = "Earlier words from the person that match this task, newest first; where they differ, the newer one governs."


@dataclass(frozen=True)
class _Hit:
    matched: Match
    created_at: str
    kind: str
    line: str
    project_id: str | None
    local_only: bool


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
        _log_error(trigger, exc, started, caller_thread_id)
        return []
    _log(reason, trigger, hits, started, caller_thread_id)
    return [_HEADER, *(hit.line for hit in hits)] if hits else []


def attach_to_result(
    db: sqlite3.Connection, operation: str, result: object, *, caller_thread_id: str,
) -> None:
    """Attach ``prior_words`` to a plan result that starts a task or raises a decision."""
    result = getattr(result, "result", result)
    if not isinstance(result, dict):
        return
    try:
        subject = _subject(db, operation, result)
    except Exception as exc:  # noqa: BLE001 - advisory context must not fail its caller
        _log_error(operation, exc, perf_counter(), caller_thread_id)
        return
    if subject is None:
        return
    text, trigger = subject
    lines = related(db, text, caller_thread_id=caller_thread_id, trigger=trigger)
    if lines:
        result[RESULT_KEY] = lines


def related_for_next(
    db: sqlite3.Connection, block: dict, *, caller_thread_id: str,
) -> list[str]:
    """Return prior words for the task a wake's ``next`` block reports started."""
    node_id = _started_node_id(block)
    if node_id is None:
        return []
    try:
        text = _node_text(db, node_id)
    except Exception as exc:  # noqa: BLE001 - advisory context must not fail its caller
        _log_error("wake", exc, perf_counter(), caller_thread_id)
        return []
    return [] if text is None else related(db, text, caller_thread_id=caller_thread_id, trigger="wake")


def _subject(db, operation: str, result: dict) -> tuple[NodeText, str] | None:
    if operation in _NODE_OPERATIONS and isinstance(result.get("node"), dict):
        node = result["node"]
        return NodeText(str(node.get("title") or ""), criteria=str(node.get("acceptance_criteria") or ""), node_id=node.get("id")), operation
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


def _node_text(db, node_id: str) -> NodeText | None:
    row = db.execute(
        "SELECT id,title,acceptance_criteria FROM plan_nodes WHERE id=?", (node_id,),
    ).fetchone()
    if row is None:
        return None
    return NodeText(str(row["title"]), criteria=str(row["acceptance_criteria"] or ""), node_id=str(row["id"]))


def _related(db, text: NodeText, caller_thread_id: str) -> tuple[str, list[_Hit]]:
    terms = task_terms(text)
    if len(terms.terms) < 2 or not terms.title_terms:
        return "no_terms", []
    user_id = caller_user_id(db, caller_thread_id)
    caller = db.execute("SELECT project_id,local_only FROM threads WHERE id=?", (caller_thread_id,)).fetchone()
    if user_id is None or caller is None:
        return "no_terms", []
    cutoff = _start_cutoff(db, text.node_id)
    cloud_off = cloud_off_project_ids(db=db)

    candidates = [*_message_candidates(db, terms, user_id, caller_thread_id),
                  *_comment_candidates(db, terms, user_id),
                  *_decision_candidates(db, terms, user_id),
                  *_rule_candidates(db, terms, user_id, caller_thread_id)]
    candidates = [
        hit for hit in candidates
        if _before(hit.created_at, cutoff) and _deliverable(hit, caller, cloud_off)
    ]
    if not candidates:
        return "below_floor", []
    # Scores are primary, then newest sources, then direct person words ahead of a rule.
    candidates.sort(
        key=lambda hit: (-hit.matched.shared, -hit.matched.title_shared, _newest_first(hit.created_at), hit.kind == "rule"),
    )
    kept: list[_Hit] = []
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


def _fts_terms(terms: TaskTerms) -> str:
    return " OR ".join(f'"{term}"*' for term in terms.terms)


def _shared_terms(column: str, terms: TaskTerms) -> tuple[str, list[str]]:
    """Count the terms ``column`` contains; never below ``match()``'s count, so the floor drops no hit."""
    return "+".join(f"({column} LIKE ?)" for _ in terms.terms), [f"%{term}%" for term in terms.terms]


def _owned_threads(alias: str) -> str:
    return f"{alias}.user_id=? AND {alias}.purged_at IS NULL"


def _message_candidates(db, terms, user_id, caller_thread_id) -> list[_Hit]:
    owner_filter, owner_params = owner_message_sql_predicate("m")
    rows = db.execute(
        f"""SELECT m.id,m.thread_id,m.content,m.role,m.metadata,m.created_at,t.title,t.project_id,t.local_only
            FROM messages_fts JOIN messages m ON m.rowid=messages_fts.rowid
            JOIN threads t ON t.id=m.thread_id
            WHERE messages_fts MATCH ? AND {owner_filter} AND m.delivery_state='delivered'
              AND {_owned_threads('t')}
              AND (m.thread_id<>? OR m.seq IS NOT NULL AND m.seq<=COALESCE((
                SELECT MAX(c.through_seq) FROM checkpoints c JOIN threads ct ON ct.id=c.thread_id
                WHERE c.thread_id=? AND c.status='installed' AND c.history_epoch=ct.history_epoch
              ), 0))
            ORDER BY bm25(messages_fts),m.created_at DESC LIMIT ?""",
        [f"role:user AND ({_fts_terms(terms)})", *owner_params.values(), user_id, caller_thread_id, caller_thread_id, SOURCE_CANDIDATES],
    ).fetchall()
    hits = []
    for row in rows:
        if not is_owner_authored_message(role=row["role"], content=row["content"], metadata=row["metadata"]):
            continue
        found = match(terms, str(row["content"]))
        if found:
            line = f'{local_clock(str(row["created_at"]))} · message in “{row["title"]}” · “{_excerpt(found)}” → get_thread_history(thread_id="{row["thread_id"]}")'
            hits.append(_Hit(found, str(row["created_at"]), "message", line, row["project_id"], bool(row["local_only"])))
    return hits


def _comment_candidates(db, terms, user_id) -> list[_Hit]:
    from core.correction_targets import comment_target_sql

    shared, params = _shared_terms("c.body", terms)
    rows = db.execute(
        f"""SELECT * FROM (
              SELECT c.id,c.artifact_id,c.body,c.created_at,ai.title,ai.path,t.project_id,t.local_only,{shared} AS shared
              FROM doc_comments c JOIN threads t ON t.id=({comment_target_sql('c')})
              LEFT JOIN artifact_identities ai ON ai.id=c.artifact_id
              WHERE c.author_kind='user' AND {_owned_threads('t')})
            WHERE shared>=? ORDER BY shared DESC,created_at DESC,id DESC LIMIT ?""",
        [*params, user_id, floor(len(terms.terms)), SOURCE_CANDIDATES],
    ).fetchall()
    hits = []
    for row in rows:
        found = match(terms, str(row["body"]))
        if found:
            about = row["title"] or row["path"] or row["artifact_id"]
            line = f'{local_clock(str(row["created_at"]))} · comment on “{about}” · “{_excerpt(found)}”'
            hits.append(_Hit(found, str(row["created_at"]), "comment", line, row["project_id"], bool(row["local_only"])))
    return hits


def _decision_candidates(db, terms, user_id) -> list[_Hit]:
    from plans.public import DECISION_TYPE, answer_note

    shared, params = _shared_terms("json_extract(ai.metadata,'$.plan_decision.answer.text')", terms)
    rows = db.execute(
        f"""SELECT * FROM (
              SELECT ai.external_id,ai.metadata,t.project_id,t.local_only,{shared} AS shared,
                     json_extract(ai.metadata,'$.plan_decision.answered_at') AS answered_at
              FROM artifact_identities ai
              JOIN threads t ON t.id=json_extract(ai.metadata,'$.plan_decision.thread_id')
              WHERE ai.integration='local' AND ai.type=? AND ai.state='active'
                AND json_extract(ai.metadata,'$.plan_decision.state')='answered'
                AND {_owned_threads('t')})
            WHERE shared>=? ORDER BY shared DESC,answered_at DESC LIMIT ?""",
        [*params, DECISION_TYPE, user_id, floor(len(terms.terms)), SOURCE_CANDIDATES],
    ).fetchall()
    hits = []
    for row in rows:
        try:
            body = json.loads(row["metadata"]).get("plan_decision")
        except (TypeError, ValueError, AttributeError):
            continue
        if not isinstance(body, dict):
            continue
        text = answer_note(body.get("answer"))
        created_at = str(body.get("answered_at") or "")
        if not text or not created_at:
            continue
        found = match(terms, text)
        if found:
            line = f'{local_clock(created_at)} · answer to decision “{body.get("title") or row["external_id"]}” · “{_excerpt(found)}”'
            hits.append(_Hit(found, created_at, "decision", line, row["project_id"], bool(row["local_only"])))
    return hits


def _rule_candidates(db, terms, user_id, caller_thread_id) -> list[_Hit]:
    rows = entry_rank_rows_matching(db, _fts_terms(terms), user_id, None, SOURCE_CANDIDATES, SOURCE_CANDIDATES)
    owned = _owned_threads_by_id(db, user_id, {str(row["thread_id"]) for row in rows})
    # Every eligible row is decoded: whether the person is the source is only in
    # the snapshot body, so cutting to the best few first could hide their rule.
    # Search keeps an entry's best-matching revision; only the current one is quoted.
    rows = [
        row for row in rows
        if row["thread_id"] in owned and row["field"] in RULE_FIELDS and row["canonical_state"] == "active"
        and row["revision"] in (NO_REVISION, row["canonical_revision"])
        and not (row["thread_id"] == caller_thread_id and not row["historical"])
    ]
    hits = []
    for row, entry in decoded_entries(hydrate_entry_rows(db, rows)):
        if not any(ref.get("kind") == "user" for ref in entry.body.get("source_refs", []) if isinstance(ref, dict)):
            continue
        found, created_at = match(terms, entry.primary_text or ""), str(row["latest_generated_at"] or row["first_generated_at"] or "")
        if not found or not created_at:
            continue
        line = f'{local_clock(created_at)} · rule an agent recorded from their words (thread {str(row["thread_id"])[:8]}) · “{_excerpt(found)}” → get_checkpoints(checkpoint_id="{row["latest_id"]}")'
        source = owned[str(row["thread_id"])]
        hits.append(_Hit(found, created_at, "rule", line, source["project_id"], bool(source["local_only"])))
    return hits


def _owned_threads_by_id(db, user_id: str, thread_ids: set[str]) -> dict[str, sqlite3.Row]:
    if not thread_ids:
        return {}
    marks = ",".join("?" for _ in thread_ids)
    rows = db.execute(
        f"SELECT t.id,t.project_id,t.local_only FROM threads t WHERE t.id IN ({marks}) AND {_owned_threads('t')}",
        [*thread_ids, user_id],
    ).fetchall()
    return {str(row["id"]): row for row in rows}


def _deliverable(hit: _Hit, caller: sqlite3.Row, cloud_off: set[str]) -> bool:
    """Words from a Local only chat or a cloud-off project reach only a caller under the same restriction."""
    if hit.local_only and not caller["local_only"]:
        return False
    return hit.project_id not in cloud_off or hit.project_id == caller["project_id"]


def _before(created_at: str, cutoff: str | None) -> bool:
    return not cutoff or created_at <= cutoff


def _excerpt(found: Match) -> str:
    text = " ".join(found.span.split())
    if len(text) <= 280:
        return text
    first = min((text.lower().find(term) for term in found.matched_terms if text.lower().find(term) >= 0), default=0)
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


def _kind_counts(hits: list[_Hit]) -> str:
    return ",".join(f"{kind}:{sum(hit.kind == kind for hit in hits)}" for kind in ("message", "comment", "decision", "rule"))


def _log(reason: str, trigger: str, hits: list[_Hit], started: float, thread_id: str) -> None:
    ms = (perf_counter() - started) * 1000
    log.info("prior_words reason={} trigger={} hits={} kinds={} ms={:.1f}", reason, trigger, len(hits), _kind_counts(hits), ms)
    _emit(reason, trigger, hits, ms, thread_id)


def _log_error(trigger: str, exc: Exception, started: float, thread_id: str) -> None:
    ms = (perf_counter() - started) * 1000
    log.opt(exception=exc).warning("prior_words reason=error trigger={} hits=0 kinds=message:0,comment:0,decision:0,rule:0 ms={:.1f} error={}: {}", trigger, ms, type(exc).__name__, exc)
    _emit("error", trigger, [], ms, thread_id)


def _emit(reason: str, trigger: str, hits: list[_Hit], ms: float, thread_id: str) -> None:
    """Record each lookup's outcome as counts only; no words leave the lookup."""
    emit_product_event("prior_words", reason, thread_id=thread_id, properties={
        "trigger": trigger, "hits": len(hits), "kinds": _kind_counts(hits), "ms": round(ms, 1),
    })
