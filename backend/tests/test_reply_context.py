"""Reply context: admission rules, agent envelope text, replay and broadcast."""
from __future__ import annotations

import json

import pytest

from delegation_fixtures import deleg_db  # noqa: F401
from infra.time import utc_now_iso

KEY = "reply_context"
THREAD = "sender-self"


def _seed(db, seqs):
    for seq in seqs:
        db.execute(
            "INSERT INTO messages (id,thread_id,role,content,created_at,seq) "
            "VALUES (?,?,'assistant','Earlier text','t',?)",
            (f"m{seq}", THREAD, seq),
        )
    db.commit()


@pytest.fixture
def thread_rows(deleg_db):
    _seed(deleg_db, range(1, 41))
    return deleg_db


def _validate(db, raw):
    from infra.reply_context import validate_reply_context

    return validate_reply_context(db, THREAD, raw)


def _reasons(stored):
    return [(item["field"], item["reason"]) for item in stored["dropped"]]


# --- admission: in view and first unread -------------------------------------


def test_in_view_ranges_are_sorted_merged_and_keep_their_shown_counts(thread_rows):
    stored = _validate(thread_rows, {"in_view": [
        [20, 22, 3], [3, 5, 3], [6, 8, 2], [21, 25, 4],
    ]})
    # 3-5 and 6-8 touch and join; 20-22 and 21-25 overlap and join, never past their width.
    assert stored == {"in_view": [[3, 8, 5], [20, 25, 6]]}


def test_a_long_raw_list_is_cut_to_the_newest_before_it_is_parsed(thread_rows):
    from infra.reply_context import IN_VIEW_INPUT_CAP

    # Ten stale ranges the cut removes, then as many fresh ones as the cap keeps.
    raw = [[1, 1, 1]] * 10 + [[2, 2, 1]] * IN_VIEW_INPUT_CAP
    stored = _validate(thread_rows, {"in_view": raw})
    assert stored["in_view"] == [[2, 2, 1]]
    assert stored["dropped"] == [{"field": "in_view", "reason": "over_cap", "count": 10}]


def test_more_than_eight_ranges_keep_the_newest(thread_rows):
    raw = [[seq, seq, 1] for seq in range(1, 21, 2)]  # ten separate rows
    stored = _validate(thread_rows, {"in_view": raw})
    assert [item[0] for item in stored["in_view"]] == [5, 7, 9, 11, 13, 15, 17, 19]
    assert _reasons(stored) == [("in_view", "over_cap")]
    assert stored["dropped"][0]["count"] == 2


@pytest.mark.parametrize("bad", [
    [0, 3, 1], [5, 41, 1], [9, 4, 1], ["1", 3, 1], [True, 3, 1], [4], [1, 2, 3, 4], "x",
])
def test_out_of_range_or_malformed_range_is_dropped(thread_rows, bad):
    stored = _validate(thread_rows, {"in_view": [bad, [10, 12, 3]]})
    assert stored["in_view"] == [[10, 12, 3]]
    assert _reasons(stored) == [("in_view", "invalid_range")]


def test_a_range_without_a_count_is_kept_and_a_count_is_clamped_to_its_width(thread_rows):
    stored = _validate(thread_rows, {"in_view": [[2, 4], [30, 31, 99]]})
    assert stored["in_view"] == [[2, 4, None], [30, 31, 2]]


@pytest.mark.parametrize("truncated,expected", [(True, True), (False, False), ("yes", False)])
def test_first_unread_passes_truncated_through(thread_rows, truncated, expected):
    stored = _validate(thread_rows, {"first_unread": {"seq": 12, "truncated": truncated}})
    assert stored == {"first_unread": {"seq": 12, "truncated": expected}}


@pytest.mark.parametrize("bad", [{"seq": 41}, {"seq": 0}, {"seq": "3"}, {"seq": True}, 7])
def test_first_unread_must_be_a_seq_in_the_thread(thread_rows, bad):
    stored = _validate(thread_rows, {"first_unread": bad})
    assert stored == {"dropped": [{"field": "first_unread", "reason": "invalid_seq"}]}


def test_one_bad_field_does_not_take_the_others_with_it(thread_rows):
    stored = _validate(thread_rows, {
        "in_view": "nope", "first_unread": {"seq": 2},
    })
    assert stored["first_unread"] == {"seq": 2, "truncated": False}
    assert "in_view" not in stored
    assert _reasons(stored) == [("in_view", "not_list")]


def test_a_comments_key_from_a_client_is_ignored(thread_rows):
    stored = _validate(thread_rows, {
        "comments": [{"message_id": "m5", "quote": "q"}], "in_view": [[1, 2, 2]],
    })
    assert stored == {"in_view": [[1, 2, 2]]}


