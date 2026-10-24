"""Making room over the cap: checked operations, one commit, or nothing (A6)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from core.placement_decide import InvalidDecision, append_lesson, lesson_count
from core.placement_room import Room, apply, validate_operations
from tests.test_checkpoint_placement import _drain, _lesson, _log, _mark, _raw, worker_env

__all__ = ["worker_env"]

_DAY = "2026-09-24"

_FULL = (
    "# R\n\n## Mission\n\nKeep context honest.\n\n## Lessons\n"
    "- 2026-09-01: Run the smallest affected tests before pushing a branch.\n"
    "- 2026-09-02: Never merge your own pull request without green checks.\n"
    "- 2026-09-03: Old deploy history from the August migration week.\n"
    "- 2026-09-04: Report counts by field, never raw database rows.\n"
    "- 2026-09-05: Always run the smallest affected tests before a push.\n"
)


def _ops(*items: dict, base: str = _FULL) -> Room:
    rows = [
        {"end": None, "text": None, "basis": None, "path": None, "quote": None, **item}
        for item in items
    ]
    return Room(base=base, operations=validate_operations({"operations": rows}))


def _apply(room: Room, tmp_path: Path, overflow: Path | None = None):
    return apply(
        room, kind="context", current=room.base,
        add_lesson=lambda text: append_lesson(text, "Small commits.", _DAY)[0],
        workspace=tmp_path, destination=tmp_path / "RESPONSIBILITY.md",
        overflow=overflow,
        overflow_current=overflow.read_text() if overflow and overflow.exists() else None,
        day=_DAY,
    )


def test_validate_refuses_unknown_operations() -> None:
    with pytest.raises(InvalidDecision):
        validate_operations({"operations": [{"op": "trim", "start": 1}]})
    with pytest.raises(InvalidDecision):
        validate_operations({"operations": [{"op": "fold", "start": "3"}]})
    with pytest.raises(InvalidDecision):
        validate_operations({"operations": [
            {"op": "fold", "start": 8, "text": "a"}, {"op": "fold", "start": 9, "text": "b"},
        ]})


def test_remove_superseded_by_a_quoted_line_of_the_same_file(tmp_path) -> None:
    room = _ops({
        "op": "remove", "start": 8, "basis": "superseded-by",
        "quote": "Always run the smallest affected tests before a push.",
    })

    text, overflow, operations = _apply(room, tmp_path)

    assert "Run the smallest affected tests before pushing" not in text
    assert "Small commits." in text and lesson_count(text) == 5
    assert overflow is None
    assert operations[0].describe() == "remove line 8 (superseded-by a line in this file)"


def test_remove_needs_a_basis_the_backend_can_check(tmp_path) -> None:
    unquoted = _ops({"op": "remove", "start": 8, "basis": "superseded-by", "quote": "nothing like it"})
    self_quote = _ops({
        "op": "remove", "start": 8, "basis": "superseded-by",
        "quote": "Run the smallest affected tests before pushing a branch.",
    })
    no_basis = _ops({"op": "remove", "start": 8})
    (tmp_path / "RUNBOOK.md").write_text("Counts only, by field.\n")
    wrong_file = _ops({
        "op": "remove", "start": 11, "basis": "handled-by", "path": "RUNBOOK.md",
        "quote": "Report counts by field, never raw database rows.",
    })

    for room in (unquoted, self_quote, no_basis, wrong_file):
        assert _apply(room, tmp_path) is None


def test_remove_handled_by_an_existing_file(tmp_path) -> None:
    (tmp_path / "RUNBOOK.md").write_text("Report counts by field, never raw database rows.\n")
    room = _ops({
        "op": "remove", "start": 11, "basis": "handled-by", "path": "RUNBOOK.md",
        "quote": "Report counts by field, never raw database rows.",
    })

    text, _, operations = _apply(room, tmp_path)

    assert "raw database rows" not in text
    assert operations[0].describe() == "remove line 11 (handled-by RUNBOOK.md)"


def test_handled_by_cannot_leave_the_workspace(tmp_path) -> None:
    room = _ops({
        "op": "remove", "start": 11, "basis": "handled-by", "path": "../../etc/hosts",
        "quote": "Report counts by field, never raw database rows.",
    })

    assert _apply(room, tmp_path) is None


def test_protected_sections_and_changed_files_are_never_touched(tmp_path) -> None:
    mission = _ops({"op": "fold", "start": 5, "text": "Keep context honest and small."})
    stale = Room(base=_FULL + "extra\n", operations=_ops({"op": "fold", "start": 8, "text": "x"}).operations)

    assert _apply(mission, tmp_path) is None
    assert apply(
        stale, kind="context", current=_FULL, add_lesson=lambda text: text,
        workspace=tmp_path, destination=tmp_path / "R.md", overflow=None,
        overflow_current=None, day=_DAY,
    ) is None


def test_a_subsection_of_a_required_section_stays_protected(tmp_path) -> None:
    nested = _FULL.replace(
        "## Lessons\n",
        "## Authority\n\n### Can decide\n\n- Merge own clean PRs to dev.\n\n## Lessons\n",
    )
    item = nested.split("\n").index("- Merge own clean PRs to dev.") + 1

    assert _apply(_ops({"op": "fold", "start": item, "text": "x y"}, base=nested), tmp_path) is None


def test_fold_targets_only_a_list_item_never_a_heading(tmp_path) -> None:
    heading = _FULL.split("\n").index("## Lessons") + 1

    assert _apply(_ops({"op": "fold", "start": heading, "text": "Lessons now"}), tmp_path) is None


def test_superseded_by_must_quote_a_line_that_stays(tmp_path) -> None:
    overflow = tmp_path / "shared" / "LESSONS-HISTORY.md"
    quote = "Always run the smallest affected tests before a push."
    moved_away = _ops(
        {"op": "remove", "start": 8, "basis": "superseded-by", "quote": quote},
        {"op": "move", "start": 12, "end": 12},
    )
    folded_away = _ops(
        {"op": "remove", "start": 8, "basis": "superseded-by", "quote": quote},
        {"op": "fold", "start": 12, "text": "Commit small."},
    )

    assert _apply(moved_away, tmp_path, overflow) is None
    assert _apply(folded_away, tmp_path) is None


def test_fold_carries_the_lesson_without_appending_it(tmp_path) -> None:
    room = _ops({"op": "fold", "start": 8, "text": "Run the smallest affected tests; commit small."})

    text, _, _ = _apply(room, tmp_path)

    assert "- 2026-09-01: Run the smallest affected tests; commit small." in text
    assert "Small commits." not in text and lesson_count(text) == 5


def test_at_cap_fold_remedy_keeps_the_lesson_count_within_cap(tmp_path) -> None:
    from core.placement_decide import over_cap

    assert lesson_count(_FULL) == 5
    assert over_cap("context", append_lesson(_FULL, "Small commits.", _DAY)[0])
    text, _, _ = _apply(_ops({
        "op": "fold", "start": 8, "text": "Run the smallest affected tests; Small commits.",
    }), tmp_path)

    assert not over_cap("context", text)
    assert lesson_count(text) == lesson_count(_FULL)


def test_move_copies_lines_verbatim_and_leaves_a_pointer(tmp_path) -> None:
    overflow = tmp_path / "shared" / "LESSONS-HISTORY.md"
    room = _ops({"op": "move", "start": 10, "end": 10})

    text, moved, operations = _apply(room, tmp_path, overflow)

    assert "Old deploy history" not in text
    assert "\nMoved to shared/LESSONS-HISTORY.md on 2026-09-24.\n" in text
    assert lesson_count(text) == 5
    assert "- 2026-09-03: Old deploy history from the August migration week.\n" in moved
    assert operations[0].describe() == "move line 10"


def test_move_without_an_overflow_file_or_still_over_cap_is_no_room(tmp_path) -> None:
    assert _apply(_ops({"op": "move", "start": 10}), tmp_path) is None
    assert _apply(_ops(), tmp_path) is None


# -- the worker (A6) -----------------------------------------------------------


def _seed_full(workspace: Path, tray: Path) -> Path:
    responsibility = tray / "context" / "RESPONSIBILITY.md"
    responsibility.parent.mkdir(parents=True)
    responsibility.write_text(_FULL)
    subprocess.run(["git", "-C", str(workspace), "add", "."], check=True)
    subprocess.run(["git", "-C", str(workspace), "commit", "-qm", "seed"], check=True)
    return responsibility


def _room(operations: list[dict], base: str = _FULL) -> dict:
    """The fork's stored make-room answer against ``base``."""
    rows = [
        {"end": None, "text": None, "basis": None, "path": None, "quote": None, **op}
        for op in operations
    ]
    return {"base": base, "operations": rows}


