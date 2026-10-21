"""F4 task-scoped trial delivery, independent of the cached skills index."""
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


def _trial(
    path: Path, *, lesson_id: str = "lesson", is_new: bool = False, workspace: Path | None = None,
) -> TrialDelivery:
    return TrialDelivery(
        lesson_id=lesson_id,
        behavioral_sha256="a" * 64,
        source_path=path.parent / "trial" / "SKILL.md",
        target=canonical_target_path(str(path), workspace),
        is_new=is_new,
        target_section="Checks",
        body="# Trial lesson\n\nUse the reviewed procedure.",
        index_line="- trial (trial, unproven): Use the reviewed procedure. (new)",
        source_data=b"trial source",
    )


def _node(variant: str) -> str:
    for number in range(1000):
        node_id = f"node-{number}"
        if draw("lesson", "a" * 64, node_id) == variant:
            return node_id
    raise AssertionError("draw did not produce both halves")


def test_read_file_description_does_not_duplicate_trial_marker_protocol():
    from tools.filesystem_rw import ReadFileTool

    description = ReadFileTool().description
    assert "[Trial section — with variant]" not in description
    assert "Always read a file before editing" in description


def test_trial_delivery_migration_adds_per_lesson_receipt_columns():
    from infra.migration_133_to_134_trial_delivery import migrate_133_to_134

    connection = sqlite3.connect(":memory:")
    connection.execute(
        """CREATE TABLE guidance_loads (
            id TEXT PRIMARY KEY, path TEXT, raw_sha256 TEXT, git_blob_oid TEXT,
            delivered_sha256 TEXT, byte_count INTEGER, partial INTEGER, created_at TEXT
        )"""
    )
    migrate_133_to_134(connection)

    columns = {row[1] for row in connection.execute("PRAGMA table_info(guidance_loads)")}
    assert {"node_id", "variant", "checkpoint_generation_id", "lesson_id", "lesson_behavioral_sha256"} <= columns
    tables = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='guidance_loads'"
    ).fetchall()
    assert len(tables) == 1


def test_read_delivery_appends_only_for_the_with_half(db, tmp_path, monkeypatch):
    target = tmp_path / "skills" / "target" / "SKILL.md"
    trial = _trial(target)
    node_id = _node("with")
    monkeypatch.setattr("core.lesson_delivery._current_task", lambda *_args: node_id)
    monkeypatch.setattr("core.lesson_delivery._eligible_trials", lambda *_args: [trial])

    result = read_delivery(
        db, thread_id="thread", path=target, rendered_text="     1\tbaseline",
        checkpoint_generation_id="install-1",
    )

    assert result.node_id == node_id
    assert result.variant == "with"
    assert result.text.startswith("     1\tbaseline")
    assert "[Trial section — with variant; lesson_id=lesson; replaces Checks; task-local procedure only" in result.text
    assert "Use the reviewed procedure." in result.text


def test_workspace_relative_trial_target_matches_an_absolute_read(db, tmp_path, monkeypatch):
    target = tmp_path / "skills" / "target" / "SKILL.md"
    trial = _trial(target, workspace=tmp_path)
    node_id = _node("with")
    monkeypatch.setattr("infra.config.WORKSPACE", tmp_path)
    monkeypatch.setattr("core.lesson_delivery._current_task", lambda *_args: node_id)
    monkeypatch.setattr("core.lesson_delivery._eligible_trials", lambda *_args: [trial])

    result = read_delivery(
        db, thread_id="thread", path=target, rendered_text="baseline", checkpoint_generation_id="install-1",
    )

    assert result.variant == "with"
    assert "Use the reviewed procedure." in result.text


