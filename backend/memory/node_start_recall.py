"""Earlier words from the person that match a task when it starts.

The lookup is advisory, bounded, and synchronous.  It never changes a plan
operation's outcome: failures are logged and simply omit the optional block.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
import json
import re
import sqlite3
from time import perf_counter

from loguru import logger as log

from core.correction_notes import local_clock
from core.corrections_recording import (
    CORRECTION_EVENT,
    CorrectionCandidate,
    is_owner_authored_message,
    owner_message_sql_predicate,
)
from infra.checkpoint_queries import current_installed_through_seq
from infra.product_events import emit_product_event
from llm.locality import cloud_off_project_ids
from memory.checkpoint_search import caller_user_id
from memory.node_text import node_text as _node_text
from memory.task_match import Match, NodeText, TaskTerms, match, task_terms

MAX_HITS = 3
SOURCE_CANDIDATES = 100
RULE_FIELDS = frozenset({"active_constraints", "approvals_and_preferences"})
RESULT_KEY = "prior_words"
_NODE_OPERATIONS = frozenset({"start", "activate", "add", "insert"})
_STARTED_HOWS = frozenset({"started", "auto_selected"})
_HEADER = "Earlier words from the person that match this task, newest first; where they differ, the newer one governs."
_MATCH_FUNCTION = "prior_words_match"
_OWNER_FUNCTION = "prior_words_owner"


@dataclass(frozen=True)
class _Hit:
    matched: Match
    created_at: str
    kind: str
    line: str


@dataclass(frozen=True)
class EarlierCorrection:
    """A prior correction message selected by the canonical message reader."""

    thread_title: str
    created_at: str
    content: str


@dataclass(frozen=True)
class _MessageHit:
    message_id: str
    thread_id: str
    thread_title: str
    created_at: str
    content: str
    matched: Match
    kind: str = "message"


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


def earlier_correction_messages(
    db: sqlite3.Connection, candidate: CorrectionCandidate,
) -> tuple[EarlierCorrection, ...]:
    """Return up to three related, earlier correction messages for one chat correction.

    This is the correction-note mode of the canonical message reader. It keeps
    its user, purge, locality, owner and delivery filters while the candidate's
    timestamp supplies the visibility boundary instead of a plan-start cutoff.
    """
    started = perf_counter()
    trigger = "correction_note"
    if candidate.source_kind != "message":
        _log("not_message", trigger, [], started, candidate.thread_id)
        return ()
    try:
        reason, hits = _earlier_correction_messages(db, candidate)
    except Exception as exc:  # noqa: BLE001 - advisory context must not fail a fact write
        _log_error(trigger, exc, started, candidate.thread_id)
        return ()
    _log(reason, trigger, hits, started, candidate.thread_id)
    return tuple(EarlierCorrection(hit.thread_title, hit.created_at, hit.content) for hit in hits)


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
    _ensure_functions(db)

    from memory.node_start_other_sources import candidates as other_candidates

    candidates = [
        *_message_candidates(db, terms, user_id, caller_thread_id, caller, cloud_off, cutoff),
        *other_candidates(db, terms, user_id, caller_thread_id, caller, cloud_off, cutoff, SOURCE_CANDIDATES),
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


def _earlier_correction_messages(
    db: sqlite3.Connection, candidate: CorrectionCandidate,
) -> tuple[str, list[_MessageHit]]:
    terms = task_terms(NodeText(candidate.content))
    if len(terms.terms) < 2 or not terms.title_terms:
        return "no_terms", []
    user_id = caller_user_id(db, candidate.thread_id)
    caller = db.execute("SELECT project_id,local_only FROM threads WHERE id=?", (candidate.thread_id,)).fetchone()
    if user_id is None or caller is None:
        return "unresolved_caller", []
    _ensure_functions(db)
    hits = _message_matches(
        db, terms, user_id, candidate.thread_id, caller, cloud_off_project_ids(db=db), cutoff=None,
        before=(candidate.created_at, candidate.source_id), correction_only=True, exclude_live_caller=False,
    )
    if not hits:
        return "below_floor", []
    hits.sort(key=lambda hit: (-hit.matched.shared, -hit.matched.title_shared, _newest_first(hit.created_at)))
    kept = hits[:MAX_HITS]
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


def _message_candidates(db, terms, user_id, caller_thread_id, caller, cloud_off, cutoff) -> list[_Hit]:
    from memory.node_start_other_sources import _excerpt

    rows = _message_matches(
        db, terms, user_id, caller_thread_id, caller, cloud_off, cutoff=cutoff,
    )
    return [
        _Hit(
            row.matched, row.created_at, "message",
            f'{local_clock(row.created_at)} · message in “{row.thread_title}” · “{_excerpt(row.matched)}” '
            f'→ get_thread_history(thread_id="{row.thread_id}")',
        )
        for row in rows
    ]


def _message_matches(
    db, terms, user_id, caller_thread_id, caller, cloud_off, *, cutoff: str | None,
    before: tuple[str, str] | None = None, correction_only: bool = False, exclude_live_caller: bool = True,
) -> list[_MessageHit]:
    """Read eligible messages through the shared FTS, scope, owner and matcher path."""
    owner_filter, owner_params = _positional(*owner_message_sql_predicate("m"))
    source_scope, source_params = _source_scope("t", "m.created_at", caller, cloud_off, cutoff)
    caller_cut = current_installed_through_seq(caller_thread_id, db=db) or 0 if exclude_live_caller else None
    matching, match_params = _matching("m.content", terms)
    cutoff_filter = "AND (m.thread_id<>? OR m.seq IS NOT NULL AND m.seq<=?)" if caller_cut is not None else ""
    cutoff_params: list[object] = [caller_thread_id, caller_cut] if caller_cut is not None else []
    before_filter = "AND (m.created_at < ? OR (m.created_at=? AND m.id < ?))" if before else ""
    before_params: list[object] = [before[0], before[0], before[1]] if before else []
    correction_filter = (
        "AND EXISTS (SELECT 1 FROM outcome_events e WHERE e.subject_key='message:' || m.id "
        "AND e.event_name=? AND e.retracted_at IS NULL)" if correction_only else ""
    )
    correction_params: list[object] = [CORRECTION_EVENT] if correction_only else []
    rows = db.execute(
        f"""SELECT m.id,m.thread_id,m.content,m.created_at,t.title
            FROM messages_fts JOIN messages m ON m.rowid=messages_fts.rowid
            JOIN threads t ON t.id=m.thread_id
            WHERE messages_fts MATCH ? AND {owner_filter} AND m.delivery_state='delivered'
              AND {_owned_threads('t')} {source_scope}
              {cutoff_filter} {before_filter} {correction_filter}
              AND {_OWNER_FUNCTION}(m.role,m.content,m.metadata) {matching}
            ORDER BY m.created_at DESC LIMIT ?""",
        [_fts_title(terms), *owner_params, user_id, *source_params, *cutoff_params,
         *before_params, *correction_params, *match_params, SOURCE_CANDIDATES],
    ).fetchall()
    hits: list[_MessageHit] = []
    for row in rows:
        found = match(terms, str(row["content"]))
        if found:
            hits.append(_MessageHit(
                str(row["id"]), str(row["thread_id"]), str(row["title"] or "Untitled thread"),
                str(row["created_at"]), str(row["content"]), found,
            ))
    return hits



def _newest_first(created_at: str) -> float:
    try:
        value = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        return -value.replace(tzinfo=value.tzinfo or UTC).timestamp()
    except ValueError:
        return 0


def _kind_counts(hits: Sequence[_Hit | _MessageHit]) -> str:
    return ",".join(f"{kind}:{sum(hit.kind == kind for hit in hits)}" for kind in ("message", "comment", "decision", "rule"))


def _log(reason: str, trigger: str, hits: Sequence[_Hit | _MessageHit], started: float, thread_id: str) -> None:
    ms = (perf_counter() - started) * 1000
    log.info("prior_words reason={} trigger={} hits={} kinds={} ms={:.1f}", reason, trigger, len(hits), _kind_counts(hits), ms)
    _emit(reason, trigger, hits, ms, thread_id)


def _log_error(trigger: str, exc: Exception, started: float, thread_id: str) -> None:
    ms = (perf_counter() - started) * 1000
    log.opt(exception=exc).warning("prior_words reason=error trigger={} hits=0 kinds=message:0,comment:0,decision:0,rule:0 ms={:.1f} error={}: {}", trigger, ms, type(exc).__name__, exc)
    _emit("error", trigger, [], ms, thread_id)


def _emit(reason: str, trigger: str, hits: Sequence[_Hit | _MessageHit], ms: float, thread_id: str) -> None:
    """Record each lookup's outcome as counts only; no words leave the lookup."""
    emit_product_event("prior_words", reason, thread_id=thread_id, properties={
        "trigger": trigger, "hits": len(hits), "kinds": _kind_counts(hits), "ms": round(ms, 1),
    })
