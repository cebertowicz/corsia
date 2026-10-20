"""Earlier words from the person that match a task when it starts.

The lookup is advisory, bounded, and synchronous.  It never changes a plan
operation's outcome: failures are logged and simply omit the optional block.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import re
import sqlite3
from time import perf_counter

from loguru import logger as log

from core.correction_notes import local_clock
from core.corrections_recording import is_owner_authored_message, owner_message_sql_predicate
from infra.product_events import emit_product_event
from llm.locality import cloud_off_project_ids
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
        return "unresolved_caller", []
    cutoff = _start_cutoff(db, text.node_id)
    cloud_off = cloud_off_project_ids(db=db)

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


def _fts_floor(terms: TaskTerms) -> str:
    """An FTS query for messages holding a title term and ``floor`` distinct terms.

    Each prefix term matches every token ``match()`` counts for it, so the floor drops no hit.
    """
    quoted = {term: f'"{term}"*' for term in terms.terms}
    title = " OR ".join(quoted[term] for term in terms.terms if term in terms.title_terms)
    return f"({title}) AND ({_at_least(floor(len(terms.terms)), list(quoted.values()))})"


def _at_least(count: int, terms: list[str]) -> str:
    """An FTS expression true when at least ``count`` of ``terms`` match."""
    if count == 1:
        return " OR ".join(terms)
    return " OR ".join(
        f"({term} AND ({_at_least(count - 1, terms[index + 1:])}))"
        for index, term in enumerate(terms[:len(terms) - count + 1])
    )


def _positional(clause: str, params: dict[str, str]) -> tuple[str, list[str]]:
    """Bind a named-placeholder clause by name so it can sit among ``?`` markers."""
    names = re.findall(r":(\w+)", clause)
    return re.sub(r":\w+", "?", clause), [params[name] for name in names]


def _hay(column: str) -> str:
    """Select ``column`` lowered once, with a leading separator, for ``_floored``."""
    return f"' '||lower({column}) AS hay"


def _floored(scoped: str, order: str, terms: TaskTerms) -> tuple[str, list[object]]:
    """Keep ``scoped`` rows (each with a ``_hay``) at the shared-term floor, best first, up to the limit.

    ``shared`` and ``title_shared`` count terms starting a word; each is never below ``match()``'s
    count, so the floor drops no hit, and exceeds it only when the terms sit in different windows.
    """
    title = [term for term in terms.terms if term in terms.title_terms]

    def count(selected: list[str] | tuple[str, ...]) -> str:
        return "+".join("(hay GLOB ?)" for _ in selected)

    patterns = [f"*[^a-z0-9]{term}*" for term in (*terms.terms, *title)]
    return (
        f"""WITH scoped AS MATERIALIZED ({scoped})
            SELECT * FROM (SELECT *,{count(terms.terms)} AS shared,{count(title)} AS title_shared FROM scoped)
            WHERE shared>=? AND title_shared>=1
            ORDER BY shared DESC,title_shared DESC,{order} LIMIT ?""",
        [*patterns, floor(len(terms.terms)), SOURCE_CANDIDATES],
    )


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


def _message_candidates(db, terms, user_id, caller_thread_id, caller, cloud_off, cutoff) -> list[_Hit]:
    owner_filter, owner_params = _positional(*owner_message_sql_predicate("m"))
    source_scope, source_params = _source_scope("t", "m.created_at", caller, cloud_off, cutoff)
    # The FTS query applies the floor before the limit; bm25 only orders eligible messages.
    rows = db.execute(
        f"""SELECT m.id,m.thread_id,m.content,m.role,m.metadata,m.created_at,t.title
            FROM messages_fts JOIN messages m ON m.rowid=messages_fts.rowid
            JOIN threads t ON t.id=m.thread_id
            WHERE messages_fts MATCH ? AND {owner_filter} AND m.delivery_state='delivered'
              AND {_owned_threads('t')} {source_scope}
              AND (m.thread_id<>? OR m.seq IS NOT NULL AND m.seq<=COALESCE((
                SELECT MAX(c.through_seq) FROM checkpoints c JOIN threads ct ON ct.id=c.thread_id
                WHERE c.thread_id=? AND c.status='installed' AND c.history_epoch=ct.history_epoch
              ), 0))
            ORDER BY bm25(messages_fts),m.created_at DESC LIMIT ?""",
        [f"role:user AND ({_fts_floor(terms)})", *owner_params, user_id, *source_params,
         caller_thread_id, caller_thread_id, SOURCE_CANDIDATES],
    ).fetchall()
    hits = []
    for row in rows:
        if not is_owner_authored_message(role=row["role"], content=row["content"], metadata=row["metadata"]):
            continue
        found = match(terms, str(row["content"]))
        if found:
            line = f'{local_clock(str(row["created_at"]))} · message in “{row["title"]}” · “{_excerpt(found)}” → get_thread_history(thread_id="{row["thread_id"]}")'
            hits.append(_Hit(found, str(row["created_at"]), "message", line))
    return hits


def _comment_candidates(db, terms, user_id, caller, cloud_off, cutoff) -> list[_Hit]:
    from core.correction_targets import comment_target_sql

    source_scope, source_params = _source_scope("t", "c.created_at", caller, cloud_off, cutoff)
    query, floor_params = _floored(
        f"""SELECT c.id,c.artifact_id,c.body,c.created_at,ai.title,ai.path,{_hay('c.body')}
            FROM doc_comments c JOIN threads t ON t.id=({comment_target_sql('c')})
            LEFT JOIN artifact_identities ai ON ai.id=c.artifact_id
            WHERE c.author_kind='user' AND {_owned_threads('t')} {source_scope}""",
        "created_at DESC,id DESC", terms,
    )
    rows = db.execute(query, [user_id, *source_params, *floor_params]).fetchall()
    hits = []
    for row in rows:
        found = match(terms, str(row["body"]))
        if found:
            about = row["title"] or row["path"] or row["artifact_id"]
            line = f'{local_clock(str(row["created_at"]))} · comment on “{about}” · “{_excerpt(found)}”'
            hits.append(_Hit(found, str(row["created_at"]), "comment", line))
    return hits


def _decision_candidates(db, terms, user_id, caller, cloud_off, cutoff) -> list[_Hit]:
    from plans.public import DECISION_TYPE

    answer = "json_extract(ai.metadata,'$.plan_decision.answer.text')"
    answered_at = "json_extract(ai.metadata,'$.plan_decision.answered_at')"
    source_scope, source_params = _source_scope("t", answered_at, caller, cloud_off, cutoff)
    query, floor_params = _floored(
        f"""SELECT ai.external_id,json_extract(ai.metadata,'$.plan_decision.title') AS title,
                   {answer} AS answer,{answered_at} AS answered_at,{_hay(answer)}
            FROM artifact_identities ai
            JOIN threads t ON t.id=json_extract(ai.metadata,'$.plan_decision.thread_id')
            WHERE ai.integration='local' AND ai.type=? AND ai.state='active'
              AND json_extract(ai.metadata,'$.plan_decision.state')='answered'
              AND json_type(ai.metadata,'$.plan_decision.answer.text')='text'
              AND {_owned_threads('t')} {source_scope}""",
        "answered_at DESC", terms,
    )
    rows = db.execute(query, [DECISION_TYPE, user_id, *source_params, *floor_params]).fetchall()
    hits = []
    for row in rows:
        text, created_at = str(row["answer"]), str(row["answered_at"])
        found = match(terms, text)
        if found:
            line = f'{local_clock(created_at)} · answer to decision “{row["title"] or row["external_id"]}” · “{_excerpt(found)}”'
            hits.append(_Hit(found, created_at, "decision", line))
    return hits


def _rule_candidates(db, terms, user_id, caller_thread_id, caller, cloud_off, cutoff) -> list[_Hit]:
    # Both rule fields are statement entries (core.continuity_types), so the statement is the rule's text.
    statement = "json_extract(ce.body,'$.statement')"
    source_scope, source_params = _source_scope("t", "ce.updated_at", caller, cloud_off, cutoff)
    fields = sorted(RULE_FIELDS)
    top, floor_params = _floored(
        f"""SELECT ce.thread_id,ce.history_epoch,ce.entry_id,ce.revision,ce.updated_at,
                   {statement} AS statement,{_hay(statement)}
            FROM continuity_entries ce JOIN threads t ON t.id=ce.thread_id
            WHERE {_owned_threads('t')} AND ce.state='active'
              AND ce.field IN (?,?)
              AND EXISTS (SELECT 1 FROM json_each(ce.body,'$.source_refs')
                          WHERE json_extract(value,'$.kind')='user')
              AND NOT (ce.thread_id=? AND ce.history_epoch=t.history_epoch)
              {source_scope}""",
        "updated_at DESC", terms,
    )
    rows = db.execute(
        f"""WITH top AS ({top})
            SELECT top.*,checkpoints.id AS checkpoint_id
            FROM top LEFT JOIN checkpoint_entry_search entry_index
              ON entry_index.thread_id=top.thread_id AND entry_index.history_epoch=top.history_epoch
             AND entry_index.entry_id=top.entry_id AND entry_index.revision=top.revision
            LEFT JOIN checkpoints ON checkpoints.generation_order=entry_index.last_generation_order
            ORDER BY top.shared DESC,top.title_shared DESC,top.updated_at DESC""",
        [user_id, *fields, caller_thread_id, *source_params, *floor_params],
    ).fetchall()
    hits = []
    for row in rows:
        found = match(terms, str(row["statement"] or ""))
        if found:
            pointer = f' → get_checkpoints(checkpoint_id="{row["checkpoint_id"]}")' if row["checkpoint_id"] else ""
            line = f'{local_clock(str(row["updated_at"]))} · rule an agent recorded from their words (thread {str(row["thread_id"])[:8]}) · “{_excerpt(found)}”{pointer}'
            hits.append(_Hit(found, str(row["updated_at"]), "rule", line))
    return hits


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
