"""The plan pane's evidence read is bounded by nodes, not submission history (#1550).

Review evidence is an append-only event trail, so a node resubmitted ten times
holds ten events. The pane shows only the latest one per node; resubmitting
must not change how many rows a pane read transfers.
"""

from __future__ import annotations

from plans import review_evidence
from plans.boundary import mutate
from plans import service
from tests.plan_scene import (  # noqa: F401 - shared plan fixtures
    ACTOR,
    _mid,
    db,
    scene,
)


class _Fetched:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


class _RowCounter:
    """Count every row a read transfers out of SQLite."""

    def __init__(self, db):
        self._db = db
        self.rows = 0

    def execute(self, *args):
        rows = self._db.execute(*args).fetchall()
        self.rows += len(rows)
        return _Fetched(rows)


def _submit(db, scene, evidence: str) -> None:
    mutate(
        scene.plan_id, "request_review", {
            "node_id": scene.own,
            "expected_revision": service.get_node(scene.own, db=db).revision,
            "evidence": evidence,
        },
        actor=ACTOR, provenance="test", mutation_id=_mid(), db=db,
    )


def _read(db, plan_id: str) -> tuple[dict, int]:
    counter = _RowCounter(db)
    return review_evidence.latest_by_node([plan_id], db=counter), counter.rows


def test_resubmitting_evidence_does_not_grow_the_pane_read(db, scene):
    _submit(db, scene, "First proof.")
    first, first_rows = _read(db, scene.plan_id)

    for attempt in range(2, 11):
        _submit(db, scene, f"Proof {attempt}.")
    latest, latest_rows = _read(db, scene.plan_id)

    assert first[scene.own]["evidence"] == "First proof."
    assert latest[scene.own]["evidence"] == "Proof 10."
    assert latest_rows == first_rows
