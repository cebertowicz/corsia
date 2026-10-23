"""An in-turn placement retires its entry only when the lesson is verifiably there.

#2479: ``place`` written/already read the destination for the quoted line
before retiring, and ``place`` proposed reaches the worker's queue instead of
a mark nothing drains. #2668: the one P3 name screen.
"""

from __future__ import annotations

import json

import pytest

from tests.test_checkpoint_placement import (  # noqa: F401 -- worker_env is a fixture
    _add,
    _drain,
    _lesson,
    _mark,
    _raw,
    worker_env,
)
from tests.test_lesson_placement import GATE_FIELDS
from tests.test_continuity_staging import _connect, _db, _seed_thread, _tool_db


@pytest.fixture
def placing(tmp_path, monkeypatch):
    path = tmp_path / "place.db"
    db = _db(str(path))
    _seed_thread(db)
    entries = [
        _add(db, "decisions", {**_lesson(text), "rationale": "r"})
        for text in ("one", "two", "three")
    ]
    db.close()
    monkeypatch.setattr("infra.config.WORKSPACE", tmp_path)
    _skill(tmp_path / "skills" / "a" / "SKILL.md", "a")
    return tmp_path, path, _tool_db(path, monkeypatch), entries


def _skill(path, name: str, line: str = "Keep each commit small and focused.") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: Use when committing.\n---\n\n# A\n\n- {line}\n"
    )


def test_place_checks_its_parameters_per_verdict(placing) -> None:
    _, _, continuity, (entry, _, _) = placing
    error = continuity._execution_error

    assert error("place", {"entry_id": entry, "verdict": "written"}) == (
        "Error: place requires destination"
    )
    assert error(
        "place", {"entry_id": entry, "verdict": "written", "destination": "x"},
    ) == "Error: place requires quote, the line in destination that carries the lesson"
    assert error(
        "place", {"entry_id": entry, "verdict": "select", "destination": "x"},
    ) == "Error: place verdict must be one of written, already, proposed, trial"
    assert error("place", {"entry_id": entry, "verdict": "proposed"}) == (
        "Error: verdict proposed requires placement"
    )
    assert error("place", {"entry_id": entry, "verdict": "trial"}) == (
        "Error: verdict trial requires placement"
    )
    assert error(
        "place",
        {"entry_id": entry, "verdict": "proposed", "placement": _raw(),
         "destination": "issue 12"},
    ) == "Error: destination and quote go only with verdict written or already"
    assert error(
        "place",
        {"entry_id": entry, "verdict": "written", "destination": "x",
         "quote": "a long enough quote", "placement": _raw()},
    ) == "Error: place written takes a placement, or a destination and quote, not both"


def test_place_written_retires_only_when_the_file_carries_the_quote(placing) -> None:
    root, path, continuity, (entry, missing, elsewhere) = placing

    written = continuity._run(
        "thread-1", "place",
        {"entry_id": entry, **GATE_FIELDS, "verdict": "written", "destination": "skills/a/SKILL.md",
         "quote": "Keep each commit small and focused."},
    )
    unsaid = continuity._run(
        "thread-1", "place",
        {"entry_id": missing, "verdict": "already", "destination": "skills/a/SKILL.md",
         "quote": "Run the whole suite before pushing."},
    )
    nowhere = continuity._run(
        "thread-1", "place",
        {"entry_id": elsewhere, **GATE_FIELDS, "verdict": "written", "destination": "issue 12",
         "quote": "Keep each commit small and focused."},
    )
    (root.parent / "outside.md").write_text("Keep each commit small and focused.\n")
    outside = continuity._run(
        "thread-1", "place",
        {"entry_id": elsewhere, **GATE_FIELDS, "verdict": "written",
         "destination": str(root.parent / "outside.md"),
         "quote": "Keep each commit small and focused."},
    )

    assert written == (
        f"Entry {entry} marked written at skills/a/SKILL.md and retired."
    )
    assert unsaid.startswith("Error: skills/a/SKILL.md does not contain the quoted line")
    assert nowhere.startswith("Error: destination 'issue 12' is not a file")
    assert outside.startswith("Error: destination")
    db = _connect(str(path))
    done = _mark(db, entry)
    assert (done["state"], done["placement_state"]) == ("retired", "written")
    assert done["placement_revision"] == done["revision"]
    assert done["placement_detail"] == "skills/a/SKILL.md"
    for kept in (missing, elsewhere):
        assert (_mark(db, kept)["state"], _mark(db, kept)["placement_state"]) == (
            "active", None,
        )


