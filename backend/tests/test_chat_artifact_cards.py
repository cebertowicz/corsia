"""Artifact cards in chat: an agent's user ask and the files a message names.

A card is computed when a page is read. It binds to the assistant step that raised
the ask (by the tool call's id, else by its Turn), reads its round state from the
hold, never enters stored message metadata, and costs the same however much older
history there is.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from api.thread_cards import attach_cards, hydrate_update_asks
from api.thread_paging import page_messages
from infra import thread_updates
from infra.schema import SCHEMA_SQL
from plans import service
from plans.boundary import PlanActor, mutate
from registry import service as registry_service
from registry.chat_cards import ask_cards
from registry.path_cards import MAX_CARDS, reference_cards, targets, workspace_path
from tests.plan_ask_helpers import document_ask

ROOT = Path(__file__).resolve().parents[2]
VECTORS = json.loads((ROOT / "testdata" / "link-target-vectors.json").read_text())
_EARLY = "2026-10-01T10:00:00+00:00"


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    connection.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('p','P','t','t')")
    for thread_id, user_id in (("owner", "user"), ("stranger", "someone-else")):
        connection.execute(
            "INSERT INTO threads (id,user_id,project_id,model,created_at,updated_at)"
            " VALUES (?,?, 'p','test/model','t','t')",
            (thread_id, user_id),
        )
    connection.commit()
    yield connection
    connection.close()


def _node(db, title="Review the plan") -> tuple[str, str]:
    plan = service.ensure_plan("thread", "owner", db=db)
    node = service.add_node(plan.id, kind="task", title=title, acceptance_criteria="done", db=db)
    db.execute("UPDATE plan_nodes SET assignee='subagent' WHERE id=?", (node.id,))
    db.commit()
    return plan.id, node.id


def _ask(db, plan_id, node_id, artifact_id, *, turn="turn-1", calls=("inv-1", "prov-1")):
    """The agent's own user ask, through the real write path with its Turn and call ids."""
    mutate(
        plan_id, "block_node",
        {"node_id": node_id, "reason_key": "needs_review", "reason": "read it",
         "owner_kind": "user", "ask_artifact_id": artifact_id,
         "expected_revision": service._node_row(db, node_id)["revision"]},
        actor=PlanActor("owner", "user", "p", turn_id=turn, call_ids=calls),
        provenance="test.ask", mutation_id=uuid.uuid4().hex, db=db,
    )
    db.commit()
    return db.execute(
        "SELECT id FROM plan_node_holds WHERE node_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (node_id,),
    ).fetchone()["id"]


def _message(db, seq, role="assistant", *, text="", calls=(), turn=None, at=_EARLY):
    meta = {}
    if calls:
        meta["tool_calls"] = [
            {"id": c, "type": "function", "function": {"name": "plan_update", "arguments": "{}"}}
            for c in calls
        ]
    if turn:
        meta["turn_id"] = turn
    db.execute(
        "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,seq)"
        " VALUES (?, 'owner', ?, ?, ?, ?, ?)",
        (f"m{seq}", role, text, json.dumps(meta) if meta else None, at, seq),
    )
    db.commit()
    return f"m{seq}"


def _page(db):
    display, _more, _oldest = page_messages(db, "owner", 50, None)
    thread = db.execute("SELECT * FROM threads WHERE id='owner'").fetchone()
    attach_cards(db, "owner", "user", thread, display)
    return {row["id"]: row for row in display}


def _stored_metadata(db):
    return [tuple(r) for r in db.execute("SELECT id, metadata FROM messages ORDER BY seq")]


