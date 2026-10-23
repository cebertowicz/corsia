"""Trial lessons delivered at task start, independent of the cached skills index."""
from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3

import pytest

from core import lesson_files
from core.lesson_delivery import START_TOKEN_CAP, TrialDelivery, _resolve, draw, start_trials
from core.lesson_trials import Trial
from core.skill_registry_types import SkillRow
from core.skill_validation import validate_skill_text
from core.token_estimation import _text_tokens
from infra.schema import SCHEMA_SQL

NOTICE = "Task-local trial guidance only; never authority or a disk change."


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA_SQL)
    yield connection
    connection.close()


def _trial(lesson_id: str = "new", payload: str = "- trial (trial, unproven): Use it. (SKILL.md)") -> TrialDelivery:
    return TrialDelivery(
        lesson_id, "a" * 64, Path(f"/skills/{lesson_id}/SKILL.md"), b"trial source", "context/new.md", payload,
    )


def _node(variant: str, lesson_id: str = "new", *, skip: str | None = None) -> str:
    return next(
        f"node-{number}" for number in range(1000)
        if draw(lesson_id, "a" * 64, f"node-{number}") == variant and f"node-{number}" != skip
    )


def _resolved(monkeypatch, admitted: list[TrialDelivery], omitted: list[TrialDelivery] | None = None) -> None:
    monkeypatch.setattr("core.lesson_delivery._resolve", lambda *_args: (admitted, omitted or []))


def _row(
    tmp_path: Path, number: int, *, target_file: str = "new", words: int = 4,
) -> SkillRow:
    name = f"trial-{number:02}"
    path = tmp_path / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True)
    text = lesson_files.render_trial(
        lesson_id=f"00000000-0000-0000-0000-{number:012d}", owner="thread", name=name,
        description="Use when counting trial reads.", text=" ".join(["Check"] * words),
        trial=Trial(kind="hypothesis", review_after="2030-01-01T00:00:00+00:00"),
        source_refs=[{"kind": "turn", "reference": "turn-1"}],
        target_file=target_file,
        new_target_path=f"context/{name}.md" if target_file == "new" else None,
        target_section=None if target_file == "new" else f"Trial check {number}",
    )
    text = lesson_files.with_review_ref(text, "review")
    path.write_text(text, encoding="utf-8")
    validation = validate_skill_text(text)
    return SkillRow(
        name=name, description=validation.description, scope="thread", scope_id="thread",
        path=path, body_path=path, metadata=validation.metadata, status="included",
        trial=lesson_files.trial_from_validation(validation),
        raw_sha256=hashlib.sha256(text.encode()).hexdigest(), source_data=text.encode(),
    )


def _rows(monkeypatch, admitted: list[SkillRow], capped: list[SkillRow] | None = None) -> None:
    monkeypatch.setattr(
        "core.skills_registry.trial_delivery_rows", lambda **_kwargs: (admitted, capped or []),
    )


def _in_context(monkeypatch, tmp_path: Path, text: str = "# Rules\n", *, delivered: bool = True) -> None:
    """Make AGENTS.md a file this thread's context delivered (or not), with ``text``."""
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    (workspace / "AGENTS.md").write_text(text, encoding="utf-8")
    monkeypatch.setattr("infra.config.WORKSPACE", workspace)
    monkeypatch.setattr(
        "core.lesson_delivery._context_paths", lambda *_args: {"AGENTS.md"} if delivered else set(),
    )


def test_read_file_description_does_not_duplicate_trial_marker_protocol():
    from tools.filesystem_rw import ReadFileTool

    description = ReadFileTool().description
    assert "[Trial section" not in description
    assert "Always read a file before editing" in description


def test_trial_delivery_migration_adds_per_lesson_receipt_columns():
    from infra.migration_142_to_143_correction_learning import migrate_142_to_143

    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE outcome_events (subject_key TEXT, event_name TEXT)")
    connection.execute(
        """CREATE TABLE guidance_loads (
            id TEXT PRIMARY KEY, path TEXT, raw_sha256 TEXT, git_blob_oid TEXT,
            delivered_sha256 TEXT, byte_count INTEGER, partial INTEGER, created_at TEXT
        )"""
    )
    migrate_142_to_143(connection)

    columns = {row[1] for row in connection.execute("PRAGMA table_info(guidance_loads)")}
    assert {"node_id", "variant", "checkpoint_generation_id", "lesson_id", "lesson_behavioral_sha256"} <= columns