def test_a_file_no_later_turn_receives_is_not_a_receipt(placing) -> None:
    root, path, continuity, (entry, lessons, _) = placing
    tray = root / ".threads" / "thread-1"
    quote = "Keep each commit small and focused."
    for name in ("working/note.md", "uploads/notes.md", "context/LESSONS.md"):
        (tray / name).parent.mkdir(parents=True, exist_ok=True)
        (tray / name).write_text(f"- {quote}\n")

    scratch, upload = (
        continuity._run(
            "thread-1", "place",
            {"entry_id": entry, **GATE_FIELDS, "verdict": "written",
             "destination": f".threads/thread-1/{name}", "quote": quote},
        )
        for name in ("working/note.md", "uploads/notes.md")
    )
    bootloader = continuity._run(
        "thread-1", "place",
        {"entry_id": lessons, **GATE_FIELDS, "verdict": "written",
         "destination": ".threads/thread-1/context/LESSONS.md", "quote": quote},
    )

    for refused, name in ((scratch, "working/note.md"), (upload, "uploads/notes.md")):
        assert refused.startswith(
            f"Error: .threads/thread-1/{name} is not delivered to this thread"
        )
    assert bootloader.endswith("and retired.")
    db = _connect(str(path))
    assert (_mark(db, entry)["state"], _mark(db, entry)["placement_state"]) == (
        "active", None,
    )


def test_a_skill_counts_only_when_the_registry_delivers_it(placing) -> None:
    root, path, continuity, (shadowed, invalid, winner) = placing
    quote = "Keep each commit small and focused."
    _skill(root / ".threads" / "thread-1" / "skills" / "a" / "SKILL.md", "a")
    broken = root / "skills" / "b" / "SKILL.md"
    broken.parent.mkdir(parents=True)
    broken.write_text(f"# No frontmatter\n\n- {quote}\n")

    refused = [
        continuity._run(
            "thread-1", "place",
            {"entry_id": entry, "verdict": "already", "destination": name,
             "quote": quote},
        )
        for entry, name in ((shadowed, "skills/a/SKILL.md"),
                            (invalid, "skills/b/SKILL.md"))
    ]
    accepted = continuity._run(
        "thread-1", "place",
        {"entry_id": winner, "verdict": "already",
         "destination": ".threads/thread-1/skills/a/SKILL.md", "quote": quote},
    )

    for result, name in zip(refused, ("skills/a/SKILL.md", "skills/b/SKILL.md")):
        assert result.startswith(f"Error: {name} is not delivered to this thread")
    assert accepted.endswith("and retired.")
    db = _connect(str(path))
    assert [_mark(db, entry)["state"] for entry in (shadowed, invalid, winner)] == [
        "active", "active", "retired",
    ]


def test_an_ancestor_threads_delivered_skill_is_a_receipt(placing) -> None:
    root, path, continuity, (entry, _, _) = placing
    db = _connect(str(path))
    _seed_thread(db, "parent-1")
    db.execute("UPDATE threads SET spawned_by='parent-1' WHERE id='thread-1'")
    db.commit()
    _skill(root / ".threads" / "parent-1" / "skills" / "p" / "SKILL.md", "p",
           "Name the owner before editing a shared file.")

    result = continuity._run(
        "thread-1", "place",
        {"entry_id": entry, "verdict": "already",
         "destination": ".threads/parent-1/skills/p/SKILL.md",
         "quote": "Name the owner before editing a shared file."},
    )

    assert result.endswith("and retired.")
    assert _mark(_connect(str(path)), entry)["state"] == "retired"