def test_an_ask_card_binds_to_the_step_whose_tool_call_raised_it(db):
    plan_id, node_id = _node(db)
    ask = document_ask(db, "owner")
    _message(db, 1, "user", text="please review")
    _message(db, 2, calls=("other-call",), turn="turn-1")
    _message(db, 3, calls=("prov-1",), turn="turn-1")
    hold_id = _ask(db, plan_id, node_id, ask)

    page = _page(db)

    assert "cards" not in page["m2"]
    assert [(c["kind"], c["hold_id"], c["artifact_id"], c["state"]) for c in page["m3"]["cards"]] == [
        ("ask", hold_id, ask, "needs_review"),
    ]
    assert page["m3"]["cards"][0]["title"] == "Review the test document"


def test_the_call_ids_reach_the_hold_so_an_earlier_step_keeps_its_card(db):
    """The ids survive the boundary's actor reload; the Turn fallback would pick the later step."""
    plan_id, node_id = _node(db)
    ask = document_ask(db, "owner")
    _message(db, 1, calls=("prov-1",), turn="turn-1", at="2026-10-01T10:00:00+00:00")
    _message(db, 2, calls=("later-call",), turn="turn-1", at="2026-10-01T10:00:05+00:00")
    hold_id = _ask(db, plan_id, node_id, ask)

    payload = json.loads(db.execute(
        "SELECT payload_json FROM plan_node_events WHERE json_extract(payload_json, '$.hold_id')=?",
        (hold_id,),
    ).fetchone()["payload_json"])
    page = _page(db)

    assert payload["raised_in"] == {"turn_id": "turn-1", "tool_invocation_id": "inv-1", "provider_call_id": "prov-1"}
    assert "turn_id" not in payload
    assert [i for i in ("m1", "m2") if page[i].get("cards")] == ["m1"]


def test_a_stored_message_is_byte_identical_after_a_page_with_cards(db):
    plan_id, node_id = _node(db)
    _message(db, 1, calls=("prov-1",), turn="turn-1", text="see [doc](x.md)")
    _ask(db, plan_id, node_id, document_ask(db, "owner"))
    before = _stored_metadata(db)

    page = _page(db)

    assert page["m1"]["cards"]
    assert _stored_metadata(db) == before
    assert "cards" not in json.loads(before[0][1])


def test_a_hold_without_call_ids_binds_to_the_latest_step_of_its_turn_at_that_time(db):
    plan_id, node_id = _node(db)
    ask = document_ask(db, "owner")
    _message(db, 1, turn="turn-1", at="2026-10-01T10:00:00+00:00")
    _message(db, 2, turn="turn-1", at="2026-10-01T10:00:05+00:00")
    _message(db, 3, turn="turn-1", at="2099-01-01T00:00:00+00:00")  # after the hold: never its step
    _message(db, 4, turn="turn-2", at="2026-10-01T10:00:06+00:00")
    _ask(db, plan_id, node_id, ask, calls=())

    page = _page(db)

    assert [i for i in ("m1", "m2", "m3", "m4") if page[i].get("cards")] == ["m2"]


def test_the_turn_fallback_compares_times_not_their_text_format(db):
    """An update stamped 'YYYY-MM-DD HH:MM:SS' still finds an ISO-stamped earlier step."""
    plan_id, node_id = _node(db)
    _message(db, 1, turn="turn-1", at="2026-10-01T10:00:05+00:00")
    _message(db, 2, turn="turn-1", at="2026-10-01T10:00:20+00:00")
    _ask(db, plan_id, node_id, document_ask(db, "owner"), calls=())
    db.execute("UPDATE thread_updates SET created_at='2026-10-01 10:00:10'")
    db.commit()

    page = _page(db)

    assert [i for i in ("m1", "m2") if page[i].get("cards")] == ["m1"]


def test_a_hold_with_neither_call_id_nor_turn_attaches_to_no_message(db):
    plan_id, node_id = _node(db)
    _message(db, 1)
    _ask(db, plan_id, node_id, document_ask(db, "owner"), turn=None, calls=())

    assert "cards" not in _page(db)["m1"]
    card = thread_updates.read_newest_cards(db, "owner", min_seq=0)[0][0]
    hydrate_update_asks(db, [card])
    assert card["ask"]["kind"] == "ask"


