"""Single and batch suggestion paths share the accepted sibling predicate."""

import sqlite3

from doceditor import comments
from doceditor.comment_batch_actions import _reread_open

_COLUMNS = ("id", "artifact_id", "parent_id", "anchor_text", "from_off", "to_off", "anchor_prefix", "anchor_suffix",
            "replacement", "insert_after", "decision", "resolved", "review", "created_at")


def comments_db(*rows: dict) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(f"CREATE TABLE doc_comments ({', '.join(_COLUMNS)})")
    for row in rows:
        values = {"parent_id": None, "from_off": 0, "to_off": 7, "anchor_prefix": "", "anchor_suffix": "\n",
                  "insert_after": 0, "decision": None, "resolved": 0, "review": 0, **row}
        db.execute(f"INSERT INTO doc_comments VALUES ({','.join('?' * len(_COLUMNS))})", [values[c] for c in _COLUMNS])
    return db


def counted_reads(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []
    real = comments.accepted_insertions

    def counted(artifact_id, anchor_texts, *, db):
        calls.append(list(anchor_texts))
        return real(artifact_id, anchor_texts, db=db)

    monkeypatch.setattr(comments, "accepted_insertions", counted)
    return calls


def test_batch_rereads_only_the_chosen_rows_and_siblings_in_one_query(monkeypatch):
    insertion = {"artifact_id": "doc", "anchor_text": "Heading", "insert_after": 1}
    db = comments_db(
        {**insertion, "id": "a", "replacement": " one", "created_at": "3"},
        {**insertion, "id": "b", "replacement": " two", "created_at": "4"},
        {"id": "c", "artifact_id": "doc", "anchor_text": "Other", "replacement": "Else", "created_at": "5"},
        {**insertion, "id": "d", "replacement": " gone", "created_at": "6", "decision": "accepted"},
        {**insertion, "id": "s1", "replacement": " kept", "created_at": "1", "decision": "accepted"},
        {**insertion, "id": "s2", "replacement": " no", "created_at": "2", "decision": "rejected"},
        {**insertion, "id": "s3", "anchor_text": "Other", "replacement": " x", "created_at": "2", "decision": "accepted"},
        {**insertion, "id": "elsewhere", "artifact_id": "other-doc", "replacement": " y", "created_at": "1", "decision": "accepted"},
    )
    calls = counted_reads(monkeypatch)
    statements: list[str] = []
    db.set_trace_callback(statements.append)
    # As loaded before the lock: d was still open then, and was accepted meanwhile.
    chosen = [{"id": key, "replies": []} for key in ("a", "b", "c", "d")]

    fresh = _reread_open(db, "doc", chosen)

    assert [row["id"] for row in fresh] == ["a", "b", "c"]
    assert calls == [["Heading"]]
    assert len([sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]) == 2
    # d, accepted meanwhile, is now a sibling the batch's insertions must follow.
    assert [sibling["id"] for sibling in fresh[0]["_accepted_insertions"]] == ["s1", "d"]
    assert fresh[1]["_accepted_insertions"] is fresh[0]["_accepted_insertions"]
    assert "_accepted_insertions" not in fresh[2]
    assert fresh[0]["replies"] == []
    # Nothing reads the document's whole comment list under the lock.
    assert not any("ORDER BY created_at" in sql and "anchor_text" not in sql for sql in statements)


def test_batch_with_several_quotes_gives_each_insertion_its_own_siblings_in_order(monkeypatch):
    def insertion(key: str, quote: str, created: str, decision: str | None = None) -> dict:
        return {"id": key, "artifact_id": "doc", "anchor_text": quote, "insert_after": 1, "replacement": f" {key}",
                "created_at": created, "decision": decision}

    db = comments_db(
        insertion("h-open", "Heading", "9"),
        insertion("i-open", "Intro", "9"),
        insertion("t-open", "Tail", "9"),
        insertion("h2", "Heading", "3", "accepted"),
        insertion("h1", "Heading", "1", "accepted"),
        insertion("i1", "Intro", "2", "accepted"),
        insertion("i-rejected", "Intro", "1", "rejected"),
        insertion("x1", "Elsewhere", "1", "accepted"),
    )
    calls = counted_reads(monkeypatch)

    fresh = _reread_open(db, "doc", [{"id": key} for key in ("h-open", "i-open", "t-open")])

    assert calls == [["Heading", "Intro", "Tail"]]
    siblings = {row["id"]: [sibling["id"] for sibling in row["_accepted_insertions"]] for row in fresh}
    assert siblings == {"h-open": ["h1", "h2"], "i-open": ["i1"], "t-open": []}
