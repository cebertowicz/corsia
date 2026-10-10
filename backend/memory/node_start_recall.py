"""Related past work for a plan node at the moment it starts.

Agents treat a known subject as new because they never think to search. A
node start, activation or add, and a raised decision, are moments every agent
passes through, so the plan tool's own response carries up to three one-line
pointers to past work: retained checkpoint continuity entries from every
epoch the caller's owner holds, and settled plan nodes with their evidence.
Nothing is shown when nothing clears the floor, because noise teaches agents
to skip the block.

The lookup is synchronous FTS and a bounded scan: no event, no background job.
It never fails the caller: any error is logged and the block is left out.

Every outcome is logged at the decision point (``node_start_recall reason=``):
``no_terms``, ``below_floor``, ``delivered`` or ``error``, and ``opened`` when
a later successful ``get_checkpoints``, ``plan_read`` or ``get_thread_history``
reads a delivered pointer.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
import sqlite3

from loguru import logger as log

from memory.checkpoint_entry_rows import entry_hits_matching
from memory.checkpoint_search import caller_user_id
from memory.checkpoint_search_entries import snippet
from memory.node_start_opened import remember

MAX_HITS = 3
MAX_TERMS = 8
LINE_TEXT_CHARS = 140
ENTRY_CANDIDATES = 100
NODE_CANDIDATES = 20

SETTLED_NODE_STATES = ("accepted", "accepted_with_followup", "failed", "closed")

_TOKEN = re.compile(r"[a-z0-9][a-z0-9_\-]{2,}")
_STOPWORDS = frozenset("""
    about above after again against all also and any are because been before
    being below between both but can could did does doing done down during each
    either else every few for from further had has have having here how into
    its itself just like made make many may more most must need needs new not
    now off once only other our out over own per same see should since some such
    than that the their them then there these they this those through too
    under until upon use used using very via was were what when where which
    while who whom why will with within without would yes you your
    add adds added change changes fix fixes fixed update updates updated
    task tasks node nodes plan step steps work item items pass passes passing
    passed test tests tested green done result results evidence criteria
    acceptance record recorded show shows shown one two three
""".split())


@dataclass(frozen=True)
class NodeText:
    """The words a lookup is built from: the node's own title comes first."""

    title: str
    criteria: str = ""
    description: str = ""
    node_id: str | None = None


def terms(text: NodeText) -> tuple[list[str], set[str]]:
    """Return up to ``MAX_TERMS`` distinctive terms and which came from the title."""
    title_terms = _distinct(_TOKEN.findall((text.title or "").lower()))
    rest = _distinct(_TOKEN.findall(
        f"{text.criteria or ''} {text.description or ''}".lower()
    ))
    chosen = (title_terms + [term for term in rest if term not in title_terms])
    chosen = chosen[:MAX_TERMS]
    return chosen, {term for term in title_terms if term in chosen}


def _distinct(tokens: list[str]) -> list[str]:
    """Drop stopwords and bare numbers shorter than an issue number, keep order."""
    seen: dict[str, None] = {}
    for token in tokens:
        token = token.strip("-_")
        if len(token) < 3 or token in _STOPWORDS:
            continue
        if token.isdigit() and len(token) < 4:
            continue
        seen.setdefault(token, None)
    return list(seen)


def floor(term_count: int) -> int:
    """Distinct terms a hit must share: two, or a third of the terms if more."""
    return max(2, math.ceil(term_count / 3))


def related(
    db: sqlite3.Connection,
    text: NodeText,
    *,
    caller_thread_id: str,
    trigger: str,
) -> list[str]:
    """Return at most three one-line pointers to related past work, or none.

    Owner-scoped exactly as checkpoint search is: only threads of the caller's
    user. Entries the caller already carries live (its own current-epoch,
    current entries) are skipped, since they are in its context already.
    """
    try:
        found = _related(db, text, caller_thread_id)
    except Exception as exc:  # noqa: BLE001 - advisory block never fails its caller
        _log_error(trigger, exc)
        return []
    reason, hits = found
    _log(reason, trigger, len(hits))
    for hit in hits:
        remember(caller_thread_id, hit.pointer)
    return [hit.line for hit in hits]


#: The key the related lines ride under, in a tool result and in a wake.
RESULT_KEY = "related_past_work"

_NODE_OPERATIONS = frozenset({"start", "activate", "add", "insert"})
#: ``next.selected.how`` values that mean the close just gave the slot a task.
_STARTED_HOWS = frozenset({"started", "auto_selected"})