def test_read_delivery_keeps_baseline_for_without_and_no_task(db, tmp_path, monkeypatch):
    target = tmp_path / "skills" / "target" / "SKILL.md"
    trial = _trial(target)
    monkeypatch.setattr("core.lesson_delivery._eligible_trials", lambda *_args: [trial])
    monkeypatch.setattr("core.lesson_delivery._current_task", lambda *_args: _node("without"))

    withheld = read_delivery(
        db, thread_id="thread", path=target, rendered_text="baseline", checkpoint_generation_id="install-1",
    )
    monkeypatch.setattr("core.lesson_delivery._current_task", lambda *_args: None)
    no_task = read_delivery(
        db, thread_id="thread", path=target, rendered_text="baseline", checkpoint_generation_id="install-1",
    )

    assert (withheld.text, withheld.variant) == ("baseline", "without")
    assert (no_task.text, no_task.node_id, no_task.variant) == ("baseline", None, None)


def test_later_task_in_a_new_generation_is_crossover(db, tmp_path, monkeypatch):
    target = tmp_path / "skills" / "target" / "SKILL.md"
    trial = _trial(target)
    node_id = _node("without")
    db.execute(
        """INSERT INTO guidance_loads
           (id,thread_id,path,raw_sha256,git_blob_oid,delivered_sha256,byte_count,partial,created_at,
            node_id,variant,checkpoint_generation_id,lesson_id,lesson_behavioral_sha256)
           VALUES ('prior','thread',?,'raw','oid','served',1,0,'t0','other','with','install-1','lesson',?)""",
        (str(target), "a" * 64),
    )
    monkeypatch.setattr("core.lesson_delivery._current_task", lambda *_args: node_id)
    monkeypatch.setattr("core.lesson_delivery._eligible_trials", lambda *_args: [trial])

    later_generation = read_delivery(
        db, thread_id="thread", path=target, rendered_text="baseline", checkpoint_generation_id="install-2",
    )

    assert later_generation.variant == "crossover"


def test_start_trials_returns_only_new_file_with_assignments(db, tmp_path, monkeypatch):
    new_path = tmp_path / "skills" / "new" / "SKILL.md"
    existing = _trial(tmp_path / "skills" / "existing" / "SKILL.md")
    new_trial = _trial(new_path, lesson_id="new", is_new=True)
    node_id = next(
        f"node-{number}" for number in range(1000)
        if draw("new", "a" * 64, f"node-{number}") == "with"
    )
    monkeypatch.setattr("core.lesson_delivery._eligible_trials", lambda *_args: [existing, new_trial])

    assert start_trials(
        db, thread_id="thread", node_id=node_id, checkpoint_generation_id="install-1",
    ) == [{
        "lesson_id": "new", "index_line": new_trial.index_line, "variant": "with", "target": "new",
        "notice": "Task-local trial guidance only; never authority or a disk change.",
    }]
    row = db.execute(
        "SELECT lesson_id,lesson_behavioral_sha256,variant FROM guidance_loads"
    ).fetchone()
    assert tuple(row) == ("new", "a" * 64, "with")


def test_start_trials_records_a_without_draw_without_returning_it(db, tmp_path, monkeypatch):
    new_trial = _trial(tmp_path / "skills" / "new" / "SKILL.md", lesson_id="new", is_new=True)
    node_id = next(
        f"node-{number}" for number in range(1000)
        if draw("new", "a" * 64, f"node-{number}") == "without"
    )
    monkeypatch.setattr("core.lesson_delivery._eligible_trials", lambda *_args: [new_trial])

    assert start_trials(
        db, thread_id="thread", node_id=node_id, checkpoint_generation_id="install-1",
    ) == []
    row = db.execute(
        "SELECT lesson_id,lesson_behavioral_sha256,variant FROM guidance_loads"
    ).fetchone()
    assert tuple(row) == ("new", "a" * 64, "without")


