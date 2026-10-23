"""Receipt causality spans checkpoint generations but never threads."""
from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from core.lesson_delivery import TrialDelivery, draw, read_delivery, start_trials
from infra.schema import SCHEMA_SQL
from tools.filesystem_gate import canonical_target_path


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA_SQL)
    yield connection
    connection.close()


def _trial(path: Path, *, is_new: bool = False) -> TrialDelivery:
    return TrialDelivery(
        lesson_id="lesson", behavioral_sha256="a" * 64,
        source_path=path.parent / "trial" / "SKILL.md",
        target=canonical_target_path(str(path)), is_new=is_new,
        target_section="Checks", body="trial", index_line="- trial", source_data=b"trial",
    )


def _node(variant: str) -> str:
    return next(
        f"node-{number}" for number in range(1000)
        if draw("lesson", "a" * 64, f"node-{number}") == variant
    )


def _prior(db, *, thread_id: str, node_id: str, generation: str | None) -> None:
    db.execute(
        """INSERT INTO guidance_loads
           (id,thread_id,path,raw_sha256,git_blob_oid,delivered_sha256,byte_count,partial,created_at,
            node_id,variant,checkpoint_generation_id,lesson_id,lesson_behavioral_sha256)
           VALUES ('prior',?,'path','raw','oid','served',1,0,'t0',?,'with',?,'lesson',?)""",
        (thread_id, node_id, generation, "a" * 64),
    )


def test_wake_with_carries_into_g1_without(db, tmp_path, monkeypatch):
    trial = _trial(tmp_path / "new" / "SKILL.md", is_new=True)
    with_node, without_node = _node("with"), _node("without")
    monkeypatch.setattr("core.lesson_delivery._trial_deliveries", lambda *_args: ([trial], []))

    start_trials(db, thread_id="thread", node_id=with_node)
    assert start_trials(
        db, thread_id="thread", node_id=without_node, checkpoint_generation_id="g1",
    ) == []

    rows = db.execute(
        "SELECT checkpoint_generation_id,variant FROM guidance_loads ORDER BY created_at,node_id"
    ).fetchall()
    assert [tuple(row) for row in rows] == [(None, "with"), ("g1", "crossover")]


def test_precheckpoint_with_carries_into_g1_without(db, tmp_path, monkeypatch):
    target = tmp_path / "target" / "SKILL.md"
    trial = _trial(target)
    _prior(db, thread_id="thread", node_id=_node("with"), generation=None)
    monkeypatch.setattr("core.lesson_delivery._current_task", lambda *_args: _node("without"))
    monkeypatch.setattr("core.lesson_delivery._eligible_trials", lambda *_args: [trial])

    result = read_delivery(
        db, thread_id="thread", path=target, rendered_text="baseline", checkpoint_generation_id="g1",
    )

    assert result.variant == "crossover"


def test_wake_without_records_a_receipt(db, tmp_path, monkeypatch):
    trial = _trial(tmp_path / "new" / "SKILL.md", is_new=True)
    node_id = _node("without")
    monkeypatch.setattr("core.lesson_delivery._trial_deliveries", lambda *_args: ([trial], []))

    assert start_trials(db, thread_id="thread", node_id=node_id) == []
    row = db.execute(
        "SELECT node_id,variant,checkpoint_generation_id FROM guidance_loads"
    ).fetchone()
    assert tuple(row) == (node_id, "without", None)


def test_cross_thread_prior_receipt_remains_without(db, tmp_path, monkeypatch):
    target = tmp_path / "target" / "SKILL.md"
    trial = _trial(target)
    _prior(db, thread_id="other-thread", node_id=_node("with"), generation=None)
    monkeypatch.setattr("core.lesson_delivery._current_task", lambda *_args: _node("without"))
    monkeypatch.setattr("core.lesson_delivery._eligible_trials", lambda *_args: [trial])

    result = read_delivery(
        db, thread_id="thread", path=target, rendered_text="baseline", checkpoint_generation_id="g1",
    )

    assert result.variant == "without"