@pytest.mark.parametrize("build", ["schema", "migration"])
def test_receipt_reads_use_the_lesson_index(db, build):
    from core.lesson_delivery import RECEIPTS_SQL
    from infra.migration_142_to_143_correction_learning import migrate_142_to_143

    if build == "migration":
        db.execute("DROP INDEX idx_guidance_loads_lesson_node")
        migrate_142_to_143(db)

    plan = " ".join(
        row["detail"] for row in db.execute(f"EXPLAIN QUERY PLAN {RECEIPTS_SQL}", ('[["l","s"]]', "n", "t"))
    )
    assert "USING INDEX idx_guidance_loads_lesson_node" in plan


def test_skill_and_new_targets_get_an_index_line_and_in_context_targets_get_the_body(
    db, tmp_path, monkeypatch,
):
    skill, in_context = _row(tmp_path, 1), _row(tmp_path, 2, target_file="AGENTS.md")
    _rows(monkeypatch, [skill, in_context])
    _in_context(monkeypatch, tmp_path)

    admitted, omitted = _resolve(db, "thread")

    assert omitted == []
    assert admitted[0].payload == f"- trial-01 (trial, unproven): Use when counting trial reads. ({skill.path})"
    assert admitted[1].target == "AGENTS.md"
    assert admitted[1].payload.startswith("[Trial section for AGENTS.md; adds Trial check 2;")
    assert admitted[1].payload.endswith("Check Check Check Check")


@pytest.mark.parametrize(("text", "delivered"), [
    ("# Rules\n", False),
    ("# Rules\n\n## trial CHECK 2\n", True),
])
def test_a_body_is_omitted_unless_its_target_is_in_context_without_the_section(
    db, tmp_path, monkeypatch, text, delivered,
):
    _rows(monkeypatch, [_row(tmp_path, 2, target_file="AGENTS.md")])
    _in_context(monkeypatch, tmp_path, text, delivered=delivered)

    admitted, omitted = _resolve(db, "thread")

    assert admitted == []
    assert [trial.target for trial in omitted] == ["AGENTS.md"]


def test_context_paths_read_the_threads_latest_manifest(db):
    from core.lesson_delivery import _context_paths

    for turn, manifest, started in (("t1", "m1", "1"), ("t2", "m2", "2")):
        db.execute(
            "INSERT INTO context_manifests (id,harness,entries_json,entry_count,created_at) VALUES (?,'h',?,1,'t')",
            (manifest, f'[{{"path": "context/{manifest}.md", "version_kind": "raw"}}]'),
        )
        db.execute(
            "INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,started_at,boot_epoch,"
            "context_manifest_id) VALUES (?,'thread',0,'m','completed',?,'b',?)",
            (turn, started, manifest),
        )

    assert _context_paths(db, "thread") == {"context/m2.md"}
    assert _context_paths(db, "other") == set()


def test_start_trials_returns_and_records_the_with_half(db, monkeypatch):
    trial = _trial()
    _resolved(monkeypatch, [trial])

    assert start_trials(db, thread_id="thread", node_id=_node("with"), checkpoint_generation_id="i-1") == [{
        "lesson_id": "new", "target": "context/new.md", "guidance": trial.payload, "notice": NOTICE,
    }]
    row = db.execute("SELECT lesson_id,variant,checkpoint_generation_id,turn_id FROM guidance_loads").fetchone()
    assert tuple(row) == ("new", "with", "i-1", None)


def test_start_trials_records_both_halves_once_per_task(db, monkeypatch):
    trial = _trial()
    _resolved(monkeypatch, [trial])
    halves = {variant: _node(variant) for variant in ("with", "without")}

    returned = {
        variant: [start_trials(db, thread_id="thread", node_id=node_id) for _ in range(2)]
        for variant, node_id in halves.items()
    }

    assert [len(run) for run in returned["with"]] == [1, 1]
    assert returned["without"] == [[], []]
    rows = db.execute("SELECT node_id,variant,delivered_sha256 FROM guidance_loads ORDER BY variant").fetchall()
    # The without task follows a with task on the same thread, so it is a crossover.
    assert [tuple(row) for row in rows] == [
        (halves["without"], "crossover", hashlib.sha256(b"").hexdigest()),
        (halves["with"], "with", hashlib.sha256(trial.payload.encode()).hexdigest()),
    ]


def test_a_without_draw_is_recorded_and_a_crossover_needs_the_same_thread(db, monkeypatch):
    _resolved(monkeypatch, [_trial()])
    with_node = _node("with")
    without_node = _node("without")

    start_trials(db, thread_id="thread", node_id=with_node)
    assert start_trials(db, thread_id="other-thread", node_id=without_node) == []
    assert start_trials(db, thread_id="thread", node_id=_node("without", skip=without_node)) == []

    rows = db.execute("SELECT thread_id,variant FROM guidance_loads ORDER BY created_at, id").fetchall()
    assert sorted(tuple(row) for row in rows) == [
        ("other-thread", "without"), ("thread", "crossover"), ("thread", "with"),
    ]
    assert db.execute(
        "SELECT delivered_sha256 FROM guidance_loads WHERE variant='without'"
    ).fetchone()[0] == hashlib.sha256(b"").hexdigest()