def test_the_blocked_update_row_carries_the_same_live_card(db):
    plan_id, node_id = _node(db)
    ask = document_ask(db, "owner")
    hold_id = _ask(db, plan_id, node_id, ask)

    [card] = thread_updates.read_newest_cards(db, "owner", min_seq=0)[0]
    hydrate_update_asks(db, [card])

    assert (card["ask"]["hold_id"], card["ask"]["state"]) == (hold_id, "needs_review")


@pytest.mark.parametrize("outcome,expected", [("answer", "answered"), ("supersede", "replaced")])
def test_a_cleared_round_reads_its_outcome_not_needs_review(db, outcome, expected):
    from registry.attention import clear_attention

    plan_id, node_id = _node(db)
    ask = document_ask(db, "owner")
    hold_id = _ask(db, plan_id, node_id, ask)
    clear_attention(ask, outcome=outcome, db=db)

    assert ask_cards(db, {hold_id: ask})[hold_id]["state"] == expected


def test_a_recorded_verdict_and_feedback_counts_survive_in_the_card(db):
    plan_id, node_id = _node(db)
    ask = document_ask(db, "owner")
    hold_id = _ask(db, plan_id, node_id, ask)
    event_id = "verdict-event"
    db.execute(
        "INSERT INTO plan_node_events (id,node_id,provenance,event,payload_json,created_at)"
        " VALUES (?,?, 'test','hold_cleared',?, 't')",
        (event_id, node_id, json.dumps({
            "hold_id": hold_id, "verdict": "approved_with_changes",
            "feedback_counts": {"comments": 2, "suggestions": 1, "edits": 0},
        })),
    )
    db.execute(
        "UPDATE plan_node_holds SET cleared_at='t', cleared_by_actor_id='user', clearance_event_id=? WHERE id=?",
        (event_id, hold_id),
    )

    card = ask_cards(db, {hold_id: ask})[hold_id]

    assert card["state"] == "approved_with_changes"
    assert card["feedback"] == {"comments": 2, "suggestions": 1}


def test_a_retired_artifact_keeps_its_title_and_reads_unavailable_and_a_gone_row_has_none(db):
    plan_id, node_id = _node(db)
    ask = document_ask(db, "owner")
    hold_id = _ask(db, plan_id, node_id, ask)
    db.execute("UPDATE artifact_identities SET state='retired' WHERE id=?", (ask,))

    card = ask_cards(db, {hold_id: ask})[hold_id]

    assert (card["title"], card["state"], card["available"], card["artifact_id"]) == (
        "Review the test document", "unavailable", False, ask,
    )
    from registry.chat_cards import card_for

    gone = card_for(None, "missing-id", kind="artifact")
    assert (gone["title"], gone["available"], gone["artifact_id"]) == ("No longer available", False, "missing-id")


def test_an_earlier_round_keeps_its_own_state_beside_a_newer_open_round(db):
    from registry.attention import clear_attention

    plan_id, node_id = _node(db)
    ask = document_ask(db, "owner")
    first = _ask(db, plan_id, node_id, ask)
    clear_attention(ask, outcome="answer", db=db)
    second = _ask(db, plan_id, node_id, ask, calls=("inv-2",))

    cards = ask_cards(db, {first: ask, second: ask})

    assert (cards[first]["state"], cards[second]["state"]) == ("answered", "needs_review")


# Focus: whether the blocked update row splits a work block (its card must show).

def _blocked_asks(db) -> list[bool]:
    return [c["blocked_asks_user"] for c in thread_updates.read_newest_cards(db, "owner", min_seq=0)[0]]


def test_an_agent_task_blocked_on_the_user_splits_then_tucks_once_answered(db):
    from registry.attention import clear_attention

    plan_id, node_id = _node(db)
    ask = document_ask(db, "owner")
    _ask(db, plan_id, node_id, ask)
    assert _blocked_asks(db) == [True]

    clear_attention(ask, outcome="answer", db=db)

    assert _blocked_asks(db) == [False]


