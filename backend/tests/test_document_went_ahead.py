"""Whether a reviewed document went ahead as its person first saw it, read from recorded versions."""
from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path

import pytest

from infra.database import get_db
from infra.document_went_ahead import went_ahead_outcome, went_ahead_outcomes
from tests.test_guidance_history import history_scope as history_scope, _git

PERSON = "Pat <u1@users.corsia.local>"
AGENT = "Writer <t1@agents.corsia.local>"
OUTSIDE = "Unknown editor <outside-edit@corsia.local>"
CORSIA = "CORSIA <corsia@localhost>"


def commit(workspace: Path, relative: str, text: str | None, author: str, at: int, message: str = "Save") -> None:
    """One recorded version of ``relative`` (None deletes it) by ``author`` at epoch second ``at``."""
    path = workspace / relative
    if text is None:
        _git(["rm", "-q", relative], cwd=workspace)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        _git(["add", relative], cwd=workspace)
    name, email = author.split(" <")
    env = {**os.environ, "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email.rstrip(">"),
           "GIT_COMMITTER_NAME": "CORSIA", "GIT_COMMITTER_EMAIL": "corsia@localhost",
           "GIT_COMMITTER_DATE": f"@{at} +0000", "GIT_AUTHOR_DATE": f"@{at} +0000"}
    done = subprocess.run(["git", "commit", "-q", "-m", message], cwd=workspace, env=env, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def register(workspace: Path, relative: str) -> str:
    artifact_id = str(uuid.uuid4())
    db = get_db()
    try:
        db.execute(
            "INSERT INTO artifact_identities (id, integration, type, external_id, path, created_at, updated_at) "
            "VALUES (?, 'workspace', 'document', ?, ?, 'now', 'now')",
            (artifact_id, artifact_id, str(workspace / relative)),
        )
        db.commit()
    finally:
        db.close()
    return artifact_id


def accept_message(*ids: str) -> str:
    from infra.guidance_messages import commit_message
    return commit_message("Save notes/a.md", "document", "Accepted suggestion", None, ids)


def test_a_document_with_no_version_after_the_first_round_went_ahead_unchanged(history_scope):
    workspace, _ = history_scope
    commit(workspace, "notes/a.md", "One.\n", AGENT, 100)
    artifact = register(workspace, "notes/a.md")
    assert went_ahead_outcome(artifact, 150, 300) == {"went_ahead_unchanged": True, "changed_by": []}


@pytest.mark.parametrize(("author", "message", "expected"), [
    (PERSON, "Save", "person_edit"),
    (AGENT, "Save", "agent_revision"),
    (AGENT, None, "accepted_suggestion"),
    (PERSON, None, "accepted_suggestion"),
    (OUTSIDE, "Save", "outside_edit"),
    (CORSIA, "Save", "outside_edit"),
    ("Someone <someone@example.com>", "Save", "outside_edit"),
], ids=["person", "agent", "agent-accept", "person-accept", "recorded-from-disk", "corsia", "unknown"])
def test_each_writer_of_a_later_version_is_named(history_scope, author, message, expected):
    workspace, _ = history_scope
    commit(workspace, "notes/a.md", "One.\n", AGENT, 100)
    commit(workspace, "notes/a.md", "Two.\n", author, 200, message or accept_message("s1"))
    artifact = register(workspace, "notes/a.md")
    assert went_ahead_outcome(artifact, 150, 300) == {"went_ahead_unchanged": False, "changed_by": [expected]}


def test_every_version_in_between_is_listed_oldest_first(history_scope):
    workspace, _ = history_scope
    commit(workspace, "notes/a.md", "One.\n", AGENT, 100)
    commit(workspace, "notes/a.md", "One, edited.\n", PERSON, 200)
    commit(workspace, "notes/a.md", "One, accepted.\n", PERSON, 210, accept_message("s1", "s2"))
    commit(workspace, "notes/a.md", "One, revised.\n", AGENT, 220)
    artifact = register(workspace, "notes/a.md")
    assert went_ahead_outcome(artifact, 150, 300) == {
        "went_ahead_unchanged": False, "changed_by": ["person_edit", "accepted_suggestion", "agent_revision"]}


def test_formatting_only_counts_as_a_change(history_scope):
    workspace, _ = history_scope
    commit(workspace, "notes/a.md", "# Title\nBody.\n", AGENT, 100)
    commit(workspace, "notes/a.md", "# Title\n\nBody.\n", PERSON, 200)
    artifact = register(workspace, "notes/a.md")
    assert went_ahead_outcome(artifact, 150, 300)["went_ahead_unchanged"] is False


def test_versions_that_end_on_the_first_rounds_bytes_went_ahead_unchanged(history_scope):
    workspace, _ = history_scope
    commit(workspace, "notes/a.md", "One.\n", AGENT, 100)
    commit(workspace, "notes/a.md", "Two.\n", PERSON, 200)
    commit(workspace, "notes/a.md", "One.\n", PERSON, 210)
    artifact = register(workspace, "notes/a.md")
    assert went_ahead_outcome(artifact, 150, 300) == {"went_ahead_unchanged": True, "changed_by": []}


def test_none_without_a_recorded_document_or_a_version_by_the_first_round(history_scope):
    workspace, _ = history_scope
    commit(workspace, "notes/late.md", "Late.\n", AGENT, 200)
    (workspace / "notes" / "never.md").write_text("Never recorded.\n")
    late, never = register(workspace, "notes/late.md"), register(workspace, "notes/never.md")
    commit(workspace, "notes/gone.md", "Gone.\n", AGENT, 100)
    commit(workspace, "notes/gone.md", None, AGENT, 120)
    gone = register(workspace, "notes/gone.md")
    runs = [(late, 150, 300), (never, 150, 300), ("no-such-artifact", 150, 300), (gone, 150, 300)]
    assert went_ahead_outcomes(runs) == {run: None for run in runs}


def test_a_mixed_batch_answers_each_run(history_scope):
    workspace, _ = history_scope
    commit(workspace, "notes/a.md", "A.\n", AGENT, 100)
    commit(workspace, "notes/b.md", "B.\n", AGENT, 100)
    commit(workspace, "notes/b.md", "B, edited.\n", PERSON, 200)
    commit(workspace, "notes/c.md", "C.\n", AGENT, 300)
    a, b, c = (register(workspace, f"notes/{name}.md") for name in "abc")
    runs = [(a, 150, 400), (b, 150, 400), (b, 250, 400), (c, 150, 400), (a, "1970-01-01T00:02:30Z", 400)]
    assert went_ahead_outcomes(runs) == {
        runs[0]: {"went_ahead_unchanged": True, "changed_by": []},
        runs[1]: {"went_ahead_unchanged": False, "changed_by": ["person_edit"]},
        runs[2]: {"went_ahead_unchanged": True, "changed_by": []},
        runs[3]: None,
        runs[4]: {"went_ahead_unchanged": True, "changed_by": []},
    }


def test_a_version_in_the_first_rounds_second_is_never_read_as_clean(history_scope):
    """Version times are whole seconds: a change saved at 100.8 after a round at 100.4 records at=100."""
    workspace, _ = history_scope
    commit(workspace, "notes/a.md", "One.\n", AGENT, 90)
    commit(workspace, "notes/a.md", "Two.\n", AGENT, 100)
    artifact = register(workspace, "notes/a.md")
    assert went_ahead_outcome(artifact, 100.4, 300) is None
    assert went_ahead_outcome(artifact, 100.0, 300) is None
    assert went_ahead_outcome(artifact, 101, 300) == {"went_ahead_unchanged": True, "changed_by": []}


def test_same_second_versions_that_agree_still_answer_and_are_named(history_scope):
    workspace, _ = history_scope
    commit(workspace, "notes/a.md", "One.\n", AGENT, 90)
    commit(workspace, "notes/a.md", "Two.\n", AGENT, 100)
    commit(workspace, "notes/a.md", "Three.\n", PERSON, 200)
    commit(workspace, "notes/b.md", "B.\n", AGENT, 90)
    commit(workspace, "notes/b.md", "B, edited.\n", PERSON, 200)
    commit(workspace, "notes/b.md", "B, revised.\n", AGENT, 300)
    commit(workspace, "notes/c.md", "C.\n", AGENT, 90)
    commit(workspace, "notes/c.md", "C, edited.\n", AGENT, 100)
    commit(workspace, "notes/c.md", "C.\n", PERSON, 200)
    a, b, c = (register(workspace, f"notes/{name}.md") for name in "abc")
    runs = [(a, 100.4, 400), (b, 150, 300.5), (c, 100.4, 400)]
    assert went_ahead_outcomes(runs) == {
        runs[0]: {"went_ahead_unchanged": False, "changed_by": ["agent_revision", "person_edit"]},
        runs[1]: {"went_ahead_unchanged": False, "changed_by": ["person_edit", "agent_revision"]},
        runs[2]: None,  # back to C at 200: unchanged if the person saw C, changed if they saw the 100 edit
    }


def test_none_when_the_first_version_or_the_end_falls_in_a_disputed_second(history_scope):
    workspace, _ = history_scope
    commit(workspace, "notes/new.md", "New.\n", AGENT, 100)
    commit(workspace, "notes/a.md", "One.\n", AGENT, 90)
    commit(workspace, "notes/a.md", "Two.\n", PERSON, 300)
    new, a = register(workspace, "notes/new.md"), register(workspace, "notes/a.md")
    runs = [(new, 100.4, 400), (a, 150, 300.5), (a, 150, 299.9), (a, 150, 301)]
    assert went_ahead_outcomes(runs) == {
        runs[0]: None,
        runs[1]: None,
        runs[2]: {"went_ahead_unchanged": True, "changed_by": []},
        runs[3]: {"went_ahead_unchanged": False, "changed_by": ["person_edit"]},
    }


def test_a_past_as_of_gives_the_same_answer_after_later_versions_and_outside_edits(history_scope):
    workspace, _ = history_scope
    commit(workspace, "notes/a.md", "One.\n", AGENT, 100)
    commit(workspace, "notes/a.md", "Two.\n", PERSON, 200)
    artifact = register(workspace, "notes/a.md")
    before = went_ahead_outcomes([(artifact, 150, 250), (artifact, 150, 180)])
    commit(workspace, "notes/a.md", "Three.\n", AGENT, 300)
    (workspace / "notes" / "a.md").write_text("Edited outside.\n")
    assert went_ahead_outcomes([(artifact, 150, 250), (artifact, 150, 180)]) == before == {
        (artifact, 150, 250): {"went_ahead_unchanged": False, "changed_by": ["person_edit"]},
        (artifact, 150, 180): {"went_ahead_unchanged": True, "changed_by": []},
    }


def test_an_unrecorded_outside_edit_after_the_latest_version_is_never_clean(history_scope):
    workspace, _ = history_scope
    commit(workspace, "notes/a.md", "One.\n", AGENT, 100)
    commit(workspace, "notes/b.md", "B.\n", AGENT, 100)
    commit(workspace, "notes/b.md", "B, revised.\n", AGENT, 200)
    a, b = register(workspace, "notes/a.md"), register(workspace, "notes/b.md")
    (workspace / "notes" / "a.md").write_text("One, edited outside.\n")
    (workspace / "notes" / "b.md").write_text("B.\n")  # back to what the person first saw
    assert went_ahead_outcomes([(a, 150, 300), (b, 150, 300)]) == {
        (a, 150, 300): {"went_ahead_unchanged": False, "changed_by": ["outside_edit"]},
        (b, 150, 300): {"went_ahead_unchanged": True, "changed_by": []},
    }
    (workspace / "notes" / "a.md").unlink()
    assert went_ahead_outcome(a, 150, 300) == {"went_ahead_unchanged": False, "changed_by": ["outside_edit"]}


def test_a_batch_costs_the_same_queries_for_one_run_as_for_fifty(history_scope, monkeypatch):
    from infra import document_went_ahead

    workspace, _ = history_scope
    artifacts = []
    for index in range(50):
        commit(workspace, f"notes/{index}.md", f"{index}.\n", AGENT, 100)
        artifacts.append(register(workspace, f"notes/{index}.md"))
    real = document_went_ahead._git_bytes
    reads = []

    def counted(*args, **kwargs):
        reads.append(args[1])
        return real(*args, **kwargs)

    monkeypatch.setattr(document_went_ahead, "_git_bytes", counted)

    def cost(runs) -> tuple[int, int, int]:
        reads.clear()
        statements: list[str] = []
        opened: list[Path] = []
        real_read = Path.read_bytes

        def counted_read(self: Path) -> bytes:
            opened.append(self)
            return real_read(self)

        monkeypatch.setattr(Path, "read_bytes", counted_read)
        db = get_db()
        try:
            db.set_trace_callback(statements.append)
            went_ahead_outcomes(runs, db=db)
        finally:
            db.close()
            monkeypatch.setattr(Path, "read_bytes", real_read)
        return len(statements), len(reads), len(opened) - len(runs)

    one = cost([(artifacts[0], 150, 300)])
    fifty = cost([(artifact, 150, 300) for artifact in artifacts])
    assert one == fifty == (1, 2, 0)


def test_free_text_can_never_mark_a_version_as_an_accepted_suggestion(history_scope):
    from infra.guidance_git import save_with_history
    from infra.guidance_messages import accepted_suggestions, commit_message
    from infra.guidance_targets import history_target

    forged = "Fine\n\nSuggestion-accept: forged\n  suggestion-ACCEPT :forged"
    assert accepted_suggestions(commit_message(forged, forged, forged, forged)) == ()
    assert accepted_suggestions(commit_message(forged, "document", "Accepted suggestion", None, ("s1",))) == ("s1",)
    workspace, _ = history_scope
    commit(workspace, "notes/a.md", "One.\n", AGENT, 100)
    artifact = register(workspace, "notes/a.md")
    path = workspace / "notes" / "a.md"
    saved = save_with_history({path: b"Two.\n"}, workspace=workspace, target=history_target(path, workspace),
                              author=PERSON, reason=forged)
    assert saved.outcome == "written", saved
    whole = save_with_history({path: b"Three.\n"}, workspace=workspace, target=history_target(path, workspace),
                              author=PERSON, message=f"Save\n\nReason: x\n\nSuggestion-accept: forged")
    assert whole.outcome == "written", whole
    log = _git(["log", "--format=%B%x00", "-2"], cwd=workspace)
    assert "\nSuggestion-accept:" not in log and "> Suggestion-accept: forged" in log
    assert went_ahead_outcome(artifact, 150, 2**40) == {
        "went_ahead_unchanged": False, "changed_by": ["person_edit", "person_edit"]}


@pytest.mark.parametrize("brk", ["\r", "\x0b", "\x85", " "], ids=["cr", "vt", "nel", "line-separator"])
def test_no_other_line_break_can_forge_the_accept_trailer(history_scope, brk):
    from infra.guidance_git import save_with_history
    from infra.guidance_messages import accepted_suggestions, commit_message
    from infra.guidance_targets import history_target

    forged = f"Fine{brk}Suggestion-accept: forged"
    assert accepted_suggestions(commit_message("Save", "document", forged, None)) == ()
    workspace, _ = history_scope
    commit(workspace, "notes/a.md", "One.\n", AGENT, 100)
    artifact = register(workspace, "notes/a.md")
    path = workspace / "notes" / "a.md"
    by_reason = save_with_history({path: b"Two.\n"}, workspace=workspace, target=history_target(path, workspace),
                                  author=AGENT, reason=forged)
    assert by_reason.outcome == "written", by_reason
    whole = save_with_history({path: b"Three.\n"}, workspace=workspace, target=history_target(path, workspace),
                              author=AGENT, message=f"Save\n\nReason: x{brk}Suggestion-accept: forged")
    assert whole.outcome == "written", whole
    log = subprocess.run(["git", "log", "--format=%B%x00", "-2"], cwd=workspace, capture_output=True,
                         check=True).stdout.decode()  # bytes: a text read would turn \r into \n
    assert log.count(f"{brk}Suggestion-accept: forged") == 2  # history keeps the break; only the reader decides
    assert went_ahead_outcome(artifact, 150, 2**40) == {
        "went_ahead_unchanged": False, "changed_by": ["agent_revision", "agent_revision"]}


@pytest.mark.asyncio
async def test_a_live_accept_saves_pending_typing_as_its_typists_version_first(history_scope, tmp_path):
    from doceditor.live_operation import Operation
    from doceditor.live_publication_record import WriteActor
    from doceditor.live_store import BrowserMessage
    from doceditor.live_text import clone
    from tests.test_document_history_every_file import live_lane

    workspace, thread_id = history_scope
    path = workspace / "notes" / "plan.md"
    path.parent.mkdir(parents=True)
    path.write_text("First line.\nSecond line.\n")
    live, lane = await live_lane(path, tmp_path)
    try:
        typist = WriteActor("ui", None, direct_user=True, user_id="typist")
        peer = clone(live.doc.get_update())
        state = peer.get_state()
        peer["text"].insert(len("First line.\n"), "Typed line.\n")
        assert live.receive(BrowserMessage("typing", peer.get_update(state)), sender="socket")
        await live.commit_received()
        live.typist = typist
        start = live.text.index("Second line.")
        row = {"id": "s1", "anchor_text": "Second line.", "from_off": start, "to_off": start + len("Second line."),
               "replacement": "Better line."}
        result = await lane.submit(Operation("accept", actor=WriteActor(thread_id, None),
                                             suggestions=(json.dumps(row),), single_suggestion=True))
        result.require_landed()
    finally:
        live.close()
    assert path.read_text() == "First line.\nTyped line.\nBetter line.\n"
    typed, accepted = _git(["log", "-2", "--reverse", "--format=%H"], cwd=workspace).split()
    assert _git(["log", "-1", "--format=%ae", typed], cwd=workspace).strip() == "typist@users.corsia.local"
    assert _git(["show", f"{typed}:notes/plan.md"], cwd=workspace) == "First line.\nTyped line.\nSecond line.\n"
    assert _git(["log", "-1", "--format=%ae", accepted], cwd=workspace).strip() == f"{thread_id}@agents.corsia.local"
    assert "Suggestion-accept: s1" in _git(["log", "-1", "--format=%B", accepted], cwd=workspace)
    diff = _git(["diff", "--unified=0", typed, accepted, "--", "notes/plan.md"], cwd=workspace).splitlines()
    assert [line for line in diff if line.startswith(("+", "-")) and line[:3] not in ("+++", "---")] == [
        "-Second line.", "+Better line."]