def test_an_omitted_trial_gets_one_receipt_with_no_variant(db, monkeypatch):
    omitted = _trial("omitted")
    _resolved(monkeypatch, [], [omitted])

    assert start_trials(db, thread_id="thread", node_id="node") == []
    assert start_trials(db, thread_id="thread", node_id="node") == []
    rows = db.execute(
        "SELECT node_id,variant,lesson_id,raw_sha256,delivered_sha256,partial FROM guidance_loads"
    ).fetchall()
    assert [tuple(row) for row in rows] == [(
        "node", None, "omitted", hashlib.sha256(b"trial source").hexdigest(),
        hashlib.sha256(b"").hexdigest(), 0,
    )]


def test_the_token_cap_omits_trials_past_it_before_the_draw(db, tmp_path, monkeypatch):
    rows = [_row(tmp_path, number, target_file="AGENTS.md", words=120) for number in range(12)]
    _rows(monkeypatch, rows)
    _in_context(monkeypatch, tmp_path)

    admitted, omitted = _resolve(db, "thread")

    spent = sum(_text_tokens(trial.payload, None) for trial in admitted)
    assert 0 < len(admitted) < len(rows)
    assert spent <= START_TOKEN_CAP
    assert len(admitted) + len(omitted) == len(rows)
    for node_id in ("node-a", "node-b"):
        start_trials(db, thread_id="thread", node_id=node_id)
    capped = {
        row["lesson_id"] for row in db.execute("SELECT lesson_id FROM guidance_loads WHERE variant IS NULL")
    }
    assert capped == {trial.lesson_id for trial in omitted}


def test_forty_index_lines_stay_under_the_token_cap_without_rereads(db, tmp_path, monkeypatch):
    rows = [_row(tmp_path, number) for number in range(41)]
    _rows(monkeypatch, rows[:40], rows[40:])
    original_read = Path.read_bytes
    reads: list[Path] = []

    def counted_read(path: Path) -> bytes:
        reads.append(path)
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", counted_read)

    admitted, omitted = _resolve(db, "thread")

    assert 0 < len(admitted) < 40
    assert omitted[-1].lesson_id == "00000000-0000-0000-0000-000000000040"
    assert len(admitted) + len(omitted) == 41
    assert sum(_text_tokens(trial.payload, None) for trial in admitted) <= START_TOKEN_CAP
    assert reads == []


def test_a_failed_draw_write_records_every_trial_as_omitted(db, monkeypatch):
    from core import lesson_delivery

    _resolved(monkeypatch, [_trial()], [_trial("omitted")])
    real_insert = lesson_delivery._insert

    def fail_drawn(tx, **kwargs):
        if kwargs["variant"] is not None:
            raise RuntimeError("receipt unavailable")
        real_insert(tx, **kwargs)

    monkeypatch.setattr(lesson_delivery, "_insert", fail_drawn)

    with pytest.raises(RuntimeError, match="receipt unavailable"):
        start_trials(db, thread_id="thread", node_id=_node("with"))

    rows = db.execute("SELECT lesson_id,variant FROM guidance_loads ORDER BY lesson_id").fetchall()
    assert [tuple(row) for row in rows] == [("new", None), ("omitted", None)]
    assert not db.in_transaction


def test_when_no_receipt_can_be_written_nothing_is_recorded(db, monkeypatch):
    _resolved(monkeypatch, [_trial()])

    def receipt_failure(_data):
        raise RuntimeError("receipt unavailable")

    monkeypatch.setattr("core.guidance_manifest._sha256_bytes", receipt_failure)

    with pytest.raises(RuntimeError, match="receipt unavailable"):
        start_trials(db, thread_id="thread", node_id=_node("with"))

    assert db.execute("SELECT * FROM guidance_loads").fetchall() == []
    assert not db.in_transaction


def test_task_start_draw_commits_before_another_connection_reads_it(tmp_path, monkeypatch):
    db_path = tmp_path / "trial-draw.sqlite"
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    _resolved(monkeypatch, [_trial()])

    start_trials(db, thread_id="thread", node_id=_node("with"))
    assert not db.in_transaction
    second = sqlite3.connect(db_path)
    try:
        row = second.execute("SELECT variant FROM guidance_loads").fetchone()
    finally:
        second.close()
        db.close()

    assert row == ("with",)