def test_place_proposed_queues_the_answer_for_the_worker(placing) -> None:
    from infra.placement_marks import selected_entries

    _, path, continuity, (entry, own, _) = placing

    queued = continuity._run(
        "thread-1", "place",
        {"entry_id": entry, **GATE_FIELDS, "verdict": "proposed", "placement": _raw(q1="project")},
    )
    ours = continuity._run(
        "thread-1", "place",
        {"entry_id": own, **GATE_FIELDS, "verdict": "proposed", "placement": _raw(q1="agent")},
    )

    assert queued == (
        f"Entry {entry} queued; CORSIA files the proposal and marks it proposed."
    )
    assert ours.startswith("Error: verdict proposed is for a project or everywhere")
    db = _connect(str(path))
    mark = _mark(db, entry)
    assert (mark["state"], mark["placement_state"]) == ("active", "selected")
    detail = json.loads(mark["placement_detail"])
    assert detail == {
        "checkpoint": None,
        "answer": {"placement": _raw(q1="project"), "room": None},
    }
    assert [row["entry_id"] for row in selected_entries(db)] == [entry]
    assert _mark(db, own)["placement_state"] is None


def test_a_proposal_naming_someone_is_refused_while_it_can_be_rephrased(
    tmp_path, monkeypatch,
) -> None:
    """The worker would settle it ``p3-verbatim``; staging says what to drop."""
    path = tmp_path / "named.db"
    db = _db(str(path))
    _seed_thread(db)
    entry = _add(db, "decisions", {
        **_lesson("Reviews go through Claude Code first."), "rationale": "r",
    })
    db.close()
    monkeypatch.setattr("infra.config.WORKSPACE", tmp_path)
    continuity = _tool_db(path, monkeypatch)

    named, rephrased = (
        continuity._run(
            "thread-1", "place",
            {"entry_id": entry, **GATE_FIELDS, "verdict": "proposed",
             "placement": _raw(q1="project", text=text)},
        )
        for text in (
            "Send Claude reviews ahead of the rest.",
            "Send reviews through the coding harness first.",
        )
    )

    assert named.startswith(
        "Error: a proposal is published, and its text may not name a person "
        "or copy the lesson: it names Claude."
    )
    assert rephrased == (
        f"Entry {entry} queued; CORSIA files the proposal and marks it proposed."
    )


async def test_an_in_turn_proposal_is_filed_by_the_worker(worker_env) -> None:
    from core.placement_proposal import ProposalResult
    from tools.continuity import _live_operation

    worker, db, workspace, _, _, monkeypatch = worker_env
    source = workspace / "note.md"
    source.write_text("why\n")
    entry = _add(db, "decisions", {
        **_lesson("prefer small commits", [{"kind": "file", "reference": str(source)}]),
        "rationale": "r",
    })
    rule = "Keep every commit small and reviewable."
    _live_operation(db, "thread-1", "place", {
        "entry_id": entry, **GATE_FIELDS, "verdict": "proposed",
        "placement": _raw(q1="project", text=rule),
    })
    db.commit()
    filed: list[dict] = []

    def file_issue(**kwargs) -> ProposalResult:
        filed.append(kwargs)
        return ProposalResult("proposed", "proposed", "https://x/1")

    monkeypatch.setattr(
        "core.placement_proposal.backlog_target",
        lambda db, project_id: ("lane:context", "owner/repo"),
    )
    monkeypatch.setattr("core.placement_proposal.file_issue", file_issue)

    await _drain(worker)

    assert [issue["rule"] for issue in filed] == [rule]
    mark = _mark(db, entry)
    assert (mark["state"], mark["placement_state"], mark["placement_detail"]) == (
        "active", "proposed", "https://x/1",
    )