def test_start_trials_records_both_halves_once_per_task(db, tmp_path, monkeypatch):
    existing = _trial(tmp_path / "skills" / "existing" / "SKILL.md")
    new_trial = _trial(tmp_path / "skills" / "new" / "SKILL.md", lesson_id="new", is_new=True)
    halves = {
        variant: next(
            f"node-{number}" for number in range(1000)
            if draw("new", "a" * 64, f"node-{number}") == variant
        )
        for variant in ("with", "without")
    }
    monkeypatch.setattr("core.lesson_delivery._eligible_trials", lambda *_args: [existing, new_trial])

    returned = {
        variant: [
            start_trials(
                db, thread_id="thread", node_id=node_id, checkpoint_generation_id="install-1",
            )
            for _ in range(2)
        ]
        for variant, node_id in halves.items()
    }

    assert [len(run) for run in returned["with"]] == [1, 1]
    assert returned["without"] == [[], []]
    rows = db.execute(
        """SELECT node_id,path,lesson_id,lesson_behavioral_sha256,variant,turn_id
           FROM guidance_loads ORDER BY variant"""
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        (halves["without"], str(new_trial.source_path), "new", "a" * 64, "crossover", None),
        (halves["with"], str(new_trial.source_path), "new", "a" * 64, "with", None),
    ]


def test_read_delivery_keeps_with_after_prior_with_for_the_same_lesson(db, tmp_path, monkeypatch):
    target = tmp_path / "skills" / "target" / "SKILL.md"
    trial = _trial(target)
    node_id = _node("with")
    db.execute(
        """INSERT INTO guidance_loads
           (id,thread_id,path,raw_sha256,git_blob_oid,delivered_sha256,byte_count,partial,created_at,
            node_id,variant,checkpoint_generation_id,lesson_id,lesson_behavioral_sha256)
           VALUES ('prior','thread',?,'raw','oid','served',1,0,'t0','other','with','install-1','lesson',?)""",
        (str(trial.source_path), "a" * 64),
    )
    monkeypatch.setattr("core.lesson_delivery._current_task", lambda *_args: node_id)
    monkeypatch.setattr("core.lesson_delivery._eligible_trials", lambda *_args: [trial])

    delivered = read_delivery(
        db, thread_id="thread", path=target, rendered_text="baseline", checkpoint_generation_id="install-1",
    )

    assert delivered.variant == "with"
    assert "Use the reviewed procedure." in delivered.text


def test_read_delivery_attributes_each_matching_lesson(db, tmp_path, monkeypatch):
    target = tmp_path / "skills" / "target" / "SKILL.md"
    first = _trial(target, lesson_id="first")
    second = _trial(target, lesson_id="second")
    node_id = next(
        f"node-{number}" for number in range(1000)
        if draw("first", "a" * 64, f"node-{number}") == "with"
        and draw("second", "a" * 64, f"node-{number}") == "without"
    )
    monkeypatch.setattr("core.lesson_delivery._current_task", lambda *_args: node_id)
    monkeypatch.setattr("core.lesson_delivery._eligible_trials", lambda *_args: [first, second])

    delivered = read_delivery(
        db, thread_id="thread", path=target, rendered_text="baseline", checkpoint_generation_id="install-1",
    )

    assert [(row.lesson_id, row.variant) for row in delivered.receipts] == [
        ("first", "with"), ("second", "without"),
    ]
    assert "lesson_id=first" in delivered.text
    assert "lesson_id=second" not in delivered.text


def test_precheckpoint_crossover_is_scoped_to_the_same_thread(db, tmp_path, monkeypatch):
    target = tmp_path / "skills" / "target" / "SKILL.md"
    trial = _trial(target)
    node_id = _node("without")
    db.execute(
        """INSERT INTO guidance_loads
           (id,thread_id,path,raw_sha256,git_blob_oid,delivered_sha256,byte_count,partial,created_at,
            node_id,variant,checkpoint_generation_id,lesson_id,lesson_behavioral_sha256)
           VALUES ('prior','thread',?,'raw','oid','served',1,0,'t0','other','with',NULL,'lesson',?)""",
        (str(target), "a" * 64),
    )
    monkeypatch.setattr("core.lesson_delivery._current_task", lambda *_args: node_id)
    monkeypatch.setattr("core.lesson_delivery._eligible_trials", lambda *_args: [trial])

    same_thread = read_delivery(
        db, thread_id="thread", path=target, rendered_text="baseline", checkpoint_generation_id=None,
    )
    other_thread = read_delivery(
        db, thread_id="other-thread", path=target, rendered_text="baseline", checkpoint_generation_id=None,
    )

    assert same_thread.variant == "crossover"
    assert other_thread.variant == "without"