def test_an_earlier_round_stays_tucked_while_a_newer_round_is_open(db):
    from registry.attention import clear_attention

    plan_id, node_id = _node(db)
    ask = document_ask(db, "owner")
    _ask(db, plan_id, node_id, ask)
    clear_attention(ask, outcome="answer", db=db)
    _ask(db, plan_id, node_id, ask, calls=("inv-2",))

    assert _blocked_asks(db) == [False, True]


@pytest.mark.parametrize("owner_kind", ["thread", "external", "system"])
def test_an_agent_task_blocked_on_someone_else_does_not_split(db, owner_kind):
    plan_id, node_id = _node(db)
    hold_id = f"hold-{owner_kind}"
    event_id = f"event-{owner_kind}"
    db.execute(
        "INSERT INTO plan_node_events (id,node_id,provenance,event,payload_json,created_at)"
        " VALUES (?,?, 'test','hold_placed',?, 't')",
        (event_id, node_id, json.dumps({"hold_id": hold_id, "kind": "blocked"})),
    )
    db.execute(
        "INSERT INTO plan_node_holds (id,node_id,kind,reason_key,reason,owner_kind,provenance,created_at)"
        " VALUES (?,?, 'blocked','waiting','waiting',?, 'test','t')",
        (hold_id, node_id, owner_kind),
    )
    thread_updates.record_plan_update(
        db, thread_id="owner", kind="blocked", event_id=event_id, node_id=node_id,
        created_at="2026-10-01T10:00:00",
    )

    assert _blocked_asks(db) == [False]


def test_a_legacy_user_hold_with_no_ask_artifact_splits_and_carries_no_card(db):
    plan_id, node_id = _node(db)
    db.execute(
        "INSERT INTO plan_node_events (id,node_id,provenance,event,payload_json,created_at)"
        " VALUES ('legacy-event',?, 'test','hold_placed',?, 't')",
        (node_id, json.dumps({"hold_id": "legacy", "kind": "blocked"})),
    )
    db.execute(
        "INSERT INTO plan_node_holds (id,node_id,kind,reason_key,reason,owner_kind,provenance,created_at)"
        " VALUES ('legacy',?, 'blocked','needs_input','needs input','user','test','t')",
        (node_id,),
    )
    thread_updates.record_plan_update(
        db, thread_id="owner", kind="blocked", event_id="legacy-event", node_id=node_id,
        created_at="2026-10-01T10:00:00",
    )

    [card] = thread_updates.read_newest_cards(db, "owner", min_seq=0)[0]

    assert card["blocked_asks_user"] is True
    assert card["ask"] is None


def test_the_ask_read_does_not_grow_with_older_history(db):
    plan_id, node_id = _node(db)
    _message(db, 1, calls=("prov-1",), turn="turn-1")
    _ask(db, plan_id, node_id, document_ask(db, "owner"))

    def statements():
        seen: list[str] = []
        db.set_trace_callback(seen.append)
        _page(db)
        db.set_trace_callback(None)
        return len(seen)

    baseline = statements()
    # A few thousand older asks and events elsewhere in the thread's history.
    _, old_node = _node(db, title="Old work")
    db.executemany(
        "INSERT INTO plan_node_events (id,node_id,provenance,event,payload_json,created_at)"
        " VALUES (?,?, 'test','hold_placed',?, 't')",
        [(f"old-e{i}", old_node, json.dumps({"hold_id": f"old-h{i}", "kind": "blocked"})) for i in range(3000)],
    )
    db.executemany(
        "INSERT INTO plan_node_holds (id,node_id,kind,reason_key,reason,owner_kind,owner_id,provenance,created_at,"
        "cleared_by_actor_id,cleared_at,clearance_event_id)"
        " VALUES (?,?, 'blocked','r','r','user',?, 'test','t','u','t',?)",
        [(f"old-h{i}", old_node, f"art-{i}", f"old-e{i}") for i in range(3000)],
    )
    db.executemany(
        "INSERT INTO thread_updates (thread_id,kind,source_kind,source_id,node_id,title,after_seq,created_at)"
        " VALUES ('owner','blocked','plan_event',?,?, 'Old work', 0, 't')",
        [(f"old-e{i}", old_node) for i in range(3000)],
    )
    db.commit()

    assert statements() == baseline