_PLACEMENT = _raw(text="Commit in small steps.", rationale="agent rule")


def _reasons(monkeypatch) -> list[tuple[str, str]]:
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "core.placement_outcomes.log_outcome",
        lambda **kwargs: seen.append((kwargs["outcome"], kwargs["reason"])),
    )
    return seen


async def test_worker_makes_room_in_one_commit(worker_env) -> None:
    worker, db, workspace, tray, select, monkeypatch = worker_env
    responsibility = _seed_full(workspace, tray)
    entry = select(
        {**_lesson("small commits"), "rationale": "r"}, placement=_PLACEMENT,
        room=_room([{
            "op": "remove", "start": 8, "basis": "superseded-by",
            "quote": "Always run the smallest affected tests before a push.",
        }]),
    )

    await _drain(worker)

    text = responsibility.read_text()
    assert "Commit in small steps." in text and "before pushing a branch" not in text
    assert "## Mission\n\nKeep context honest.\n" in text
    body = subprocess.run(
        ["git", "-C", str(workspace), "log", "-1", "--format=%B"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert "Room: remove line 8 (superseded-by a line in this file)" in body
    assert _log(workspace)[0].startswith("placement: written")
    assert _mark(db, entry)["placement_state"] == "written"


async def test_worker_without_checkable_room_writes_nothing(worker_env) -> None:
    worker, db, workspace, tray, select, _ = worker_env
    responsibility = _seed_full(workspace, tray)
    entry = select(
        {**_lesson("small commits"), "rationale": "r"}, placement=_PLACEMENT,
        room=_room([{"op": "remove", "start": 8}]),
    )

    await _drain(worker)

    assert responsibility.read_text() == _FULL
    assert _log(workspace)[0] == "seed"
    marked = _mark(db, entry)
    assert (marked["state"], marked["placement_state"]) == ("active", None)


async def test_worker_unplaces_when_the_file_changed_since_the_fork_made_room(
    worker_env,
) -> None:
    worker, db, workspace, tray, select, monkeypatch = worker_env
    responsibility = _seed_full(workspace, tray)
    entry = select(
        {**_lesson("small commits"), "rationale": "r"}, placement=_PLACEMENT,
        room=_room([{
            "op": "remove", "start": 8, "basis": "superseded-by",
            "quote": "Always run the smallest affected tests before a push.",
        }]),
    )
    changed = _FULL + "- 2026-09-06: A concurrent edit.\n"
    responsibility.write_text(changed)
    reasons = _reasons(monkeypatch)

    await _drain(worker)

    assert reasons == [("unplaced", "destination-changed")]
    assert responsibility.read_text() == changed
    assert _log(workspace)[0] == "seed"
    assert _mark(db, entry)["placement_state"] is None


@pytest.mark.parametrize(
    ("fold_text", "state", "reason"),
    [
        ("Run the smallest affected tests. Commit in small steps.", "written", "written"),
        ("Run the smallest affected tests; commit small.", None, "fold-lost-lesson"),
    ],
)
async def test_worker_writes_a_fold_only_when_it_carries_the_lesson(
    worker_env, fold_text, state, reason,
) -> None:
    worker, db, workspace, tray, select, monkeypatch = worker_env
    responsibility = _seed_full(workspace, tray)
    entry = select(
        {**_lesson("small commits"), "rationale": "r"}, placement=_PLACEMENT,
        room=_room([{"op": "fold", "start": 8, "text": fold_text}]),
    )
    reasons = _reasons(monkeypatch)

    await _drain(worker)

    assert reasons[-1][1] == reason
    assert _mark(db, entry)["placement_state"] == state
    if state is None:
        assert responsibility.read_text() == _FULL
        assert _log(workspace)[0] == "seed"
    else:
        assert f"- 2026-09-01: {fold_text}\n" in responsibility.read_text()


async def test_worker_moves_lines_to_a_new_overflow_file_in_one_commit(worker_env) -> None:
    from core.placement_decide import ThreadPlace

    worker, db, workspace, tray, select, monkeypatch = worker_env
    responsibility = _seed_full(workspace, tray)
    reference = workspace / "shared" / "lane"
    reference.mkdir(parents=True)
    monkeypatch.setattr(
        worker, "_thread_place",
        lambda thread_id: ThreadPlace(thread_id, "Lane", None, True, tray, reference),
    )
    entry = select(
        {**_lesson("small commits"), "rationale": "r"}, placement=_PLACEMENT,
        room=_room([{"op": "move", "start": 10, "end": 10}]),
    )

    await _drain(worker)

    history = reference / "LESSONS-HISTORY.md"
    assert "Old deploy history" not in responsibility.read_text()
    assert "Moved to shared/lane/LESSONS-HISTORY.md" in responsibility.read_text()
    assert "- 2026-09-03: Old deploy history from the August migration week.\n" in history.read_text()
    files = subprocess.run(
        ["git", "-C", str(workspace), "show", "--name-only", "--format=%s", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.split("\n")
    assert files[0].startswith("placement: written")
    assert {".threads/thread-1/context/RESPONSIBILITY.md", "shared/lane/LESSONS-HISTORY.md"} <= set(files)
    assert _mark(db, entry)["placement_state"] == "written"