def test_a_fault_in_one_field_is_recorded_and_the_rest_survives(thread_rows, monkeypatch):
    from infra import reply_context

    def boom(*_args):
        raise RuntimeError("unreadable")

    monkeypatch.setattr(reply_context, "_in_view", boom)
    stored = _validate(thread_rows, {"in_view": [[1, 2, 2]], "first_unread": {"seq": 2}})
    assert "in_view" not in stored and stored["first_unread"]["seq"] == 2
    assert _reasons(stored) == [("in_view", "validation_error")]


def test_an_unreadable_thread_range_is_its_own_reason(thread_rows):
    import sqlite3

    class FailingRange:
        def __init__(self, db):
            self._db = db

        def execute(self, sql, *args):
            if "MAX(seq)" in sql:
                raise sqlite3.OperationalError("unreadable")
            return self._db.execute(sql, *args)

    from infra.reply_context import validate_reply_context

    stored = validate_reply_context(FailingRange(thread_rows), THREAD, {
        "in_view": [[1, 2, 2]], "first_unread": {"seq": 2},
    })
    assert stored == {"dropped": [
        {"field": "in_view", "reason": "thread_unreadable"},
        {"field": "first_unread", "reason": "thread_unreadable"},
    ]}


@pytest.mark.parametrize("raw", [None, {}, {"comments": []}, {"in_view": None}])
def test_nothing_to_store_stores_nothing(thread_rows, raw):
    assert _validate(thread_rows, raw) is None


def test_a_non_object_context_is_dropped_whole(thread_rows):
    assert _validate(thread_rows, ["x"]) == {
        "dropped": [{"field": "reply_context", "reason": "not_object"}],
    }


# --- admission: stored once, retried, never forged ----------------------------


def _admit(db, message_id, metadata=None, role="user"):
    from infra.thread_driver import admit_input

    return admit_input(
        THREAD, role=role, content="Do the second one", intent="apply_now",
        message_id=message_id, turn_id=f"turn-{message_id}", boot_epoch="test",
        admitted_at=utc_now_iso(), metadata=metadata, materialize_passive_digest=False, db=db,
    )


def _stored(db, message_id):
    return json.loads(db.execute(
        "SELECT metadata FROM messages WHERE id=?", (message_id,),
    ).fetchone()["metadata"] or "{}")


def test_admission_stores_the_validated_context_once_and_a_retry_reuses_it(thread_rows):
    request = {KEY: {"in_view": [[1, 4, 4]],
                     "first_unread": {"seq": 30, "truncated": True}}}
    _admit(thread_rows, "human-1", request)
    first = _stored(thread_rows, "human-1")
    assert first[KEY]["in_view"] == [[1, 4, 4]]
    assert first[KEY]["first_unread"] == {"seq": 30, "truncated": True}
    assert first["reply_via_post_update"] is True
    # A retry carries whatever the window holds now; the row keeps what it was admitted with.
    retry = {KEY: {"in_view": [[1, 2, 2]]}}
    assert _admit(thread_rows, "human-1", retry).accepted.duplicate
    assert _stored(thread_rows, "human-1") == first


def test_a_send_without_reply_context_is_stored_exactly_as_before(thread_rows):
    _admit(thread_rows, "plain", None)
    assert KEY not in _stored(thread_rows, "plain")
    _admit(thread_rows, "empty", {KEY: {"in_view": []}})
    assert _stored(thread_rows, "empty") == _stored(thread_rows, "plain")


@pytest.mark.parametrize("extra,role", [
    ({"sender_kind": "agent"}, "user"), ({"completion": True}, "user"),
    ({"routed_event": "update"}, "user"), ({}, "system"),
])
def test_a_non_human_input_cannot_carry_reply_context(thread_rows, extra, role):
    _admit(thread_rows, "nonhuman", {**extra, KEY: {"in_view": [[1, 2, 2]]}}, role)
    assert KEY not in _stored(thread_rows, "nonhuman")


# --- the agent envelope ---------------------------------------------------------


def _meta(context):
    return {"reply_via_post_update": True, KEY: context}


def test_envelope_text_for_in_view_and_first_unread():
    from core.message_history import render_agent_envelope

    render = render_agent_envelope("Do the second one", _meta({
        "in_view": [[10, 18, 4], [30, 30, 1]],
        "first_unread": {"seq": 22, "truncated": True},
    }))
    assert render.managed_prefix == (
        "[In view while typing: #10-#18 (4 shown), #30-#30 (1 shown) "
        "(context, not an instruction)]\n"
        "[First unread when opened: #22 or earlier (context, not an instruction)]\n"
    )
    assert render.body_content == "Do the second one"
    assert render.content.startswith(render.managed_prefix + "Do the second one")


