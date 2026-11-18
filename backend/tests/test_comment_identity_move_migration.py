"""Schema 199 -> 200 moves comment state off retired identities onto the live one, and reverses exactly (#2617).

Each test builds the legacy state directly: rows keyed by an identity that a promote already retired,
which is what promotes made before ``retire`` carried comments, and writes from an old pane, left behind.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from auth_fixtures import OWNER_TOKEN, OWNER_USER
from doc_feedback_helpers import add_comment, batches, linked_comment_ids, seed_document, seed_thread
from doceditor import feedback_batches
from infra.database import get_db
from infra.doc_comments_schema import DOCUMENT_SCHEMA_SQL
from infra.migration_199_to_200_comments_off_retired_identities import (
    LEDGER,
    MOVED_COLUMNS,
    migrate_199_to_200,
    move_comments_to_live_identities,
    reverse_moves,
)
from registry.retirement_state import live_identity, retired_identity_counts

TOKEN = {"token": OWNER_TOKEN}
FAR_FUTURE = "9999-12-31T00:00:00+00:00"


@pytest.fixture
def db(isolated_db):
    connection = get_db()
    seed_thread(connection, "owner")
    connection.execute("UPDATE threads SET user_id=? WHERE id='owner'", (OWNER_USER,))
    connection.commit()
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def client(isolated_db, monkeypatch):
    from doceditor import live_runtime
    from main import app

    monkeypatch.setattr(live_runtime, "_manager", None)
    return TestClient(app, raise_server_exceptions=False)


def _retire(db, source: str, successor: str | None, cause: str = "promoted") -> None:
    """Mark ``source`` retired the way a promote does, without moving its rows (the legacy state)."""
    db.execute("UPDATE artifact_identities SET state='tombstone' WHERE id=?", (source,))
    db.execute(
        "UPDATE artifact_identities SET metadata=json_set(COALESCE(NULLIF(metadata,''),'{}'),'$.retired_cause',?)"
        " WHERE id=?", (cause, source),
    )
    if successor:
        db.execute(
            "UPDATE artifact_identities SET metadata=json_set(metadata,'$.successor_id',?) WHERE id=?",
            (successor, source),
        )
    db.commit()


def _chain(db, length: int) -> list[str]:
    """``length`` retired identities, each promoted to the next, then the live one: [first, ..., live]."""
    ids = [seed_document(db, "owner") for _ in range(length + 1)]
    for source, successor in zip(ids, ids[1:]):
        _retire(db, source, successor)
    return ids


def _held(db, table: str, artifact: str) -> list[str]:
    return sorted(row[0] for row in db.execute(f"SELECT id FROM {table} WHERE artifact_id=?", (artifact,)))


def _snapshot(db) -> dict[str, list[tuple]]:
    return {
        table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY id")]
        for table in MOVED_COLUMNS
    }


def _comment_on(db, artifact: str, *, parent: str | None = None, **kw) -> dict:
    row, _ = add_comment(db, artifact, author_kind="user", author="Steve", parent_id=parent, **kw)
    return row


def test_a_comment_and_its_reply_move_across_one_retirement(db):
    source, live = _chain(db, 1)
    root = _comment_on(db, source)
    reply = _comment_on(db, source, parent=root["id"])
    report = move_comments_to_live_identities(db)
    assert _held(db, "doc_comments", live) == sorted([root["id"], reply["id"]])
    assert _held(db, "doc_comments", source) == []
    assert report.moved["doc_comments"] == 2 and report.left_behind == {}
    ledger = db.execute(f"SELECT table_name, row_id, moved_to FROM {LEDGER} ORDER BY row_id").fetchall()
    assert sorted((r[1], r[2]) for r in ledger if r[0] == "doc_comments") == sorted(
        [(root["id"], live), (reply["id"], live)]
    )


def test_a_twelve_step_chain_moves_every_comment_to_the_final_identity(db):
    ids = _chain(db, 12)
    live = ids[-1]
    moved = [_comment_on(db, artifact)["id"] for artifact in ids[:-1]]
    assert live_identity(ids[0], db=db) == live
    move_comments_to_live_identities(db)
    assert _held(db, "doc_comments", live) == sorted(moved)
    assert all(_held(db, "doc_comments", artifact) == [] for artifact in ids[:-1])


def test_comments_move_even_when_the_live_identity_already_holds_comments(db):
    source, live = _chain(db, 1)
    mine = _comment_on(db, source)
    theirs = _comment_on(db, live)
    move_comments_to_live_identities(db)
    assert _held(db, "doc_comments", live) == sorted([mine["id"], theirs["id"]])
    assert _held(db, "doc_comments", source) == []


def test_a_reply_whose_parent_is_a_comment_on_another_document_stays_and_is_counted(db):
    source, live = _chain(db, 1)
    stranger = seed_document(db, "owner")
    foreign_root = _comment_on(db, stranger)
    stray = _comment_on(db, source)
    db.execute("UPDATE doc_comments SET parent_id=? WHERE id=?", (foreign_root["id"], stray["id"]))
    db.commit()
    own = _comment_on(db, source)
    db.commit()
    report = move_comments_to_live_identities(db)
    assert _held(db, "doc_comments", source) == [stray["id"]]
    assert own["id"] in _held(db, "doc_comments", live)
    assert report.left_behind == {"parent_outside_lineage": 1}


def test_a_chain_that_ends_at_no_active_identity_stays_and_is_counted(db):
    source, middle = _chain(db, 1)
    _retire(db, middle, None, cause="file_gone")
    kept = [_comment_on(db, source)["id"], _comment_on(db, source)["id"]]
    report = move_comments_to_live_identities(db)
    assert _held(db, "doc_comments", source) == sorted(kept)
    assert report.left_behind == {"no_live_identity": 2} and report.moved["doc_comments"] == 0


def test_a_loop_in_the_chain_stays_and_is_counted(db):
    first, second = (seed_document(db, "owner") for _ in range(2))
    comment = _comment_on(db, first)
    _retire(db, first, second)
    _retire(db, second, first)
    report = move_comments_to_live_identities(db)
    assert _held(db, "doc_comments", first) == [comment["id"]] and report.left_behind == {"no_live_identity": 1}


def test_a_second_run_changes_nothing(db):
    source = _chain(db, 2)[0]
    _comment_on(db, source)
    migrate_199_to_200(db)
    after_first = _snapshot(db), db.execute(f"SELECT COUNT(*) FROM {LEDGER}").fetchone()[0]
    report = move_comments_to_live_identities(db)
    assert (_snapshot(db), db.execute(f"SELECT COUNT(*) FROM {LEDGER}").fetchone()[0]) == after_first
    assert sum(report.moved.values()) == 0


def _legacy_state(db):
    """One retired identity holding a comment with its batch, a review request and a section writer line."""
    source, live = _chain(db, 1)
    root = _comment_on(db, source)
    seed_thread(db, "agent1")
    db.execute(
        "INSERT INTO doc_review_requests(id, artifact_id, requester_kind, requester_id, reviewer_thread_id,"
        " client_ids, created_at) VALUES ('rr1', ?, 'user', 'u', 'agent1', '[]', '2026-01-01T00:00:00+00:00')",
        (source,),
    )
    source_path = db.execute("SELECT path FROM artifact_identities WHERE id=?", (source,)).fetchone()[0]
    ops = json.dumps([{"call": {"path": source_path, "text": "x"}}])
    db.execute(
        "INSERT INTO doc_comment_writer_line(id, root_id, artifact_id, agent_thread_id, user_id, ops, saved_at)"
        " VALUES ('wl1', ?, ?, 'agent1', 'u', ?, '2026-01-01T00:00:00+00:00')",
        (f"section:{source}:Intro", source, ops),
    )
    db.commit()
    return source, live, root


def test_every_kind_of_row_moves_and_the_section_line_is_rekeyed_and_repointed(db):
    source, live, root = _legacy_state(db)
    report = move_comments_to_live_identities(db)
    assert _held(db, "doc_review_requests", live) == ["rr1"]
    assert len(_held(db, "doc_feedback_batches", live)) == 1
    line = db.execute("SELECT artifact_id, root_id, ops FROM doc_comment_writer_line WHERE id='wl1'").fetchone()
    live_path = db.execute("SELECT path FROM artifact_identities WHERE id=?", (live,)).fetchone()[0]
    assert (line[0], line[1]) == (live, f"section:{live}:Intro")
    assert live_path != source and json.loads(line[2])[0]["call"]["path"] == live_path
    assert report.left_behind == {}


def test_the_ledger_and_the_reverse_restore_every_row_exactly(db):
    _legacy_state(db)
    before = _snapshot(db)
    migrate_199_to_200(db)
    assert _snapshot(db) != before
    counts = reverse_moves(db)
    db.commit()
    assert _snapshot(db) == before
    assert counts["reversed"] == db.execute("SELECT COUNT(*) FROM doc_comments").fetchone()[0] + 3
    assert counts["changed_since"] == counts["blocked"] == counts["gone"] == 0
    assert db.execute(f"SELECT COUNT(*) FROM {LEDGER}").fetchone()[0] == 0


def test_the_reverse_leaves_a_row_someone_changed_since_and_drops_one_that_is_gone(db):
    source, live, root = _legacy_state(db)
    other = _comment_on(db, source)
    migrate_199_to_200(db)
    third = seed_document(db, "owner")
    db.execute("UPDATE doc_comments SET artifact_id=? WHERE id=?", (third, root["id"]))
    db.execute("DELETE FROM doc_comments WHERE id=?", (other["id"],))
    counts = reverse_moves(db)
    assert counts["changed_since"] == 1 and counts["gone"] == 1
    assert _held(db, "doc_comments", third) == [root["id"]]


def test_a_batch_that_would_collide_is_folded_and_a_section_line_that_would_stays_and_is_counted(db):
    source, live = _chain(db, 1)
    comment = _comment_on(db, source)
    _comment_on(db, live)
    seed_thread(db, "agent1")
    for artifact in (source, live):
        db.execute(
            "INSERT INTO doc_comment_writer_line(id, root_id, artifact_id, agent_thread_id, user_id, ops, saved_at)"
            " VALUES (?, ?, ?, 'agent1', 'u', '[]', '2026-01-01T00:00:00+00:00')",
            (f"wl-{artifact}", f"section:{artifact}:Intro", artifact),
        )
    db.commit()
    assert len([b for b in batches(db, state="open") if b["artifact_id"] == source]) == 1
    report = move_comments_to_live_identities(db)
    assert report.left_behind == {"section_writer_line_collision": 1} and report.folded == 1
    assert comment["id"] in _held(db, "doc_comments", live)
    assert [b["artifact_id"] for b in batches(db, state="open")] == [live]
    assert _held(db, "doc_comment_writer_line", source) == [f"wl-{source}"]


def test_a_comment_whose_batch_was_folded_is_listed_on_the_live_document_and_its_feedback_is_in_the_live_batch(db, client):
    source, live = _chain(db, 1)
    comment = _comment_on(db, source)
    other = _comment_on(db, live)
    db.commit()
    move_comments_to_live_identities(db)
    db.commit()
    (batch,) = batches(db, state="open")
    assert batch["artifact_id"] == live and batch["item_count"] == 2
    assert linked_comment_ids(db, batch["id"]) == sorted([comment["id"], other["id"]])
    for artifact in (live, source):
        response = client.get(f"/api/artifacts/{artifact}/comments", params=TOKEN)
        assert response.status_code == 200, response.text
        assert comment["id"] in {thread["id"] for thread in response.json()["comments"]}


def test_a_folded_batch_is_delivered_with_the_live_one_and_nothing_stays_stranded(db):
    source, live = _chain(db, 1)
    _comment_on(db, source)
    _comment_on(db, live)
    db.commit()
    move_comments_to_live_identities(db)
    db.commit()
    feedback_batches.close_due(FAR_FUTURE, db=db)
    (batch,) = batches(db)
    assert (batch["artifact_id"], batch["state"], batch["item_count"]) == (live, "delivered", 2)
    assert retired_identity_counts(db) == (0, 0)


def _feedback_state(db):
    return (
        [tuple(row) for row in db.execute("SELECT * FROM doc_feedback_batches ORDER BY id")],
        [tuple(row) for row in db.execute("SELECT batch_id, comment_id, event FROM doc_feedback_batch_items ORDER BY 1, 2")],
    )


def test_the_reverse_splits_a_folded_batch_back_out_exactly(db):
    source, live = _chain(db, 1)
    _comment_on(db, source)
    _comment_on(db, source)
    _comment_on(db, live)
    db.commit()
    before, comments_before = _feedback_state(db), _snapshot(db)
    move_comments_to_live_identities(db)
    db.commit()
    assert len(batches(db)) == 1 and _feedback_state(db) != before
    counts = reverse_moves(db)
    db.commit()
    assert counts["blocked"] == counts["changed_since"] == 0
    assert _feedback_state(db) == before and _snapshot(db) == comments_before
    assert db.execute(f"SELECT COUNT(*) FROM {LEDGER}").fetchone()[0] == 0


def test_the_reverse_leaves_a_fold_alone_once_the_live_batch_was_delivered(db):
    source, live = _chain(db, 1)
    _comment_on(db, source)
    _comment_on(db, live)
    db.commit()
    move_comments_to_live_identities(db)
    db.commit()
    feedback_batches.close_due(FAR_FUTURE, db=db)
    assert reverse_moves(db)["changed_since"] == 1
    assert [b["artifact_id"] for b in batches(db)] == [live]


EDIT_SQL = (
    "INSERT INTO doc_feedback_edit_items(batch_id, actor_display, baseline_sha256, baseline_text, latest_sha256,"
    " latest_text, save_count, first_at, last_at) VALUES (?, 'a', 'h', 't', 'h', 't', 1,"
    " '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
)


def _open_batch_on(db, artifact: str) -> dict:
    return next(b for b in batches(db, state="open") if b["artifact_id"] == artifact)


def _edit_owners(db) -> list[str]:
    return [row[0] for row in db.execute("SELECT batch_id FROM doc_feedback_edit_items")]


def test_an_edit_snapshot_moves_with_a_fold_when_the_live_batch_has_none_and_the_reverse_puts_it_back(db):
    source, live = _chain(db, 1)
    _comment_on(db, source)
    _comment_on(db, live)
    stranded = _open_batch_on(db, source)
    db.execute(EDIT_SQL, (stranded["id"],))
    db.commit()
    before = _feedback_state(db)
    report = move_comments_to_live_identities(db)
    db.commit()
    assert report.folded == 1 and report.left_behind == {}
    assert _edit_owners(db) == [_open_batch_on(db, live)["id"]]
    assert reverse_moves(db)["reversed"] >= 1
    db.commit()
    assert _edit_owners(db) == [stranded["id"]] and _feedback_state(db) == before


def test_batches_with_an_edit_snapshot_on_both_are_not_folded_and_deliver_on_the_live_document_when_due(db):
    source, live = _chain(db, 1)
    _comment_on(db, source)
    _comment_on(db, live)
    for artifact in (source, live):
        db.execute(EDIT_SQL, (_open_batch_on(db, artifact)["id"],))
    db.commit()
    report = move_comments_to_live_identities(db)
    db.commit()
    assert report.left_behind == {"open_feedback_batch_edit_items_on_both": 1} and report.folded == 0
    feedback_batches.close_due(FAR_FUTURE, db=db)
    assert sorted((b["artifact_id"], b["state"]) for b in batches(db)) == [(live, "delivered"), (live, "delivered")]
    assert retired_identity_counts(db) == (0, 0)


def test_a_batch_whose_comment_is_in_the_live_batch_with_another_reopen_event_is_not_folded(db):
    source, live = _chain(db, 1)
    shared = _comment_on(db, source)
    _comment_on(db, live)
    stranded, target = _open_batch_on(db, source), _open_batch_on(db, live)
    db.execute("INSERT INTO doc_feedback_batch_items(batch_id, comment_id, event, event_at, event_by)"
               " VALUES (?, ?, 'reopened', '2026-01-02T00:00:00+00:00', 'u')", (target["id"], shared["id"]))
    db.commit()
    report = move_comments_to_live_identities(db)
    assert report.left_behind == {"open_feedback_batch_event_conflict": 1} and report.folded == 0
    assert {b["id"] for b in batches(db, state="open")} == {stranded["id"], target["id"]}


def test_the_reverse_refuses_a_fold_whose_live_batch_changed_after_the_migration(db):
    source, live = _chain(db, 1)
    _comment_on(db, source)
    _comment_on(db, live)
    move_comments_to_live_identities(db)
    db.commit()
    target = _open_batch_on(db, live)
    added = _comment_on(db, live)  # joins the live batch after the migration
    db.commit()
    assert added["id"] in linked_comment_ids(db, target["id"])
    counts = reverse_moves(db)
    assert counts["changed_since"] == 1 and len(batches(db)) == 1


def _region(db, comment: dict, number: int, image: str = "diagram.png") -> None:
    db.execute(
        "UPDATE doc_comments SET region_x=0.1, region_y=0.1, region_w=0.2, region_h=0.2,"
        " region_number=?, image_src=? WHERE id=?", (number, image, comment["id"]),
    )
    db.commit()


def _numbers(db, artifact: str) -> dict[str, int]:
    return {row[0]: row[1] for row in db.execute(
        "SELECT id, region_number FROM doc_comments WHERE artifact_id=? AND region_number IS NOT NULL", (artifact,))}


def test_a_region_number_the_live_document_already_uses_takes_the_next_free_one_and_the_reverse_restores_it(db):
    """The unique index on (document, image, number) means a clash cannot be kept; the comment still moves."""
    source, live = _chain(db, 1)
    mine, theirs, kept = _comment_on(db, source), _comment_on(db, live), _comment_on(db, source)
    for row, number, image in ((mine, 1, "diagram.png"), (theirs, 1, "diagram.png"), (kept, 1, "other.png")):
        _region(db, row, number, image)
    before = _snapshot(db)
    report = move_comments_to_live_identities(db)
    assert _numbers(db, live) == {theirs["id"]: 1, mine["id"]: 2, kept["id"]: 1}
    assert report.renumbered == 1 and report.moved["doc_comments"] == 2
    assert reverse_moves(db)["reversed"] >= 2
    db.commit()
    assert _snapshot(db) == before


def test_two_retired_identities_with_the_same_region_number_move_to_one_live_document(db):
    first, middle, live = _chain(db, 2)
    one, two = _comment_on(db, first), _comment_on(db, middle)
    for row in (one, two):
        _region(db, row, 1)
    move_comments_to_live_identities(db)
    assert sorted(_numbers(db, live).values()) == [1, 2]


def test_a_pane_left_open_on_the_old_id_sees_every_comment_and_its_reply_lands_with_them(db, client):
    source, live = _chain(db, 1)
    roots = [_comment_on(db, source) for _ in range(4)]
    db.commit()
    migrate_199_to_200(db)
    db.commit()
    response = client.post(
        f"/api/artifacts/{source}/comments", params=TOKEN,
        json={"body": "from the open pane", "parent_id": roots[0]["id"], "delivery_policy": "immediate"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["comment"]["artifact_id"] == live
    listed = client.get(f"/api/artifacts/{source}/comments", params=TOKEN).json()["comments"]
    assert {thread["id"] for thread in listed} == {root["id"] for root in roots}
    assert "from the open pane" in [reply["body"] for reply in listed[0]["replies"]] + [
        reply["body"] for thread in listed for reply in thread["replies"]
    ]


def test_the_startup_counts_fall_to_zero_after_a_clean_move_and_show_what_stayed(db):
    source, live = _chain(db, 1)
    _comment_on(db, source)
    assert retired_identity_counts(db) == (1, 1)
    move_comments_to_live_identities(db)
    assert retired_identity_counts(db) == (0, 0)
    stranger = seed_document(db, "owner")
    foreign_root = _comment_on(db, stranger)
    other = seed_document(db, "owner")
    _retire(db, other, live)
    stray = _comment_on(db, other)
    db.execute("UPDATE doc_comments SET parent_id=? WHERE id=?", (foreign_root["id"], stray["id"]))
    db.commit()
    move_comments_to_live_identities(db)
    # the stray reply stays, and its open batch is folded into the live one: only the comment is left
    assert retired_identity_counts(db) == (1, 0)


def _ledger_database(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(DOCUMENT_SCHEMA_SQL)
    conn.execute(
        "INSERT INTO doc_comments(id, artifact_id, author, body, created_at, updated_at)"
        " VALUES ('c1', 'live', 'a', 'b', 't', 't')"
    )
    conn.execute(
        f"INSERT INTO {LEDGER}(table_name, row_id, original, moved_to, moved_at)"
        " VALUES ('doc_comments', 'c1', '{\"artifact_id\": \"old\"}', 'live', 't')"
    )
    conn.commit()
    conn.close()


def _script():
    import importlib.util

    path = Path(__file__).resolve().parents[2] / "scripts" / "migrations" / "reverse_comment_identity_move.py"
    spec = importlib.util.spec_from_file_location("reverse_comment_identity_move", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _owner_of(path: Path) -> str:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT artifact_id FROM doc_comments WHERE id='c1'").fetchone()[0]
    finally:
        conn.close()


def test_the_reverse_script_needs_a_named_database_and_changes_nothing_without_apply(tmp_path, capsys):
    script = _script()
    with pytest.raises(SystemExit):
        script.main([])
    database = tmp_path / "dry.db"
    _ledger_database(database)
    assert script.main(["--db", str(database)]) == 0
    assert _owner_of(database) == "live" and "Dry run" in capsys.readouterr().out
    assert script.main(["--db", str(database), "--apply"]) == 0
    assert _owner_of(database) == "old"
    assert script.main(["--db", str(tmp_path / "missing.db")]) == 1
