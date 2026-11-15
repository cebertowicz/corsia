"""``registry.predecessors``: the identities a document's promotes retired, found through an index."""

from __future__ import annotations

from doc_feedback_helpers import seed_document, seed_thread
from infra.database import get_db, write_transaction
from registry import models

INDEX = "idx_artifact_identities_successor"


def _promote(db, source, target):
    with write_transaction(db, scope="test_promote") as connection:
        models.retire(source, tombstone=True, cause="promoted", successor=target, db=connection)


def _documents(db, count):
    seed_thread(db, "owner")
    return [seed_document(db, "owner") for _ in range(count)]


def test_a_documents_predecessors_are_every_identity_whose_chain_ends_at_it(isolated_db):
    db = get_db()
    try:
        first, second, live, other = _documents(db, 4)
        _promote(db, first, second)
        _promote(db, second, live)
        assert sorted(models.predecessors(live, db=db)) == sorted([second, first])
        assert models.predecessors(second, db=db) == [first]
        assert models.predecessors(first, db=db) == []
        assert models.predecessors(other, db=db) == []
    finally:
        db.close()


def test_a_document_promoted_twelve_times_still_lists_every_earlier_identity(isolated_db):
    """No depth ends the lookup: an older identity's comments are never hidden for being old."""
    db = get_db()
    try:
        chain = _documents(db, 13)
        for source, target in zip(chain, chain[1:]):
            _promote(db, source, target)
        assert sorted(models.predecessors(chain[-1], db=db)) == sorted(chain[:-1])
    finally:
        db.close()


def test_two_identities_promoted_into_one_document_are_both_its_predecessors(isolated_db):
    db = get_db()
    try:
        left, right, live = _documents(db, 3)
        _promote(db, left, live)
        _promote(db, right, live)
        assert sorted(models.predecessors(live, db=db)) == sorted([left, right])
    finally:
        db.close()


def test_a_retirement_with_no_successor_is_no_ones_predecessor(isolated_db):
    db = get_db()
    try:
        gone, live = _documents(db, 2)
        models.retire(gone, tombstone=True, cause="file_gone", db=db)
        assert models.predecessors(live, db=db) == []
    finally:
        db.close()


def test_a_loop_of_successors_ends_and_never_lists_the_document_itself(isolated_db):
    db = get_db()
    try:
        first, second = _documents(db, 2)
        for source, target in ((first, second), (second, first)):
            db.execute(
                "UPDATE artifact_identities SET state='tombstone', metadata=json_set(metadata, '$.successor_id', ?) WHERE id=?",
                (target, source),
            )
        assert models.predecessors(first, db=db) == [second]
    finally:
        db.close()


def test_an_identity_whose_metadata_is_not_json_does_not_break_the_lookup(isolated_db):
    db = get_db()
    try:
        broken, live = _documents(db, 2)
        db.execute("UPDATE artifact_identities SET state='tombstone', metadata='not json' WHERE id=?", (broken,))
        assert models.predecessors(live, db=db) == []
    finally:
        db.close()


def test_the_lookup_uses_the_successor_index(isolated_db):
    db = get_db()
    try:
        _first, live = _documents(db, 2)
        statements: list[str] = []
        db.set_trace_callback(lambda sql: statements.append(sql) if "WITH RECURSIVE lineage" in sql else None)
        models.predecessors(live, db=db)
        db.set_trace_callback(None)
        query = statements[0][statements[0].index("WITH"):]
        plan = " ".join(str(row[-1]) for row in db.execute("EXPLAIN QUERY PLAN " + query))
        assert f"USING INDEX {INDEX}" in plan, plan
    finally:
        db.close()
