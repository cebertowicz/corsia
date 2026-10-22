"""One compact, bounded status read: what is current, what settled, how to reach the rest.

Every tool should return enough to decide the next read or action, not the
whole plan. This is that read for Plans: an overview (plan identity, revision,
state, progress, the current task or that there is none, the next step or the
wait), then two independently capped sections -- **Current**, the open leaf
work attention-first; **Recent**, the newest settled outcomes -- with truthful
omission counts and cursors bound to one snapshot. Every listed row keeps its
exact identity and a direct detail read; a cut row says so. Whole-plan output
is an explicit opt-in elsewhere (``plan_read(mode='full')``).

The facts come from the one canonical projection the operator UI reads
(``rollup`` + ``operator_node_state`` + ``operator_nodes``); this module only
chooses, orders, bounds and pages them. Times are three different things and
are named as such: how long ago a task was created, how long it has been in
its current state, and how long executors actually ran it -- a node's own
share of ``node_cost``, a sum of durable Turn intervals in which a malformed
or dead-unfinished Turn counts as unknown, never as zero.
``commitment_cost.elapsed_seconds`` keeps its meaning.

A continuation recomputes its section from canonical state and refuses unless
the section's membership, order and row identities are exactly what the first
page was cut from; it also keeps the first page's clock, so ages on page two
are not measured against a later moment. Nothing is persisted: a cursor can be
stale, never wrong.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from plans import node_history, operator_node_state, service, status_facts, wait_causes
from plans.close_next_bounds import bounded, json_bytes
from plans.errors import PlanError
from plans.status_facts import facts as _facts, full_facts as _full_facts
from plans.status_cursor import STALE, decode as _decode, encode as _encode, token as _token
from plans.status_time import ages as _ages, clock_at as _clock_at, parse as _parse

#: Engineering budgets: at most 4 Current and 2 Recent rows per page, each
#: section under its own serialized-byte cap, pagination included.
CURRENT_ROWS = 4
RECENT_ROWS = 2
CURRENT_BYTES = 3584
RECENT_BYTES = 1536
TITLE_MAX_BYTES = 96
REASON_MAX_BYTES = 72
PATH_MAX_BYTES = 72
RECENT_DAYS = status_facts.RECENT_DAYS
SECTIONS = ("current", "recent")

_TERMINAL = service._TERMINAL_STATES
#: Attention-first: what a person must act on, then what is blocked, then what
#: moves, then what waits, then what could start.
_RANK = {"blocked": 1, "running": 2, "review": 2, "waiting": 3, "paused": 3, "ready": 5}
#: Rows that carry the node's own waiting seconds beside their ages (W6): the
#: work running now and the work waiting, review included. A blocked row
#: carries it only when it waited (an unmet dependency on a failed prerequisite).
_WAITED_CONDITIONS = frozenset({"running", "review", "waiting"})


def _rank(node: dict, next_ids: set[str]) -> int:
    if node.get("requires_user_action"):
        return 0
    if node["id"] in next_ids:
        return 4
    return _RANK.get(_condition(node), 6)


def read(
    db, plan_row, *, actor, section: str | None = None, cursor: str | None = None,
    clock: str | None = None,
) -> dict[str, Any]:
    """Return the overview with both sections' first pages, or one continued section."""
    if section is not None and section not in SECTIONS:
        raise PlanError(f"unknown status section {section!r}; use one of {SECTIONS}")
    position = _decode(cursor, plan_id=str(plan_row["id"]), section=section)
    clock = position["a"] if position is not None else (clock or service._now())
    facts = _facts(db, plan_row, actor, clock)
    if section is not None:
        return _page(facts, section, position)
    return {
        **_overview(db, plan_row, facts),
        "current": _page(facts, "current", None),
        "recent": _page(facts, "recent", None),
    }


def node_detail(db, plan_row, node_id: str, *, actor, clock: str | None = None) -> dict:
    """Return one node exactly: every field, its execution, evidence and history.

    Every durable column of the node comes first, then the operator
    presentation over it, so a consumer of the detail read sees the plan's
    own facts (order, assignee, spawn kind, owner and executor threads,
    follow-up lineage, checkpoint and watchdog settings) and never a subset.
    Detail is a deliberate additional read, so nothing here is bounded. A node
    outside this plan is refused by name, so the caller need not reread the
    plan to learn why. An archived node in this plan is served from its durable
    row by ``node_history.retired_detail``: the live projection never holds it.
    """
    clock = clock or service._now()
    durable = db.execute(
        "SELECT * FROM plan_nodes WHERE id=? AND plan_id=?",
        (node_id, str(plan_row["id"])),
    ).fetchone()
    if durable is not None and durable["archived_at"] is not None:
        return node_history.retired_detail(db, durable, clock=clock)
    facts = _facts(db, plan_row, actor, clock, detail_id=node_id)
    node = next((n for n in facts["nodes"] if n["id"] == node_id), None)
    if durable is None or node is None:
        raise PlanError(f"node {node_id!r} is not in this plan; read plan_read() for its rows")
    from plans import intent_words

    return {
        **dict(durable),
        **intent_words.detail(db, durable),
        **node,
        "ages": _ages(node, clock),
        "execution_time": node["node_cost"]["own"]["execution"],
        "events": node_history.events(db, node_id),
        "as_of": clock,
    }