def attach_to_result(
    db: sqlite3.Connection, operation: str, result: object, *, caller_thread_id: str,
) -> None:
    """Add ``related_past_work`` to a plan tool result that starts or opens work.

    Covers start, activate, add and insert (the node in the result), a close
    whose ``next`` block started or selected a task, and a raised decision.
    Anything else, and an empty lookup, leaves the result untouched. A
    mutation outcome wrapper is unwrapped to the result dict it carries.
    """
    result = getattr(result, "result", result)
    if not isinstance(result, dict):
        return
    try:
        subject = _subject(db, operation, result)
    except Exception as exc:  # noqa: BLE001 - advisory block never fails its caller
        _log_error(operation, exc)
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
    """Return the related lines for the task a ``next`` block reports started."""
    node_id = _started_node_id(block)
    if node_id is None:
        return []
    try:
        text = _node_text(db, node_id)
    except Exception as exc:  # noqa: BLE001 - advisory block never fails its caller
        _log_error("wake", exc)
        return []
    if text is None:
        return []
    return related(db, text, caller_thread_id=caller_thread_id, trigger="wake")


def _subject(db, operation: str, result: dict) -> tuple[NodeText, str] | None:
    if operation in _NODE_OPERATIONS and isinstance(result.get("node"), dict):
        node = result["node"]
        return NodeText(
            str(node.get("title") or ""), str(node.get("acceptance_criteria") or ""),
            str(node.get("description") or ""), node.get("id"),
        ), operation
    if operation == "raise" and isinstance(result.get("decision"), dict):
        decision = result["decision"]
        return NodeText(
            str(decision.get("title") or ""), str(decision.get("context") or ""),
        ), "decision_raise"
    node_id = _started_node_id(result.get("next"))
    if node_id is None:
        return None
    text = _node_text(db, node_id)
    return None if text is None else (text, "close_next")


def _started_node_id(block: object) -> str | None:
    selected = block.get("selected") if isinstance(block, dict) else None
    if not isinstance(selected, dict) or selected.get("how") not in _STARTED_HOWS:
        return None
    return str(selected["id"])


def _node_text(db, node_id: str) -> NodeText | None:
    row = db.execute(
        "SELECT id, title, acceptance_criteria, description FROM plan_nodes WHERE id=?",
        (node_id,),
    ).fetchone()
    if row is None:
        return None
    return NodeText(
        str(row["title"]), str(row["acceptance_criteria"]),
        str(row["description"] or ""), str(row["id"]),
    )


@dataclass(frozen=True)
class _Hit:
    score: tuple[int, int]
    rank: float
    line: str
    pointer: str


def _related(
    db: sqlite3.Connection, text: NodeText, caller_thread_id: str,
) -> tuple[str, list[_Hit]]:
    chosen, title_terms = terms(text)
    if len(chosen) < 2 or not title_terms:
        return "no_terms", []
    user_id = caller_user_id(db, caller_thread_id)
    if user_id is None:
        return "no_terms", []
    need = floor(len(chosen))
    candidates = [
        *_entry_candidates(db, chosen, title_terms, need, user_id, caller_thread_id),
        *_node_candidates(
            db, chosen, title_terms, need, user_id, text.node_id, caller_thread_id,
        ),
    ]
    kept = [hit for hit in candidates if _clears(hit.score, need)]
    if not kept:
        return "below_floor", []
    # Shared terms decide; a tie goes to an entry (bm25 is negative) before a
    # node (scan position), then to the better bm25 or the newer node.
    kept.sort(key=lambda hit: (-hit.score[0], -hit.score[1], hit.rank))
    return "delivered", kept[:MAX_HITS]


def _clears(score: tuple[int, int], need: int) -> bool:
    """The floor: enough shared terms, at least one of them from the title."""
    return score[0] >= need and score[1] > 0


def _coverage(body: str, chosen: list[str], title_terms: set[str]) -> tuple[int, int]:
    """Return (distinct terms matched, title terms matched), prefix-matched."""
    tokens = set(_TOKEN.findall(body.lower()))
    matched = {
        term for term in chosen
        if any(token.startswith(term) for token in tokens)
    }
    return len(matched), len(matched & title_terms)


