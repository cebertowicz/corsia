"""Replacement admission, exact receipts, projections, and history recovery."""
import json

import pytest

from infra import presence, presence_records
from infra.database import get_db
from infra.queries import insert_message
from presence_helpers import USER, at, new_thread, pdb
from tools.post_update import _persist_update
from tools.query_helpers import thread_history_rows, search_messages_fts
from infra.thread_confinement import Reader

__all__ = ["pdb"]


def save(thread, text="original", ids=(), key=None, node=None):
    return _persist_update(thread, text, node, key, "turn", ids, "updates" if ids else None)


def test_replace_many_is_atomic_retains_raw_text_and_unions_nodes(pdb):
    thread = new_thread(pdb, "many")
    # Node associations are retained independently of the legacy singular metadata field.
    a = save(thread, "old A")[0]
    b = save(thread, "old B")[0]
    pdb.executemany("INSERT INTO message_update_nodes VALUES (?,?)", [(a, "node-a"), (b, "node-b")])
    pdb.commit()
    before = pdb.total_changes
    with pytest.raises(ValueError, match="not a known message"):
        save(thread, "no partial write", [a, "missing"])
    assert pdb.total_changes == before
    new, _, _, result = save(thread, "current", [a, b], key="replace-once")
    assert f"Replaced: {a}, {b}." in result and "Other unseen updates: 0." in result
    assert [r[0] for r in pdb.execute("SELECT content FROM messages WHERE thread_id=? ORDER BY seq", (thread,))] == ["old A", "old B", "current"]
    assert [r[0] for r in pdb.execute("SELECT node_id FROM message_update_nodes WHERE message_id=? ORDER BY node_id", (new,))] == ["node-a", "node-b"]
    assert [r[0] for r in pdb.execute("SELECT source_id FROM thread_updates WHERE thread_id=?", (thread,))] == [new]
    receipt = pdb.execute("SELECT result_text,result_chars FROM message_update_state WHERE message_id=?", (new,)).fetchone()
    assert tuple(receipt) == (result, len(result))
    successor = save(thread, "latest", [new])[0]
    assert save(thread, "current", [b, a], key="replace-once")[3] == result
    with pytest.raises(ValueError, match=successor):
        save(thread, "old target", [a])


@pytest.mark.parametrize("amount", [2000, 2001])
def test_e3_full_text_budget_and_unicode_length(pdb, amount):
    thread = new_thread(pdb, f"budget-{amount}")
    a_text = "A\r\n" + "a" * 997
    b_text = "B\n" + "é😀" * 499 + ("x" if amount == 2001 else "")
    a = save(thread, a_text)[0]
    b = save(thread, b_text)[0]
    _, _, _, result = save(thread, "new text is not echoed")
    assert "Other unseen updates: 2." in result
    assert result.index(b + " |") < result.index(a + " |")
    assert b_text in result and "new text is not echoed" not in result
    if amount == 2000:
        assert a_text in result and "Oldest updates show" not in result
    else:
        assert a_text not in result and " | A\nOldest updates show" in result
        assert 'from_seq=N, to_seq=N, tool_output="full"' in result


def test_history_search_pages_and_direct_broadcast_exclude_old_rows(pdb):
    from api.thread_paging import page_messages
    from api.thread_focus_page import page_focus, page_run_range
    from infra.send_broadcast import read_input_broadcast_row
    from tools.query_projects import _history_line

    thread = new_thread(pdb, "readers")
    old = save(thread, "obsolete unique phrase")[0]
    current = save(thread, "current", [old])[0]
    _, history = thread_history_rows(pdb, thread, reader=Reader.agent())
    assert [r["id"] for r in history] == [current]
    assert f"id: {current}" in _history_line(history[0], "full")
    _, exact = thread_history_rows(pdb, thread, reader=Reader.agent(), from_seq=1, to_seq=1, tool_output="full")
    assert current in exact[0]["content"] and "obsolete" not in exact[0]["content"]
    assert search_messages_fts(pdb, "obsolete", thread_id=thread) == []
    rows, _, _ = page_messages(pdb, thread, 50, None)
    assert [r["id"] for r in rows] == [current]
    assert rows[0]["replaced_message_ids"] == [old]
    assert read_input_broadcast_row(old, db=pdb) is None
    # A before cursor keeps the old sequence, rather than following to the new row.
    assert page_messages(pdb, thread, 50, old)[0] == []