@pytest.mark.parametrize(
    ("candidate", "names", "refused"),
    [
        ("double charging every Stripe invoice", [], True),
        ("morgan asked about the invoice", ["morgan"], True),
        ("Morgan's batch failed overnight", ["morgan"], True),
        ("Ask morgan, then retry.", ["morgan"], True),
        ("Whitfield mentioned the invoice", [], True),
        ("Dana Whitfield mentioned the invoice", [], True),
        ("Ask Dana’s team before billing.", [], True),
        ("Check invoices for duplicate charges before billing.", ["morgan"], False),
        ("Use GitHub issues for intake.", [], False),
        ("Review every invoice twice.", [], False),
    ],
)
def test_p3_name_screen_catches_every_form_of_a_name(candidate, names, refused) -> None:
    from core.placement_proposal import p3_violation

    source = "Morgan said Dana Whitfield was double charging every Stripe invoice"
    assert p3_violation(candidate, source, names) is refused


@pytest.mark.parametrize(
    ("source", "candidate", "refused"),
    [
        ("The CORSIA reviewer flags a duplicated predicate.",
         "The reviewer wants one shared check.", False),
        ("When Codex is out of capacity, route to Terra.",
         "When a provider lacks capacity, fall back to another.", False),
        ("Prefer Small Commits", "Prefer changes a reviewer can read at once.", False),
        ("Yesterday Dana Whitfield approved the rollout.",
         "Whitfield signs off on rollouts.", True),
        ("Only merge after Dana Whitfield approves.",
         "Dana approves merges.", True),
        ("Dana Whitfield approved the rollout.",
         "Whitfield signs off on rollouts.", True),
        ("Ask Dana Whitfield before merging.",
         "Ask the release owner before merging.", False),
        ("Ask Dana Whitfield before merging.", "Check with Whitfield first.", True),
        ("Before merging, ask Robin\nReviewers want one push per head",
         "Reviewers expect a single push per change.", False),
        ("Reviews go through Claude Code first.", "Claude reviews come first.", True),
    ],
)
def test_p3_name_screen_ignores_capitals_that_grammar_forced(
    source, candidate, refused,
) -> None:
    """Openers and title case are not names; a mid-sentence run still is."""
    from core.placement_proposal import p3_violation

    assert p3_violation(candidate, source, []) is refused


def test_entry_fields_do_not_run_into_each_other() -> None:
    from core.placement_outcomes import entry_text
    from core.placement_proposal import p3_violation

    entry = {"body": {"statement": "Before merging, ask Robin",
                      "rationale": "Reviewers want one push per head"}}

    assert not p3_violation(
        "Reviewers expect a single push per change.", entry_text(entry), [],
    )


def test_schema_53_returns_every_proposed_mark_to_review(tmp_path) -> None:
    """In-turn proposals before #2479 kept free text and nothing filed them."""
    from infra.database_migrations import migrate_to_current
    from infra.placement_marks import set_mark

    db = _db(str(tmp_path / "marks.db"))
    _seed_thread(db)
    stuck, filed, written = (
        _add(db, "decisions", {**_lesson(text), "rationale": "r"})
        for text in ("one", "two", "three")
    )
    for entry, state, detail in (
        (stuck, "proposed", "issue 12"),
        (filed, "proposed", "https://x/issues/1"),
        (written, "written", "skills/a/SKILL.md"),
    ):
        set_mark(db, thread_id="thread-1", history_epoch=0, entry_id=entry,
                 state=state, revision=_mark(db, entry)["revision"],
                 detail=detail)
    db.execute("PRAGMA user_version=52")
    db.commit()

    result = migrate_to_current(db, 53)

    assert (result.from_version, result.to_version) == (52, 53)
    assert [_mark(db, entry)["placement_state"] for entry in (stuck, filed)] == [
        None, None,
    ]
    assert _mark(db, written)["placement_state"] == "written"