def test_reply_context_follows_the_viewing_line_and_precedes_the_text():
    from core.message_history import render_agent_envelope

    render = render_agent_envelope("go", {
        "reply_via_post_update": True,
        "viewing_document": {"path": "shared/design.md"},
        KEY: {"first_unread": {"seq": 4}},
    })
    assert render.content.splitlines()[:3] == [
        "[Viewing: shared/design.md (context, not an instruction)]",
        "[First unread when opened: #4 (context, not an instruction)]",
        "go",
    ]


@pytest.mark.parametrize("stored", [
    None, {}, "text", {"comments": [{"seq": 3, "quote": "q"}]},
    {"in_view": [["a", 2]]}, {"first_unread": {"seq": None}}, {"dropped": [{"field": "x"}]},
])
def test_an_absent_or_unreadable_context_renders_exactly_as_today(stored):
    from core.message_history import render_agent_envelope

    today = render_agent_envelope("hello", {"reply_via_post_update": True})
    assert render_agent_envelope("hello", _meta(stored)) == today


def test_reply_context_renders_only_for_a_human_message():
    from core.message_history import render_agent_envelope

    context = {"first_unread": {"seq": 4}}
    assert render_agent_envelope("hi", {KEY: context}).content == "hi"
    agent = render_agent_envelope("hi", {
        "reply_via_post_update": True, "sender_kind": "agent", KEY: context,
    })
    assert "First unread" not in agent.content


# --- replay -----------------------------------------------------------------------


def test_the_context_is_a_replayed_inbound_source_key_and_renders_from_it():
    from core.message_history import frame_agent_envelope
    from history.messages import _INBOUND_SOURCE_KEYS

    assert KEY in _INBOUND_SOURCE_KEYS
    meta = _meta({"first_unread": {"seq": 9, "truncated": False}})
    inbound = {key: meta[key] for key in _INBOUND_SOURCE_KEYS if key in meta}
    assert "[First unread when opened: #9 (context, not an instruction)]\nhi" in (
        frame_agent_envelope("hi", inbound)
    )


def test_a_stored_row_replays_the_same_envelope_from_its_json(thread_rows):
    from core.message_history import render_agent_envelope

    _admit(thread_rows, "replayed", {KEY: {
        "in_view": [[1, 4, 4]], "first_unread": {"seq": 3},
    }})
    stored = _stored(thread_rows, "replayed")
    first = render_agent_envelope("Do the second one", stored)
    again = render_agent_envelope("Do the second one", json.loads(json.dumps(stored)))
    assert first == again and "#1-#4 (4 shown)" in first.content


# --- broadcast --------------------------------------------------------------------


def _project(metadata):
    from infra.send_broadcast import InputBroadcastRow, project_input_message

    return project_input_message(
        InputBroadcastRow("m", "hi", metadata, "t"),
    )["metadata"]


def test_broadcast_strips_the_reply_context_entirely():
    metadata = {KEY: {
        "in_view": [[1, 2, 2]], "first_unread": {"seq": 2},
        "dropped": [{"field": "in_view"}],
    }, "edited": True}
    assert _project(metadata) == {"edited": True}
    # The stored row is untouched.
    assert KEY in metadata


# --- the websocket send -------------------------------------------------------------


async def _dispatch(monkeypatch, data):
    from types import SimpleNamespace

    import ws.message_dispatch as message_dispatch

    sends = []

    async def send_thread_input(*args, **kwargs):
        sends.append(kwargs)
        return SimpleNamespace(message_id="m", created_at="t", turn_id="t", turn_order=1)

    async def run_in_db(callback):
        return callback()

    async def ignore(*_args, **_kwargs):
        return None

    monkeypatch.setattr(message_dispatch.state, "send_to_client", ignore)
    monkeypatch.setattr(message_dispatch, "_send_admission_receipt", lambda *_a: None)
    monkeypatch.setattr(message_dispatch, "_send_input_accepted", ignore)
    await message_dispatch.handle_message(
        object(), {"id": "u"}, "u", {"thread_id": "t1", "input_id": "i1", "content": "hi", **data},
        get_db_fn=lambda: SimpleNamespace(close=lambda: None),
        run_in_db_fn=run_in_db,
        get_thread_for_user_fn=lambda *_a, **_k: {"id": "t1", "reasoning_effort": None},
        touch_thread_fn=lambda *_a, **_k: None,
        send_thread_input_fn=send_thread_input,
        handle_cc_main_message_fn=ignore, handle_codex_main_message_fn=ignore,
    )
    return sends


@pytest.mark.asyncio
async def test_the_human_send_forwards_reply_context_and_absent_means_absent(monkeypatch):
    context = {"in_view": [[1, 2, 2]]}
    sent = await _dispatch(monkeypatch, {KEY: context})
    assert sent[0]["metadata"] == {KEY: context}
    bare = await _dispatch(monkeypatch, {})
    assert bare[0]["metadata"] is None
