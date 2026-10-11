"""The artifact cards a thread's messages and update cards carry.

Cards are computed when a page is read and added as a sibling ``cards`` key on
the REST display row; nothing is written to a message's stored metadata, so a
card's title and round state are always the registry's and the hold's current
truth.

An ask card is found from the thread's own ``blocked`` update rows, which the
page's sequence range selects through ``idx_thread_updates_thread_seq``, then
from each update's ``hold_placed`` event by primary key. That event names the
Turn and the tool call that raised the ask, so the card attaches to the
assistant step whose tool calls contain it. A hold with no call id (placed
before they were recorded) attaches to the latest step of its Turn that
existed when the hold was placed. A step outside the page gets nothing from this
page: its own page reads its asks.

An artifact asked in a Turn is one card in that Turn, wherever the ask card renders
and whatever round state it reads. This module is the one dedup owner: a reference
card for an asked artifact is kept and marked ``covered_by_ask`` with the holds that
cover it, and the client shows it only when none of those ask cards is on screen
(its host row is collapsed, or on another page). The reviewed document is never
dropped. An ask raised on an earlier page of a Turn is not seen from a later page,
so there the reference is unmarked: it shows, and may repeat the earlier ask card.
"""

from __future__ import annotations

import json

from api.files_roots import scope_bases
from infra.queries import get_thread_for_user
from registry.chat_cards import ask_cards
from registry.path_cards import reference_cards_by_message

_ASK_UPDATES_LIMIT = 200

_ASK_UPDATES_SQL = """
SELECT u.created_at, {hold_id} AS hold_id
  FROM thread_updates u
 WHERE u.thread_id = ? AND u.kind = 'blocked' AND u.after_seq >= ?{upper}
   AND {hold_id} IS NOT NULL
 ORDER BY u.after_seq, u.id LIMIT ?"""


def hydrate_update_asks(db, cards: list[dict]) -> None:
    """Replace each update card's ``{hold_id, artifact_id}`` ask with its live card."""
    asks = {card["ask"]["hold_id"]: card["ask"]["artifact_id"] for card in cards if card.get("ask")}
    live = ask_cards(db, asks)
    for card in cards:
        if card.get("ask"):
            card["ask"] = live.get(card["ask"]["hold_id"])


def _loads(raw: object) -> dict:
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _step_calls(row: dict) -> set[str]:
    calls = _loads(row.get("metadata")).get("tool_calls") or []
    return {c["id"] for c in calls if isinstance(c, dict) and isinstance(c.get("id"), str)}


def ask_updates_sql(upper: str) -> str:
    """The ask-update read, with ``upper`` as its optional upper sequence bound."""
    from plans import public

    return _ASK_UPDATES_SQL.format(upper=upper, hold_id=public.user_ask_hold_sql("u.source_id", "id"))


def _page_updates(db, thread_id: str, rows: list[dict]) -> list:
    seqs = [r["seq"] for r in rows if isinstance(r.get("seq"), int)]
    if not seqs:
        return []
    # Tool results sit between a step and its next message, so an ask raised by
    # the page's last step can follow it by a few sequence numbers: read up to
    # the next message that is not a tool result.
    following = db.execute(
        "SELECT MIN(seq) FROM messages WHERE thread_id=? AND seq>? AND role!='tool'",
        (thread_id, max(seqs)),
    ).fetchone()[0]
    upper = " AND u.after_seq < ?" if following is not None else ""
    args = [thread_id, min(seqs), *([following] if following is not None else []), _ASK_UPDATES_LIMIT]
    return db.execute(ask_updates_sql(upper), args).fetchall()


def _turn_steps(
    db, thread_id: str, requests: list[tuple[str, str]],
) -> dict[tuple[str, str], str | None]:
    """The latest pre-hold assistant step for each requested Turn in one read."""
    unique = list(dict.fromkeys(requests))
    if not unique:
        return {}
    values = ",".join("(?, ?)" for _ in unique)
    rows = db.execute(
        f"""WITH requested(turn_id, placed_at) AS (VALUES {values})
            SELECT requested.turn_id, requested.placed_at,
                   (SELECT id FROM messages
                     WHERE thread_id=? AND role='assistant'
                       AND json_extract(metadata, '$.turn_id')=requested.turn_id
                       AND julianday(created_at)<=julianday(requested.placed_at)
                     ORDER BY seq DESC LIMIT 1) AS message_id
              FROM requested""",
        (*[value for request in unique for value in request], thread_id),
    ).fetchall()
    return {
        (row["turn_id"], row["placed_at"]): row["message_id"]
        for row in rows
    }