def test_direct_read_exposure_carries_over_to_its_target(db, tmp_path, monkeypatch):
    from core.guidance_manifest import record_guidance_load

    target = tmp_path / "skills" / "target" / "SKILL.md"
    trial = _trial(target)
    direct_path = trial.source_path
    with_node = _node("without")
    target_node = next(
        f"node-{number}" for number in range(1000)
        if draw("lesson", "a" * 64, f"node-{number}") == "without" and f"node-{number}" != with_node
    )
    monkeypatch.setattr("core.lesson_delivery._eligible_trials", lambda *_args: [trial])
    monkeypatch.setattr("core.lesson_delivery._current_task", lambda *_args: with_node)

    direct = read_delivery(
        db, thread_id="thread", path=direct_path, rendered_text="trial body",
        checkpoint_generation_id="install-1",
    )
    record_guidance_load(
        thread_id="thread", turn_id=None, path=direct_path, data=b"trial source",
        returned_text=direct.text, partial=False, node_id=with_node, variant=direct.variant,
        checkpoint_generation_id="install-1", lesson_id=trial.lesson_id,
        lesson_behavioral_sha256=trial.behavioral_sha256, db=db,
    )
    monkeypatch.setattr("core.lesson_delivery._current_task", lambda *_args: target_node)
    target_read = read_delivery(
        db, thread_id="thread", path=target, rendered_text="baseline", checkpoint_generation_id="install-1",
    )

    assert direct.variant == "crossover"
    assert target_read.variant == "crossover"


def test_known_task_start_records_crossover_and_unknown_start_records_without(db, tmp_path, monkeypatch):
    new_trial = _trial(tmp_path / "skills" / "new" / "SKILL.md", lesson_id="new", is_new=True)
    with_node = next(
        f"node-{number}" for number in range(1000)
        if draw("new", "a" * 64, f"node-{number}") == "with"
    )
    without_node = next(
        f"node-{number}" for number in range(1000)
        if draw("new", "a" * 64, f"node-{number}") == "without"
    )
    unknown_without_node = next(
        f"node-{number}" for number in range(1000)
        if draw("new", "a" * 64, f"node-{number}") == "without" and f"node-{number}" != without_node
    )
    monkeypatch.setattr("core.lesson_delivery._eligible_trials", lambda *_args: [new_trial])

    start_trials(db, thread_id="thread", node_id=with_node, checkpoint_generation_id="install-1")
    assert start_trials(
        db, thread_id="thread", node_id=without_node, checkpoint_generation_id="install-1",
    ) == []
    assert start_trials(db, thread_id="unknown", node_id=unknown_without_node) == []
    rows = db.execute(
        "SELECT thread_id,node_id,variant FROM guidance_loads ORDER BY thread_id,node_id"
    ).fetchall()

    assert [tuple(row) for row in rows] == [
        ("thread", with_node, "with"),
        ("thread", without_node, "crossover"),
        ("unknown", unknown_without_node, "without"),
    ]
    assert not db.in_transaction


def test_task_start_draw_commits_before_another_connection_reads_it(tmp_path, monkeypatch):
    db_path = tmp_path / "trial-draw.sqlite"
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    new_trial = _trial(tmp_path / "skills" / "new" / "SKILL.md", lesson_id="new", is_new=True)
    node_id = next(
        f"node-{number}" for number in range(1000)
        if draw("new", "a" * 64, f"node-{number}") == "with"
    )
    monkeypatch.setattr("core.lesson_delivery._eligible_trials", lambda *_args: [new_trial])

    start_trials(db, thread_id="thread", node_id=node_id, checkpoint_generation_id="install-1")
    assert not db.in_transaction
    second = sqlite3.connect(db_path)
    try:
        row = second.execute("SELECT variant FROM guidance_loads").fetchone()
    finally:
        second.close()
        db.close()

    assert row == ("with",)