def test_the_ask_reads_seek_an_index_instead_of_scanning(db):
    from api import thread_cards

    queries = [
        (thread_cards._ASK_UPDATES_SQL.format(upper=" AND u.after_seq < ?"), ("owner", 1, 9, 200)),
        (thread_cards._ASK_UPDATES_SQL.format(upper=""), ("owner", 1, 200)),
        ("SELECT id FROM messages WHERE thread_id=? AND role='assistant'"
         " AND json_extract(metadata, '$.turn_id')=? AND created_at<=? ORDER BY seq DESC LIMIT 1",
         ("owner", "turn-1", "t")),
    ]
    for sql, args in queries:
        plan = [row["detail"] for row in db.execute("EXPLAIN QUERY PLAN " + sql, args)]
        assert plan and not [line for line in plan if line.startswith("SCAN")], plan


# Reference cards: the registered files a message names.

@pytest.fixture
def ws() -> Iterator[Path]:
    """A directory inside the disposable workspace, so its files register as workspace artifacts."""
    from infra import config

    root = config.WORKSPACE.resolve() / f"cards-{uuid.uuid4().hex[:8]}"
    root.mkdir(parents=True)
    yield root
    shutil.rmtree(root, ignore_errors=True)


def _registered(db, root: Path, name: str, thread="owner") -> str:
    path = root / name
    path.write_text("# doc\n")
    return str(registry_service.register_write_strict(thread, str(path), title=name, db=db)["id"])


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda c: c["href"] or "empty")
def test_the_path_rules_match_the_client_vectors(case):
    assert workspace_path(case["href"], VECTORS["roots"]) == case["expected"]


def test_only_links_and_bare_paths_outside_code_are_candidates():
    text = (
        "See [the plan](shared/plan.md) and /workspace/notes/a.md, not [site](https://x.dev/a.md).\n"
        "```\n/workspace/inside/code.md and [no](inside/fence.md)\n```\n"
        "Again [dup](shared/plan.md)."
    )

    assert targets(text) == ["shared/plan.md", "/workspace/notes/a.md"]


def test_a_message_gets_a_card_per_registered_file_it_names_and_none_for_others(db, ws):
    kept = _registered(db, ws, "plan.md")
    (ws / "unregistered.md").write_text("x")
    strangers = _registered(db, ws, "theirs.md", thread="stranger")
    text = f"[plan](plan.md) [no](unregistered.md) [theirs](theirs.md) [gone](missing.md) {ws}/plan.md"

    cards, more = reference_cards(db, text, [ws.resolve()], "user")

    assert [(c["kind"], c["artifact_id"], c["title"]) for c in cards] == [("artifact", kept, "plan.md")]
    assert more == 0 and strangers not in {c["artifact_id"] for c in cards}


def test_a_message_past_the_cap_shows_eight_cards_and_the_count_of_the_rest(db, ws):
    names = [f"f{i}.md" for i in range(MAX_CARDS + 3)]
    for name in names:
        _registered(db, ws, name)

    cards, more = reference_cards(db, " ".join(f"[x]({n})" for n in names), [ws.resolve()], "user")

    assert (len(cards), more) == (MAX_CARDS, 3)


def _marked(cards) -> list[tuple[str, str, list[str]]]:
    return [(c["kind"], c["title"], c.get("covered_by_ask", [])) for c in cards]