def _condition(node: dict) -> str:
    """The state word a row shows: a dependency on open work waits, it is not blocked."""
    return operator_node_state.shown_condition(node)


def _reason(node: dict) -> str | None:
    if node["status_reason"]:
        return node["status_reason"]
    # A check-in watch ahead of a working child is not what the row is doing.
    child = wait_causes.working_child(node["execution"])
    if child is not None:
        return child["reason"]
    causes = node["execution"].get("causes") or []
    if causes:
        return causes[0].get("reason")
    review = node.get("review")
    if review:
        return review.get("label")
    return None


def _delegated_worker(node: dict) -> dict[str, str] | None:
    """Name a working child or one whose plan now needs its own review."""
    child = node.get("child")
    causes = node["execution"].get("causes") or []
    review_wait = any(
        cause.get("reason_key") == wait_causes.DELEGATED_CHILD_REVIEW
        for cause in causes
    )
    if child is None or not (wait_causes.child_is_working(node["execution"]) or review_wait):
        return None
    thread_id = child["thread_id"]
    return {
        "thread_id": thread_id,
        "plan_read": f"plan_read(thread_id='{thread_id}')",
    }


def _row(node: dict, facts: dict) -> dict[str, Any]:
    title = bounded(str(node["title"]), TITLE_MAX_BYTES)
    reason = _reason(node)
    reason_text = bounded(str(reason), REASON_MAX_BYTES) if reason else None
    path = facts["path"](node["id"])
    row = {
        "id": node["id"],
        "title": title,
        "state": node["state"],
        "revision": node["revision"],
        "condition": _condition(node),
        "label": node["display"]["label_key"],
        "reason": reason_text,
        "path": bounded(path, PATH_MAX_BYTES) if path else None,
        "ages": _ages(node, facts["clock"]),
        "execution_time": node["node_cost"]["own"]["execution"],
        "detail_read": f"plan_read(node_id='{node['id']}')",
    }
    if node.get("size") is not None:
        row["size"] = node["size"]
    worker = _delegated_worker(node)
    if worker is not None:
        row["worker"] = worker
    waited = node["node_cost"]["own"]["waiting"]["seconds"]
    shown = row["condition"] in _WAITED_CONDITIONS or (row["condition"] == "blocked" and waited > 0)
    if shown and node["state"] not in _TERMINAL:
        row["ages"]["waited_seconds"] = waited
    if node["state"] in _TERMINAL:
        row["settled_at"] = node["last_transition_at"]
    if title != node["title"] or reason_text != reason or (path and row["path"] != path):
        row["truncated"] = True
    return row


def _minimal(row: dict) -> dict:
    """The smallest truthful row: identity, state and the read that gets the rest."""
    return {
        key: row[key] for key in ("id", "title", "state", "revision", "detail_read")
    } | {"truncated": True}


def _members(facts: dict, section: str) -> list[dict]:
    if section == "current":
        open_leaves = [n for n in facts["leaves"] if n["state"] not in _TERMINAL]
        next_ids = set(facts["next_ids"])
        return sorted(open_leaves, key=lambda n: (_rank(n, next_ids), n["id"]))
    cutoff = (_clock_at(facts) - timedelta(days=RECENT_DAYS)).isoformat()
    settled = [
        n for n in facts["leaves"]
        if n["state"] in _TERMINAL and str(n["last_transition_at"]) >= cutoff
    ]
    return sorted(settled, key=lambda n: (str(n["last_transition_at"]), n["id"]), reverse=True)


def cut_rows(
    rows: list[dict], max_rows: int, max_bytes: int, minimal=_minimal,
) -> list[dict]:
    """Keep rows while both caps hold; the first row is always kept, minimal if it must be.

    ``minimal`` shapes that first row when it alone exceeds the byte cap, so
    other row kinds (the checkpoint artifact snapshot) share this one rule.
    """
    kept: list[dict] = []
    spent = 0
    for row in rows:
        if len(kept) >= max_rows:
            break
        size = json_bytes(row)
        if spent + size > max_bytes:
            if kept:
                break
            row = minimal(row)
            size = json_bytes(row)
        kept.append(row)
        spent += size
    return kept


