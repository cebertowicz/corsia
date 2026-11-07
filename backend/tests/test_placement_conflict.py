"""A guidance file changed by someone else while a placement was deciding.

Both writers read a lesson's destination before they build what they mean to write, so each one's
compare-and-swap expectation has to be the bytes *that* read saw. An expectation taken later matches
whatever the other writer left, and the placement then overwrites it with text built from the older
read. Each test changes the file on disk immediately after the placement's own read and requires the
write to come back stale with that change still on disk.

A file saved with CRLF endings is where this matters most: its text read is not the bytes the save
compares, so an expectation cannot be rebuilt from the text.
"""

from __future__ import annotations

from tests.test_checkpoint_placement import _change_after_the_first_read, _repo

STALE = "deferred-destination-changed"
HELD = "# Captured\r\n\r\nHeld evidence.\r\n"
BY_SOMEONE_ELSE = b"# Captured\r\n\r\nSomeone else wrote this line.\r\n"

CONTEXT = "# Notes\n\n## Lessons\n- 2026-09-01: Run the smallest affected tests before pushing.\n"
CONTEXT_CHANGED = (CONTEXT + "- 2026-09-06: Someone else wrote this lesson.\n").encode()


def test_a_capture_changed_after_its_read_is_refused_as_stale(tmp_path, monkeypatch) -> None:
    """The capture's expectation is the bytes its text was read from: a change made after that read is
    stale, and the other writer's line survives."""
    from core import lesson_files, lesson_outcomes, placement_outcomes
    from core.lesson_admission import Admission
    from core.lesson_trials import Trial
    from core.placement_decide import Decision, ThreadPlace
    from infra import config

    workspace = _repo(tmp_path)
    monkeypatch.setattr(config, "WORKSPACE", str(workspace))
    tray = workspace / ".threads" / "owner"
    path = lesson_files.capture_path(tray, "cap-1")
    path.parent.mkdir(parents=True)
    path.write_bytes(HELD.encode())
    _change_after_the_first_read(monkeypatch, path, BY_SOMEONE_ELSE)

    deferred: list[str] = []
    settled: list[tuple[str, str]] = []
    monkeypatch.setattr(placement_outcomes, "defer", lambda _e, reason, **_k: deferred.append(reason))
    monkeypatch.setattr(
        placement_outcomes, "settle", lambda _e, outcome, reason, **_k: settled.append((outcome, reason))
    )
    # The review step is downstream of the expectation under test and needs the artifact registry.
    monkeypatch.setattr(lesson_outcomes, "_review", lambda *_a, **_k: "node-1")
    decision = Decision(
        q1="thread", q2="task_start", duplicate_of=None, text="Check the output twice before sending it.",
        skill_name="demo", skill_description="Use when checking output.", reference_file=None,
        rationale="A cheap check catches the miss.", trial=Trial(kind="instruction", review_after="2026-12-01"),
    )

    lesson_outcomes._capture(
        {"thread_id": "owner", "entry_id": 1, "revision": 1, "history_epoch": 1, "body": {}},
        ThreadPlace(
            thread_id="owner", title="Owner", project_id=None, agent=True, tray=tray, reference_dir=None,
        ),
        decision,
        Admission(lesson_id="cap-1", capture_id="cap-1", hold="missing-derivation", case_metadata={}),
    )

    assert deferred == [STALE], f"the change was overwritten instead of refused: {settled}"
    assert path.read_bytes() == BY_SOMEONE_ELSE


def test_a_context_lesson_changed_after_its_read_is_refused_as_stale(tmp_path, monkeypatch) -> None:
    """``commit_plan`` binds its expectation to the same read its new text came from, so a lesson someone
    else added in the meantime is refused rather than dropped."""
    from core import placement_outcomes
    from core.placement_decide import Decision, Destination, ThreadPlace
    from infra import config

    workspace = _repo(tmp_path)
    monkeypatch.setattr(config, "WORKSPACE", str(workspace))
    path = workspace / "notes.md"
    path.write_text(CONTEXT, encoding="utf-8")
    _change_after_the_first_read(monkeypatch, path, CONTEXT_CHANGED)

    deferred: list[str] = []
    settled: list[tuple[str, str]] = []
    monkeypatch.setattr(placement_outcomes, "defer", lambda _e, reason, **_k: deferred.append(reason))
    monkeypatch.setattr(
        placement_outcomes, "settle", lambda _e, outcome, reason, **_k: settled.append((outcome, reason))
    )
    decision = Decision(
        q1="thread", q2="task_start", duplicate_of=None, text="Commit in small steps.",
        skill_name=None, skill_description=None, reference_file=None,
        rationale="Smaller commits revert easier.",
    )

    placement_outcomes.write(
        {"thread_id": "owner", "entry_id": 1, "revision": 1, "history_epoch": 1, "body": {}},
        ThreadPlace(
            thread_id="owner", title="Owner", project_id=None, agent=True,
            tray=workspace / ".threads" / "owner", reference_dir=None,
        ),
        decision, Destination(kind="context", path=path),
    )

    assert deferred == [STALE], f"the change was overwritten instead of refused: {settled}"
    assert settled == []
    assert path.read_bytes() == CONTEXT_CHANGED