def test_presence_here_and_visit_latches_never_become_unseen(pdb):
    thread = new_thread(pdb, "seen")
    old = save(thread)[0]
    presence_records.enter(pdb, user_id=USER, thread_id=thread, now=at(10_000_000))
    # Clearing mutable Mark read/entry fields must not clear the canonical seen latch.
    pdb.execute("UPDATE thread_presence SET last_entered_at='2020-01-01T00:00:00',marked_read_at=NULL,present=0 WHERE thread_id=?", (thread,))
    pdb.commit()
    with pytest.raises(ValueError, match="chat opened"):
        save(thread, "changed", [old])
    result = save(thread, "independent")[3]
    assert "Other unseen updates: 0." in result


def test_mark_read_then_unread_and_merged_visit_never_clear_seen_evidence(pdb):
    thread = new_thread(pdb, 'mark-read-latch')
    presence_records.enter(pdb, user_id=USER, thread_id=thread, now=at())
    pdb.execute('UPDATE thread_presence SET present=0 WHERE thread_id=?', (thread,))
    pdb.commit()
    old = save(thread)[0]
    presence_records.mark_seen(pdb, user_id=USER, thread_id=thread, unread=False, now=at(10_000_000))
    presence_records.mark_seen(pdb, user_id=USER, thread_id=thread, unread=True, now=at(10_000_001))
    pdb.execute("UPDATE thread_presence SET last_exited_at='2026-09-18T20:40:01' WHERE thread_id=?", (thread,))
    pdb.commit()
    presence_records.enter(pdb, user_id=USER, thread_id=thread, now=at(10_000_002))
    pdb.execute('UPDATE thread_presence SET present=0,marked_read_at=NULL WHERE thread_id=?', (thread,))
    pdb.commit()
    with pytest.raises(ValueError, match='Mark read'):
        save(thread, 'changed', [old])


def test_restart_unknown_stays_seen_after_durable_presence_is_cleared(pdb):
    thread = new_thread(pdb, 'restart-latch')
    old = save(thread)[0]
    # A surviving durable open claim has no process-local claimant after a restart.
    pdb.execute("INSERT INTO thread_presence(user_id,thread_id,present,last_entered_at,last_confirmed_at,updated_at) VALUES (?,?,1,'2020-01-01','2020-01-01','2020-01-01')", (USER, thread))
    pdb.commit()
    presence_records.mark_restart_exits(pdb, at(10_000_000))
    pdb.execute('UPDATE thread_presence SET present=0 WHERE thread_id=?', (thread,))
    pdb.commit()
    with pytest.raises(ValueError, match='unknown.*counts as seen'):
        save(thread, 'changed', [old])


def test_backdated_exit_is_not_viewing_evidence(pdb):
    thread = new_thread(pdb, 'exit-not-seen')
    presence_records.enter(pdb, user_id=USER, thread_id=thread, now=at())
    pdb.execute('UPDATE thread_presence SET present=0 WHERE thread_id=?', (thread,))
    pdb.commit()
    old = save(thread)[0]
    pdb.execute("UPDATE thread_presence SET last_exited_at='2027-01-01',exit_pending_at='2027-01-01' WHERE thread_id=?", (thread,))
    pdb.commit()
    assert save(thread, 'changed', [old])[0] != old


def test_posting_during_a_live_visit_is_seen_at_post(pdb):
    thread = new_thread(pdb, 'posting-here')
    presence.claim(pdb, user_id=USER, surface='chat', thread_id=thread, session='posting', now=at())
    old = save(thread)[0]
    with pytest.raises(ValueError, match='presence at post'):
        save(thread, 'changed', [old])


def test_ordinary_reply_and_unknown_legacy_update_are_not_replaceable(pdb):
    from infra.message_update_state import register

    thread = new_thread(pdb, "provenance")
    ordinary = insert_message(thread, "assistant", "ordinary", {"for_user": True, "turn_id": "turn"}, db=pdb)
    with pytest.raises(ValueError, match="ordinary reply"):
        save(thread, "changed", [ordinary])
    # Actual fallback promotion keeps its original away posting evidence.
    register(pdb, ordinary, thread, "reminder_fallback")
    pdb.commit()
    save(thread, "replacement fallback", [ordinary])
    legacy = insert_message(thread, "assistant", "legacy", {"turn_id": "turn"}, db=pdb)
    pdb.execute("DELETE FROM message_update_state WHERE message_id=?", (legacy,))
    register(pdb, legacy, thread, "legacy_update")
    pdb.commit()
    with pytest.raises(ValueError, match="unknown.*counts as seen"):
        save(thread, "unknown", [legacy])