def _page(facts: dict, section: str, position: dict | None) -> dict[str, Any]:
    members = _members(facts, section)
    token = _token(facts["plan"]["id"], members)
    offset = 0
    if position is not None:
        if position["t"] != token or position["o"] >= len(members):
            raise PlanError(STALE)
        offset = position["o"]
    max_rows, max_bytes = (
        (CURRENT_ROWS, CURRENT_BYTES) if section == "current" else (RECENT_ROWS, RECENT_BYTES)
    )
    rows = cut_rows([_row(node, facts) for node in members[offset:]], max_rows, max_bytes)
    omitted = len(members) - offset - len(rows)
    page: dict[str, Any] = {
        "section": section, "rows": rows, "omitted_count": omitted,
        "total_count": len(members), "as_of": facts["clock"],
    }
    if omitted > 0:
        cursor = _encode({
            "v": 1, "p": facts["plan"]["id"], "s": section, "t": token,
            "o": offset + len(rows), "a": facts["clock"],
        })
        page["cursor"] = cursor
        page["continue_read"] = f"plan_read(section='{section}', cursor='{cursor}')"
    return page


# --- overview ---------------------------------------------------------------


def _overview(db, plan_row, facts: dict) -> dict[str, Any]:
    from plans import selection_validity

    plan = facts["plan"]
    by_id = {node["id"]: node for node in facts["nodes"]}
    current = None
    if plan_row["owner_kind"] == "thread":
        attempt = selection_validity.attributed_selection(db, str(plan_row["owner_id"]))
        node = by_id.get(str(attempt["node_id"])) if attempt is not None else None
        if node is not None:
            current = _brief(node, facts)
    if current is None:
        for node_id in facts["current_ids"]:
            node = by_id.get(node_id)
            if node is not None and wait_causes.child_is_working(node["execution"]):
                current = _brief(node, facts)
                break
    next_step = None
    for node_id in facts["next_ids"]:
        if node_id in by_id and (current is None or node_id != current["id"]):
            next_step = _brief(by_id[node_id], facts)
            break
    open_rows = [_condition(n) for n in facts["leaves"] if n["state"] not in _TERMINAL]
    progress = facts["progress"]
    overview = {
        "plan": {"id": plan["id"], "revision": plan["revision"], "state": plan["state"]},
        "progress": {
            "done": progress.get("done"), "total": progress.get("total"),
            "liveness": progress.get("liveness"),
        },
        "severity": _severity(open_rows, facts),
        "current_task": current,
        "no_current_task": current is None,
        "next_step": next_step,
        "as_of": facts["clock"],
        "full_read": "plan_read(mode='full')",
    }
    if wait := facts["display"].get("wait"):
        overview["wait"] = wait
    return overview


def _brief(node: dict, facts: dict) -> dict[str, Any]:
    reason = _reason(node)
    brief = {
        "id": node["id"], "title": bounded(str(node["title"]), TITLE_MAX_BYTES),
        "state": node["state"], "revision": node["revision"],
        "condition": _condition(node),
        "reason": bounded(str(reason), REASON_MAX_BYTES) if reason else None,
        "detail_read": f"plan_read(node_id='{node['id']}')",
    }
    worker = _delegated_worker(node)
    if worker is not None:
        brief["worker"] = worker
    return brief


def _severity(conditions: list[str], facts: dict) -> str:
    """What the plan needs, judged from its rows: a wait is neutral, a block is not.

    What the user owes outranks a node pause, but a paused plan defers its own
    asks, so its rows carry none (``plan_defers_asks``). The internal
    ``plans.not_moving`` predicate stays agent-facing and does not add UI state.
    """
    if facts["stuck"] is not None:
        # EW1/EW5: only an outside wait, with nothing owed by the user, is waiting.
        waiting = facts["display"].get("wait") and not facts["stuck"].get("need_you")
        return "waiting" if waiting else "not_moving"
    if any(n.get("requires_user_action") for n in facts["leaves"]):
        return "needs_input"
    if facts["plan"]["state"] == "paused":
        return "paused"
    if facts["next_ids"]:
        return "ready"
    if "blocked" in conditions:
        return "blocked"
    if "running" in conditions or "review" in conditions:
        return "moving"
    if "waiting" in conditions:
        return "waiting"
    return "ready" if conditions else "idle"