def _ask_cards_by_message(
    db, thread_id: str, rows: list[dict],
) -> tuple[dict[str, list[dict]], dict[str, dict[str, list[str]]]]:
    """Ask cards by message id, and the holds that asked each artifact in each Turn, wherever the card renders."""
    assistants = [r for r in rows if r.get("role") == "assistant"]
    from plans import public

    updates = _page_updates(db, thread_id, rows) if assistants else []
    if not updates:
        return {}, {}
    by_call: dict[str, str] = {}
    for row in assistants:
        for call_id in _step_calls(row):
            by_call.setdefault(call_id, row["id"])
    in_page = {r["id"] for r in assistants}
    turn_of = {r["id"]: _loads(r.get("metadata")).get("turn_id") for r in assistants}
    asked: dict[str, dict[str, list[str]]] = {}
    placed: list[tuple[str, str, str]] = []  # (message id, hold id, ask artifact id)
    holds = public.ask_holds(db, [u["hold_id"] for u in updates])
    ask_updates = [
        (update, hold)
        for update in updates
        if (hold := holds.get(update["hold_id"])) is not None
    ]
    fallback_requests = [
        (hold["raised_in"]["turn_id"], update["created_at"])
        for update, hold in ask_updates
        if not any(hold["raised_in"].get(key) for key in ("tool_invocation_id", "provider_call_id"))
        and hold["raised_in"].get("turn_id")
    ]
    fallback_steps = _turn_steps(db, thread_id, fallback_requests)
    for update, hold in ask_updates:
        raised_in = hold["raised_in"]
        ids = [raised_in[key] for key in ("tool_invocation_id", "provider_call_id") if raised_in.get(key)]
        if ids:
            message_id = next((by_call[call_id] for call_id in ids if call_id in by_call), None)
        elif raised_in.get("turn_id"):
            message_id = fallback_steps[(raised_in["turn_id"], update["created_at"])]
        else:
            message_id = None
        turn = raised_in.get("turn_id") or turn_of.get(message_id)
        if turn:
            asked.setdefault(turn, {}).setdefault(hold["artifact_id"], []).append(update["hold_id"])
        if message_id is not None and message_id in in_page:
            placed.append((message_id, update["hold_id"], hold["artifact_id"]))
    live = ask_cards(db, {hold_id: artifact_id for _, hold_id, artifact_id in placed})
    found: dict[str, list[dict]] = {}
    for message_id, hold_id, _ in placed:
        if hold_id in live:
            found.setdefault(message_id, []).append(live[hold_id])
    return found, asked


def attach_cards(db, thread_id: str, user_id: str, thread_row, messages: list[dict]) -> None:
    """Add ``cards`` (and ``cards_more``) to the assistant rows that carry artifacts."""
    if not messages:
        return
    asks, asked_in_turn = _ask_cards_by_message(db, thread_id, messages)
    text_rows = [
        row for row in messages
        if row.get("role") == "assistant" and isinstance(row.get("content"), str) and row["content"]
    ]
    covered_by_message: dict[str, dict[str, set[str]]] = {}
    for row in text_rows:
        # An artifact asked in a Turn is covered by that ask: the reference is marked, never dropped.
        turn_id = _loads(row.get("metadata")).get("turn_id")
        asked_here = asked_in_turn.get(turn_id, {}) if isinstance(turn_id, str) else {}
        covered = {artifact: set(holds) for artifact, holds in asked_here.items()}
        for card in asks.get(row["id"], []):
            covered.setdefault(card["artifact_id"], set()).add(card["hold_id"])
        covered_by_message[row["id"]] = covered
    references = (
        reference_cards_by_message(
            db,
            {row["id"]: row["content"] for row in text_rows},
            scope_bases(thread_row["project_id"], db),
            user_id,
            covered=covered_by_message,
        )
        if text_rows else {}
    )
    for row in messages:
        if row.get("role") != "assistant":
            continue
        cards = [*asks.get(row["id"], []), *references.get(row["id"], ([], 0))[0]]
        more = references.get(row["id"], ([], 0))[1]
        if cards:
            row["cards"] = cards
            if more:
                row["cards_more"] = more


def attach_result_cards(db, thread_id: str, user_id: str, result) -> None:
    """``attach_cards`` for a page read's result, whichever shape the read returned."""
    if result is None:
        return
    page = result["messages"] if isinstance(result, dict) else result[0]
    attach_cards(db, thread_id, user_id, get_thread_for_user(thread_id, user_id, db=db), page)