def test_an_asked_artifact_is_covered_by_its_ask_in_its_turn_and_never_dropped(db, ws, monkeypatch):
    """The ask rides the tool step; the turn's closing text keeps its reference, marked as covered by that ask."""
    plan_id, node_id = _node(db)
    ask = _registered(db, ws, "design.md")
    _registered(db, ws, "other.md")
    _message(db, 1, "user", text="draft it and ask me")
    _message(db, 2, calls=("prov-1",), turn="turn-1")
    _message(db, 3, turn="turn-1", text="Drafted [design](design.md) and [other](other.md).")
    _message(db, 4, "user", text="thanks")
    _message(db, 5, turn="turn-2", text="Still on [design](design.md).")
    _ask(db, plan_id, node_id, ask)
    monkeypatch.setattr("api.thread_cards.scope_bases", lambda *_a, **_k: [ws.resolve()])

    page = _page(db)

    hold = page["m2"]["cards"][0]["hold_id"]
    assert _marked(page["m2"]["cards"]) == [("ask", "design.md", [])]
    assert _marked(page["m3"]["cards"]) == [("artifact", "design.md", [hold]), ("artifact", "other.md", [])]
    assert _marked(page["m5"]["cards"]) == [("artifact", "design.md", [])]


def test_the_cover_holds_for_a_cleared_ask(db, ws, monkeypatch):
    from registry.attention import clear_attention

    plan_id, node_id = _node(db)
    ask = _registered(db, ws, "design.md")
    # Clearing posts its own message, so these sit well clear of the next sequence number.
    _message(db, 100, calls=("prov-1",), turn="turn-1", at="2026-10-01T10:00:00+00:00")
    _message(db, 101, turn="turn-1", text="See [design](design.md).", at="2026-10-01T10:00:05+00:00")
    _ask(db, plan_id, node_id, ask)
    clear_attention(ask, outcome="answer", db=db)
    monkeypatch.setattr("api.thread_cards.scope_bases", lambda *_a, **_k: [ws.resolve()])

    page = _page(db)

    cards = [c for row in page.values() for c in row.get("cards", [])]
    hold = next(c["hold_id"] for c in cards if c["kind"] == "ask")
    assert _marked(cards) == [("ask", "design.md", []), ("artifact", "design.md", [hold])]


def test_a_reference_on_the_asks_own_message_is_marked_covered_by_it(db, ws, monkeypatch):
    plan_id, node_id = _node(db)
    ask = _registered(db, ws, "design.md")
    _message(db, 1, calls=("prov-1",), turn="turn-1", text="[design](design.md) and [other](other.md)")
    _registered(db, ws, "other.md")
    _ask(db, plan_id, node_id, ask)
    thread = db.execute("SELECT * FROM threads WHERE id='owner'").fetchone()
    display, _m, _o = page_messages(db, "owner", 50, None)

    monkeypatch.setattr("api.thread_cards.scope_bases", lambda *_a, **_k: [ws.resolve()])
    attach_cards(db, "owner", "user", thread, display)

    hold = display[0]["cards"][0]["hold_id"]
    assert _marked(display[0]["cards"]) == [
        ("ask", "design.md", []), ("artifact", "design.md", [hold]), ("artifact", "other.md", []),
    ]


def test_a_reference_card_carries_the_artifact_type_the_canvas_uses_for_images(db, ws):
    (ws / "shot.png").write_bytes(b"\x89PNG\r\n")
    png = str(registry_service.register_write_strict("owner", str(ws / "shot.png"), title="Shot", db=db)["id"])
    doc = _registered(db, ws, "plan.md")

    cards, _ = reference_cards(db, "[a](shot.png) [b](plan.md)", [ws.resolve()], "user")

    by_id = {c["artifact_id"]: c for c in cards}
    assert (by_id[png]["type"], by_id[png]["icon"], by_id[png]["opens"]) == ("image", "image", True)
    assert by_id[doc]["type"] != "image"