def _entry_candidates(
    db, chosen: list[str], title_terms: set[str], need: int, user_id: str,
    caller_thread_id: str,
) -> list[_Hit]:
    scores: dict[tuple, tuple[int, int]] = {}

    def select(rows: list[dict]) -> list[dict]:
        # Score on the indexed text and keep only the best few, so at most
        # MAX_HITS frozen snapshots are decoded. An entry the caller still
        # holds live is already in its context, whatever revision matched.
        scored = [
            (_coverage(str(row["search_text"]), chosen, title_terms), row)
            for row in rows
            if not (
                row["thread_id"] == caller_thread_id
                and not row["historical"]
                and row["canonical_state"] == "active"
            )
        ]
        kept = [(score, row) for score, row in scored if _clears(score, need)]
        kept.sort(key=lambda pair: (-pair[0][0], -pair[0][1], pair[1]["relevance"]))
        for score, row in kept[:MAX_HITS]:
            scores[(row["thread_id"], row["history_epoch"], row["entry_id"])] = score
        return [row for _score, row in kept[:MAX_HITS]]

    match = " OR ".join(f'"{term}"*' for term in chosen)
    rows = entry_hits_matching(
        db, match, user_id=user_id, candidate_limit=ENTRY_CANDIDATES, per_thread=3,
        select=select,
    )
    hits = []
    for relevance, _order, hit in rows:
        latest = hit.get("latest_checkpoint") or hit.get("first_checkpoint")
        if latest is None:
            continue
        state = hit["entry_state"]
        if hit["historical"]:
            state = f"{state}, epoch {hit['history_epoch']}"
        line = (
            f"{hit['field']} ({state}) · thread {str(hit['thread_id'])[:8]} · "
            f"{str(latest['generated_at'])[:10]} · "
            f"{snippet(hit['statement'], LINE_TEXT_CHARS)} → "
            f"get_checkpoints(checkpoint_id=\"{latest['checkpoint_id']}\") "
            f"entry {hit['entry_id']}"
        )
        score = scores[(hit["thread_id"], hit["history_epoch"], hit["entry_id"])]
        hits.append(_Hit(score, relevance, line, str(latest["checkpoint_id"])))
    return hits


def _node_candidates(
    db, chosen: list[str], title_terms: set[str], need: int, user_id: str,
    own_node: str | None, caller_thread_id: str,
) -> list[_Hit]:
    from plans import public

    # Titles carry a node's subject, so the scan matches titles only (a short
    # column: this is the cost that grows with settled work) and orders by how
    # many terms a title shares, not by recency: a common term would otherwise
    # fill the window with new nodes that share nothing else. Coverage is then
    # scored on title, criteria and description together. Archived nodes stay:
    # close_node always archives, and archival retires a node from the live
    # plan, not from history. A purged thread's work does not.
    matched = " + ".join("(n.title LIKE ?)" for _ in chosen)
    marks = ",".join("?" for _ in SETTLED_NODE_STATES)
    params: list[object] = [f"%{term}%" for term in chosen]
    params.extend((user_id, *SETTLED_NODE_STATES, own_node or "", NODE_CANDIDATES))
    rows = db.execute(
        "SELECT * FROM ("
        "  SELECT n.id, n.title, n.acceptance_criteria, n.description, n.state, "
        "         n.updated_at, n.requirements_revision, p.owner_id, "
        f"        ({matched}) AS matched "
        "  FROM plan_nodes n JOIN plans p ON p.id = n.plan_id "
        "  JOIN threads t ON t.id = p.owner_id "
        "  WHERE p.owner_kind = 'thread' AND t.user_id = ? AND t.purged_at IS NULL "
        f"    AND n.state IN ({marks}) AND n.id != ?"
        ") WHERE matched > 0 ORDER BY matched DESC, updated_at DESC LIMIT ?",
        params,
    ).fetchall()
    scored = []
    for position, row in enumerate(rows):
        body = f"{row['title']} {row['acceptance_criteria']} {row['description'] or ''}"
        score = _coverage(body, chosen, title_terms)
        if _clears(score, need):
            scored.append((score, position, row))
    scored.sort(key=lambda item: (-item[0][0], -item[0][1], item[1]))
    hits = []
    for score, position, row in scored[:MAX_HITS]:
        evidence = public.latest_review_evidence(
            str(row["id"]), requirements_revision=row["requirements_revision"], db=db,
        )
        said = "" if evidence is None else f" — {evidence['evidence']}"
        # plan_read opens a node only in the caller's own plan, and nothing
        # reads another thread's node: the pointer names what the read does
        # return (that thread's messages) and says the node is not it.
        owner = str(row["owner_id"])
        if owner == caller_thread_id:
            pointer, action = str(row["id"]), f'plan_read(node_id="{row["id"]}")'
        else:
            pointer = owner
            action = (
                f'get_thread_history(thread_id="{owner}") — '
                "that thread's messages, not this node"
            )
        line = (
            f"node {row['state']} · thread {owner[:8]} · "
            f"{str(row['updated_at'])[:10]} · "
            f"{snippet(str(row['title']) + said, LINE_TEXT_CHARS)} → {action}"
        )
        # Scan position (most shared title terms, then newest) breaks ties.
        hits.append(_Hit(score, float(position), line, pointer))
    return hits



def _log(reason: str, trigger: str, hits: int) -> None:
    log.info(
        "node_start_recall reason={} trigger={} hits={}", reason, trigger, hits,
    )


def _log_error(trigger: str, exc: Exception) -> None:
    """Log a skipped lookup loudly enough that a lasting failure is diagnosable."""
    log.opt(exception=exc).warning(
        "node_start_recall reason=error trigger={} hits=0 error={}: {}",
        trigger, type(exc).__name__, exc,
    )
