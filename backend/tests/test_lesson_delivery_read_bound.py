"""Real trial delivery reuses one shared validation scan, including cap omissions."""
from __future__ import annotations

from collections import Counter
import hashlib
from pathlib import Path
import sqlite3

import pytest

from core import lesson_files, skill_scanning
from core.lesson_delivery import draw, read_delivery, start_trials
from core.lesson_trials import Trial
from infra.schema import SCHEMA_SQL
from plans import lesson_reviews
from tests.project_ref_helpers import select_workspace


@pytest.fixture
def delivery_scope(tmp_path, monkeypatch):
    select_workspace(monkeypatch, tmp_path)
    # Use an empty System scope so package projection/repair reads do not enter
    # this delivery regression; the resolver and every candidate scan remain real.
    system_root = tmp_path / "system"
    (system_root / "skills").mkdir(parents=True)
    monkeypatch.setattr("core.skill_resolution.system_skills_root", lambda _workspace: system_root)
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    db.execute(
        """INSERT INTO threads (id,user_id,model,created_at,updated_at)
           VALUES ('owner','user','test/model','t','t')""",
    )
    db.execute(
        """INSERT INTO artifact_identities
           (id,integration,type,external_id,title,state,created_at,updated_at)
           VALUES ('artifact','workspace','skill','workspace://artifact','artifact','active','t','t')""",
    )
    db.commit()
    yield db, tmp_path
    db.close()


def _install_trials(db, workspace, count, *, is_new):
    trials = []
    for number in range(count):
        name = f"trial-{number:02}"
        target = f"context/{name}.md"
        text = lesson_files.render_trial(
            lesson_id=f"00000000-0000-0000-0000-{number:012d}", owner="owner", name=name,
            description="Use when checking trial delivery reads.", text=f"Procedure for {name}.",
            trial=Trial(kind="hypothesis", review_after="2030-01-01T00:00:00+00:00"),
            source_refs=[{"kind": "turn", "reference": "turn-1"}],
            target_file="new" if is_new else target,
            new_target_path=target if is_new else None,
        )
        trial = lesson_files.read_trial(text)
        review_id, _created = lesson_reviews.ensure_lesson_review(
            db, "owner", lesson_id=trial.lesson.id, behavioral_sha256=trial.behavioral_sha256,
            artifact_id="artifact", title=f"Review {name}",
            acceptance="Retire, promote or extend the trial with evidence.",
            review_after=trial.lesson.review_after,
        )
        text = lesson_files.with_review_ref(text, review_id)
        path = workspace / ".threads" / "owner" / "skills" / name / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text(text, encoding="utf-8")
        trials.append((path, lesson_files.read_trial(text), text.encode(), workspace / target))
    db.commit()
    return trials


def _count_skill_reads(monkeypatch):
    validation_calls = Counter()
    validation_reads = Counter()
    delivery_reads = Counter()
    validating = False
    original_validate = skill_scanning.validate_skill_file
    original_open = Path.open

    def validate(path):
        nonlocal validating
        validation_calls[path] += 1
        validating = True
        try:
            return original_validate(path)
        finally:
            validating = False

    def opened(path, mode="r", *args, **kwargs):
        if path.name == "SKILL.md" and "r" in mode:
            (validation_reads if validating else delivery_reads)[path] += 1
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(skill_scanning, "validate_skill_file", validate)
    monkeypatch.setattr(Path, "open", opened)
    return validation_calls, validation_reads, delivery_reads


@pytest.mark.parametrize("count", [2, 41, 45])
@pytest.mark.parametrize("seam", ["start", "read"])
def test_delivery_uses_one_validation_scan_and_no_omitted_rereads(
    delivery_scope, monkeypatch, count, seam,
):
    db, workspace = delivery_scope
    trials = _install_trials(db, workspace, count, is_new=seam == "start")
    last_trial = trials[-1][1]
    node_id = next(
        f"node-{number}" for number in range(1000)
        if draw(last_trial.lesson.id, last_trial.behavioral_sha256, f"node-{number}") == "with"
    )
    monkeypatch.setattr("core.lesson_delivery._current_task", lambda *_args: node_id)
    validation_calls, validation_reads, delivery_reads = _count_skill_reads(monkeypatch)

    if seam == "start":
        delivered = start_trials(db, thread_id="owner", node_id=node_id)
        expected = [
            trial.lesson.id for _path, trial, _data, _target in trials[:40]
            if draw(trial.lesson.id, trial.behavioral_sha256, node_id) == "with"
        ]
        assert [item["lesson_id"] for item in delivered] == expected
        rows = db.execute("SELECT * FROM guidance_loads ORDER BY lesson_id").fetchall()
        assert len(rows) == count
        for row, (_path, trial, data, _target) in zip(rows, trials, strict=True):
            omitted = trial.lesson.id not in {item[1].lesson.id for item in trials[:40]}
            assert row["variant"] == (None if omitted else draw(
                trial.lesson.id, trial.behavioral_sha256, node_id,
            ))
            assert row["partial"] == int(omitted)
            assert row["raw_sha256"] == hashlib.sha256(data).hexdigest()
            assert row["delivered_sha256"] == hashlib.sha256(
                b"" if omitted else trial.body.encode(),
            ).hexdigest()
    else:
        result = read_delivery(
            db, thread_id="owner", path=trials[-1][3], rendered_text="baseline",
            checkpoint_generation_id=None,
        )
        if count > 40:
            assert result.text == "baseline"
            assert result.receipts == ()
        else:
            assert result.variant == "with"
            assert last_trial.body.strip() in result.text
            assert result.receipts[0].lesson_id == last_trial.lesson.id
        assert db.execute("SELECT COUNT(*) FROM guidance_loads").fetchone()[0] == 0

    # Count every physical SKILL.md open during the real runtime call. Each candidate
    # is read exactly once by validation; partition, delivery and receipts add none.
    expected_scan = Counter({path: 1 for path, _trial, _data, _target in trials})
    assert validation_calls == expected_scan
    assert validation_reads == expected_scan
    assert delivery_reads == Counter()
