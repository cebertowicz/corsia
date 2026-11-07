"""A document section's writer turn: an agent edit or suggestion in a section a person is changing waits in line."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import pytest

from doceditor.live_runtime import LiveDocuments
from doceditor.live_text import clone
from tests.test_comment_writer_turn import _end, _wake_turn
from tests.test_document_live_transport import Socket
from tests.test_shared_document_ownership import ownership_scope as ownership_scope, _write

TEXT = "Intro line\n## Terms\nold terms\n## Scope\nold scope\n"
#: Two sections headed "## Notes".
REPEATED = "Intro line\n## Notes\nfirst note\n## Terms\nold terms\n## Notes\nsecond note\n"


@pytest.fixture(autouse=True)
def _fresh_turns():
    from doceditor import writer_turns
    from ws import section_turn

    writer_turns.reset()
    section_turn.reset()
    yield
    section_turn.reset()
    writer_turns.reset()


@pytest.fixture
def driven(monkeypatch):
    from plans import public

    calls: list[str] = []

    async def drive_threads(thread_ids):
        calls.extend(thread_ids)

    monkeypatch.setattr(public, "drive_threads", drive_threads)
    return calls


@pytest.fixture
def steps(monkeypatch) -> list[dict]:
    """The writer-line product events recorded, each a step's properties."""
    from doceditor import live_events

    recorded: list[dict] = []
    real = live_events.emit

    def emit(action, **properties):
        if action == "comment_writer_line":
            recorded.append(properties)
        else:
            real(action, **properties)

    monkeypatch.setattr(live_events, "emit", emit)
    return recorded


def _outcomes(steps: list[dict]) -> list[str]:
    for step in steps:
        assert (step["root_id"], step["line"]) == (None, "section")
        assert "Terms" not in json.dumps(step)  # a heading is document text, never a product event
    return [step["outcome"] for step in steps]


class Page:
    """A person's editor on the document, through the real frame handler."""

    def __init__(self, scope, user):
        self.scope, self.user, self.socket = scope, user, Socket()
        self.identity: dict = {}
        self.peer = None
        self.sent = 0

    async def open(self, path: Path) -> None:
        from ws import document_control

        await document_control.handle_frame(self.socket, self.user, {
            "type": "doc_open", "path": str(path), "project_id": "project-alpha", "thread_id": self.scope["owner"]})
        opened = await self.socket.next("doc_opened")
        self.identity = {key: opened[key] for key in ("artifact_id", "store_id")}
        self.peer = clone(base64.b64decode(opened["update"]))

    async def type_after(self, anchor: str, typed: str, live) -> None:
        from ws import document_control

        self.peer.apply_update(live.doc.get_update(self.peer.get_state()))
        before = self.peer.get_state()
        self.peer["text"].insert(str(self.peer["text"]).index(anchor) + len(anchor), typed)
        self.sent += 1
        await document_control.handle_frame(self.socket, self.user, {
            **self.identity, "type": "doc_update", "message_id": f"m{id(self)}-{self.sent}",
            "update": base64.b64encode(self.peer.get_update(before)).decode()})

    async def close(self) -> None:
        from ws import document_control

        await document_control.handle_frame(self.socket, self.user, {**self.identity, "type": "doc_close"})


async def _until(condition, seconds: float = 10) -> None:
    """Wait for ``condition`` itself, never a fixed time; ``seconds`` only bounds a hang."""
    async with asyncio.timeout(seconds):
        while not condition():
            await asyncio.sleep(0.005)


async def _settled() -> None:
    from ws import section_turn

    for _ in range(5):
        await asyncio.sleep(0)
        if section_turn._tasks:
            await asyncio.gather(*tuple(section_turn._tasks))


def _rows(sql: str, *args) -> list[dict]:
    from infra.database import get_db

    db = get_db()
    try:
        return [dict(row) for row in db.execute(sql, args).fetchall()]
    finally:
        db.close()


def _woken(thread_id: str) -> bool:
    from doceditor import writer_line
    from infra.database import get_db

    db = get_db()
    try:
        return writer_line.section_woken(db, thread_id)
    finally:
        db.close()


def _line(artifact_id: str, heading: str) -> list[dict]:
    from doceditor import writer_turns

    return _rows("SELECT * FROM doc_comment_writer_line WHERE root_id=? ORDER BY saved_at,id",
                 writer_turns.section_key(artifact_id, heading))


def _wake(row: dict) -> dict:
    [message] = _rows("SELECT * FROM messages WHERE id=?", row["wake_message_id"])
    return message


@pytest.fixture
async def doc(request, ownership_scope, tmp_path, monkeypatch):
    """The open document: ``TEXT`` unless the test parametrizes ``doc`` with another."""
    from doceditor import live_runtime
    from infra import app_loop, send_target
    from tools import filesystem_edit, filesystem_rw
    from tools.filesystem_gate import AuthorizedPath
    from ws import document_control

    scope = ownership_scope
    app_loop.capture(asyncio.get_running_loop())

    async def authorized(*, path, **kwargs):
        return AuthorizedPath(Path(path).resolve(), reviewed=False)

    monkeypatch.setattr(filesystem_rw, "_resolve_path_for_action", authorized)
    monkeypatch.setattr(filesystem_edit, "_resolve_path_for_action", authorized)
    monkeypatch.setattr(send_target, "model_admission", lambda model, **_kwargs: "run")  # the agent's model is ready
    _write(scope, "sections.md", getattr(request, "param", TEXT))
    manager = LiveDocuments(tmp_path / "live", workspace=scope["workspace"],
                            db_path=Path(scope["db"].execute("PRAGMA database_list").fetchone()[2]))
    monkeypatch.setattr(live_runtime, "_manager", manager)
    for name in ("_subscriptions", "_tasks", "_openings", "_commits"):
        monkeypatch.setattr(document_control, name, {})
    user_id = scope["db"].execute("SELECT user_id FROM threads WHERE id=?", (scope["owner"],)).fetchone()[0]
    page = Page(scope, {"id": user_id, "name": "Pat"})
    path = scope["category"] / "sections.md"
    await page.open(path)
    entry = manager.documents[page.identity["artifact_id"]]
    context = {"_thread_id": scope["owner"], "_thread_project_id": "project-alpha"}
    try:
        yield scope, page, entry, path, context
    finally:
        await document_control.disconnect(page.socket)
        await manager.close()
        app_loop.clear()


async def _edit(path: Path, context: dict, old: str, new: str) -> str:
    from tools.filesystem_edit import EditFileTool

    return await EditFileTool().execute(path=str(path), old_string=old, new_string=new, **context)


async def _read(scope, path: Path, context: dict) -> None:
    from tools.filesystem_rw import ReadFileTool

    await ReadFileTool(workspace=scope["workspace"]).execute(str(path), **context)


def _queued(result: str) -> dict:
    receipt = json.loads(result)
    assert receipt["outcome"] == "queued"
    return receipt


@pytest.mark.asyncio
async def test_an_edit_in_a_section_a_person_is_changing_waits_and_posts_on_the_same_call(doc, driven, steps):
    from doceditor import writer_turns

    scope, page, entry, path, context = doc
    artifact_id = page.identity["artifact_id"]
    await _read(scope, path, context)
    await page.type_after("## Terms\n", "new ", entry.live)
    assert writer_turns.person_holding(writer_turns.section_key(artifact_id, "## Terms")).name == "Pat"

    saved = _queued(await _edit(path, context, "old terms", "agent terms"))
    assert saved["result"].startswith("Saved, 1st in line behind Pat.")
    assert "agent terms" not in entry.live.text
    [row] = _line(artifact_id, "## Terms")
    assert row["state"] == "waiting"

    landed = await _edit(path, context, "old scope", "agent scope")
    assert json.loads(landed).get("outcome") != "queued"
    assert "agent scope" in entry.live.text

    await page.type_after("new ", "and ", entry.live)
    await page.type_after("agent scope", " (person)", entry.live)
    await _settled()
    [row] = _line(artifact_id, "## Terms")
    assert row["state"] == "woken" and driven == [scope["owner"]]
    wake = _wake(row)
    assert 'The writer turn is yours on the section "## Terms"' in wake["content"]
    assert "-new old terms" in wake["content"] and "+new and old terms" in wake["content"]
    assert 'the section "## Scope": 1 edit' in wake["content"]
    assert "make the same call again" in wake["content"]
    assert json.loads(wake["metadata"])["kind"] == "section_writer_turn"
    assert "comment_id" not in json.loads(wake["metadata"])

    posted = await _edit(path, context, "old terms", "agent terms")
    assert json.loads(posted).get("outcome") != "queued"
    assert "new and agent terms" in entry.live.text
    assert _line(artifact_id, "## Terms") == []
    assert _outcomes(steps) == ["queued", "woken", "posted_as_is"]


@pytest.mark.asyncio
@pytest.mark.parametrize("leaving", ["idle", "close", "disconnect"])
async def test_the_section_frees_when_the_person_goes_idle_closes_or_drops(doc, driven, monkeypatch, leaving):
    from doceditor import writer_turns
    from ws import document_control, section_turn

    scope, page, entry, path, context = doc
    artifact_id = page.identity["artifact_id"]
    await _read(scope, path, context)
    await page.type_after("## Terms\n", "new ", entry.live)
    _queued(await _edit(path, context, "old terms", "agent terms"))

    if leaving == "idle":
        # The timer is armed only now, by the person's next change, and the test waits on the release
        # itself: a timer armed before the agent's edit could expire first on a loaded machine.
        monkeypatch.setattr(section_turn, "IDLE_SECONDS", 0.01)
        await page.type_after("new ", "again ", entry.live)
        await _until(lambda: writer_turns.person_holding(writer_turns.section_key(artifact_id, "## Terms")) is None)
    elif leaving == "close":
        await page.close()
    else:
        await document_control.disconnect(page.socket)
    await _settled()
    [row] = _line(artifact_id, "## Terms")
    assert row["state"] == "woken" and driven == [scope["owner"]]
    assert "Nothing changed in the document's other sections since you last read it." in _wake(row)["content"]

    posted = await _edit(path, context, "old terms", "agent terms")
    assert not posted.startswith("Error"), posted
    assert _line(artifact_id, "## Terms") == []


@pytest.mark.asyncio
async def test_after_a_restart_the_wake_says_counts_are_unknown_and_a_write_to_the_closed_document_answers_it(
    doc, driven, monkeypatch, steps,
):
    from doceditor import agent_comment_writes, live_runtime, writer_turns
    from ws import section_turn

    scope, page, entry, path, context = doc
    artifact_id = page.identity["artifact_id"]
    await _read(scope, path, context)
    await page.type_after("## Terms\n", "new ", entry.live)
    _queued(await _edit(path, context, "old terms", "agent terms"))
    manager = live_runtime._manager
    # A restart: every turn is free and nothing is open.
    section_turn.reset()
    writer_turns.reset()
    monkeypatch.setattr(live_runtime, "_manager", None)

    assert agent_comment_writes.recover_line() == [scope["owner"]]
    [row] = _line(artifact_id, "## Terms")
    wake = _wake(row)["content"]
    assert row["state"] == "woken"
    assert "Changes in the document's other sections since you last read it are unknown" in wake

    posted = await _edit(path, context, "old terms", "better terms")
    assert not posted.startswith("Error"), posted
    assert "better terms" in path.read_text()
    assert _line(artifact_id, "## Terms") == []
    assert _outcomes(steps)[-1] == "rewritten"
    assert not _woken(scope["owner"])
    monkeypatch.setattr(live_runtime, "_manager", manager)


@pytest.mark.asyncio
async def test_a_woken_agents_next_call_answers_its_wake_even_when_that_call_is_refused(doc, driven, steps):
    """The row settles as the call is let through; the refusal is the call's own result, never a drop note.

    The call touches the section (its text is in both sections, so it is refused as ambiguous).
    """
    from doceditor import writer_turns

    scope, page, entry, path, context = doc
    artifact_id = page.identity["artifact_id"]
    await page.type_after("## Terms\n", "new ", entry.live)
    _queued(await _edit(path, context, "old terms", "agent terms"))
    await page.close()
    await _settled()
    assert [row["state"] for row in _line(artifact_id, "## Terms")] == ["woken"]

    refused = await _edit(path, context, "old ", "anything ")

    assert "agent terms" not in entry.live.text and "anything" not in entry.live.text
    assert refused.startswith("Warning"), refused
    assert _line(artifact_id, "## Terms") == []
    assert _outcomes(steps) == ["queued", "woken", "rewritten"]
    assert not _woken(scope["owner"])
    assert _rows("""SELECT 1 FROM context_change_journal WHERE object_kind='thread_context' AND object_id=?
                    AND summary LIKE '%dropped out of line%'""", scope["owner"]) == []


@pytest.mark.asyncio
async def test_a_suggestion_in_a_held_section_waits_while_a_plain_comment_lands(doc, driven):
    from tools.documents import DocCommentCreateTool

    scope, page, entry, path, context = doc
    artifact_id = page.identity["artifact_id"]
    await page.type_after("## Terms\n", "new ", entry.live)

    saved = await DocCommentCreateTool().execute(
        body="Tighter", artifact_id=artifact_id, quote="old terms", replacement="terms", **context)
    assert saved.startswith("Saved, 1st in line behind Pat.")
    [row] = _line(artifact_id, "## Terms")
    assert json.loads(row["ops"])[0]["kind"] == "suggest"

    plain = json.loads(await DocCommentCreateTool().execute(
        body="Is this right?", artifact_id=artifact_id, quote="old terms", **context))
    assert plain["comment"]["replacement"] is None
    suggestions = _rows("SELECT id FROM doc_comments WHERE artifact_id=? AND replacement IS NOT NULL", artifact_id)
    assert suggestions == []


@pytest.mark.asyncio
async def test_a_saved_edit_drops_with_the_agent_told_when_the_document_is_deleted(doc, driven, steps):
    scope, page, entry, path, context = doc
    artifact_id = page.identity["artifact_id"]
    await page.type_after("## Terms\n", "new ", entry.live)
    _queued(await _edit(path, context, "old terms", "agent terms"))
    scope["db"].execute("UPDATE artifact_identities SET state='tombstone' WHERE id=?", (artifact_id,))
    scope["db"].commit()
    await page.close()
    await _settled()
    assert _line(artifact_id, "## Terms") == []
    assert _outcomes(steps) == ["queued", "dropped"]
    [note] = _rows("""SELECT summary FROM context_change_journal WHERE object_kind='thread_context' AND object_id=?
                      AND summary LIKE '%dropped out of line%'""", scope["owner"])
    assert 'Your saved write on the section "## Terms"' in note["summary"]
    assert '"old terms" -> "agent terms"' in note["summary"]


@pytest.mark.asyncio
async def test_a_call_answers_only_the_rows_of_the_sections_it_touches(doc, driven, steps):
    from doceditor import writer_turns

    scope, page, entry, path, context = doc
    artifact_id = page.identity["artifact_id"]
    other = Page(scope, page.user)
    await other.open(path)
    await page.type_after("## Terms\n", "new ", entry.live)
    await other.type_after("## Scope\n", "fresh ", entry.live)
    _queued(await _edit(path, context, "old terms", "agent terms"))
    _queued(await _edit(path, context, "old scope", "agent scope"))
    await page.close()
    await other.close()
    await _settled()
    assert [row["state"] for row in _line(artifact_id, "## Terms") + _line(artifact_id, "## Scope")] == ["woken"] * 2

    await _edit(path, context, "old scope", "agent scope")  # posts the Scope row as is, touching only Scope

    assert _line(artifact_id, "## Scope") == []
    assert [row["state"] for row in _line(artifact_id, "## Terms")] == ["woken"]
    assert _woken(scope["owner"])
    assert _outcomes(steps).count("posted_as_is") == 1 and "rewritten" not in _outcomes(steps)

    await _edit(path, context, "old terms", "better terms")  # now the Terms row, rewritten
    assert _line(artifact_id, "## Terms") == []
    assert not _woken(scope["owner"])
    assert _outcomes(steps)[-1] == "rewritten"
    await other.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("doc", [REPEATED], indirect=True)
async def test_headings_with_the_same_text_are_separate_sections(doc, driven):
    from doceditor import doc_sections, writer_turns

    scope, page, entry, path, context = doc
    artifact_id = page.identity["artifact_id"]
    first, second = "## Notes", "## Notes\n2"
    assert [name for _, name in doc_sections.headings(REPEATED)] == ["", first, "## Terms", second]
    assert doc_sections.section_at(REPEATED, REPEATED.index("second")) == second
    assert doc_sections.sections_in(REPEATED, REPEATED.index("first"), REPEATED.index("second")) == [
        first, "## Terms", second]
    assert doc_sections.section_text(REPEATED, second) == "## Notes\nsecond note\n"
    assert doc_sections.section_text(REPEATED, first) == "## Notes\nfirst note\n"

    await _read(scope, path, context)
    await page.type_after("second ", "big ", entry.live)
    assert writer_turns.person_holding(writer_turns.section_key(artifact_id, second)).name == "Pat"
    assert writer_turns.person_holding(writer_turns.section_key(artifact_id, first)) is None

    landed = await _edit(path, context, "first note", "agent note")  # the person is in the other section
    assert json.loads(landed).get("outcome") != "queued"
    assert "agent note" in entry.live.text
    _queued(await _edit(path, context, "big note", "agent big note"))
    [row] = _line(artifact_id, second)
    assert json.loads(row["ops"])[0]["base"] == {second: "## Notes\nsecond big note\n"}

    await page.type_after("big ", "very ", entry.live)
    await page.close()
    await _settled()
    wake = _wake(_line(artifact_id, second)[0])["content"]
    assert "-second big note" in wake and "+second big very note" in wake
    assert "Nothing changed in it since you saved your write" not in wake


@pytest.mark.asyncio
async def test_a_landed_edit_is_a_read_only_when_it_met_no_change_the_agent_had_not_read(doc, driven):
    scope, page, entry, path, context = doc
    thread = scope["owner"]
    await _read(scope, path, context)
    await _edit(path, context, "old terms", "agent terms")  # nothing changed since the read: a read
    assert entry.live.sections.since(thread) == {}

    await page.type_after("## Scope\n", "new ", entry.live)
    assert entry.live.sections.since(thread) == {"## Scope": 1}
    await _edit(path, context, "agent terms", "agent terms two")  # lands beside a change the agent has not read
    assert entry.live.sections.since(thread) == {"## Scope": 1}

    await _read(scope, path, context)
    assert entry.live.sections.since(thread) == {}


def test_a_write_that_merged_with_live_edits_or_has_no_read_to_extend_is_not_a_read(monkeypatch):
    from doceditor import section_log

    clock = iter(f"2026-11-06T00:00:{second:02d}" for second in range(60))
    monkeypatch.setattr(section_log, "now", lambda: next(clock))
    log = section_log.SectionLog()
    log.wrote("agent", merged=False)
    assert log.since("agent") is None
    log.read("agent")
    log.wrote("agent", merged=True)
    log.changed(["## A"], "other")
    log.wrote("agent", merged=False)
    assert log.since("agent") == {"## A": 1}


def test_replace_all_scans_the_headings_once(monkeypatch):
    from doceditor import doc_sections, section_writes

    text = "start\n" + "".join(f"## S{number}\n" + "word " * 50 + "\n" for number in range(40))
    scans = []
    real = doc_sections._marks
    monkeypatch.setattr(doc_sections, "_marks", lambda *args: scans.append(1) or real(*args))
    touched = section_writes.edit_sections(text, [{"old_string": "word", "new_string": "w", "replace_all": True}])
    assert touched == [f"## S{number}" for number in range(40)]
    assert len(scans) == 1


def test_the_last_edit_keeps_no_offset_runs(monkeypatch):
    """Only a later edit reads the runs, so a lone replace_all holds nothing per occurrence."""
    from doceditor import section_writes

    text = "## A\n" + "e" * 1000 + "\n## B\ne\n"
    monkeypatch.setattr(section_writes, "_moved", lambda *args: pytest.fail("the last edit built runs"))
    edits = [{"old_string": "e", "new_string": "E", "replace_all": True}]
    assert section_writes.edit_sections(text, edits) == ["## A", "## B"]


def test_edits_find_their_sections_as_they_are_applied_one_after_another():
    """Each edit matches the text the edits before it left, as ``match_edits`` applies them."""
    from doceditor import section_writes

    text = "## A\nfoo\n## B\nfoo\n"
    edits = [{"old_string": "foo", "new_string": "bar"}, {"old_string": "foo", "new_string": "baz"}]
    assert section_writes.edit_sections(text, edits) == ["## A", "## B"]



def test_a_renamed_heading_does_not_rename_the_sections_later_edits_fall_in():
    """Sections are named in the document the turns belong to: renaming the first of two ``## A`` headings
    leaves the second ``## A\n2``, the section a later edit in it must hold."""
    from doceditor import section_writes

    text = "## A\none\n## A\ntwo\n"
    edits = [{"old_string": "## A\none", "new_string": "## Z\none"}, {"old_string": "two", "new_string": "2"}]
    assert section_writes.edit_sections(text, edits) == ["## A", "## A\n2"]


def test_an_edit_matching_text_an_earlier_edit_wrote_holds_the_section_that_text_replaced():
    from doceditor import section_writes

    text = "## A\nfoo\n## B\nbar\n"
    edits = [{"old_string": "bar", "new_string": "qux"}, {"old_string": "qux", "new_string": "q"}]
    assert section_writes.edit_sections(text, edits) == ["## B"]

@pytest.mark.asyncio
async def test_a_woken_section_row_dropped_at_turn_end_leaves_the_agent_unwoken(doc, driven, steps):
    from doceditor import writer_turns

    scope, page, entry, path, context = doc
    artifact_id = page.identity["artifact_id"]
    await page.type_after("## Terms\n", "new ", entry.live)
    _queued(await _edit(path, context, "old terms", "agent terms"))
    await page.close()
    await _settled()
    [row] = _line(artifact_id, "## Terms")
    assert row["state"] == "woken" and _woken(scope["owner"])

    _end(scope["owner"], _wake_turn(scope["owner"], row), "completed")  # the turn ends without answering

    assert _line(artifact_id, "## Terms") == []
    assert _outcomes(steps)[-1] == "dropped"
    assert not _woken(scope["owner"])


@pytest.mark.asyncio
async def test_a_sweep_that_ends_an_agents_last_section_row_and_wakes_it_on_another_keeps_it_woken(doc, driven, steps):
    """Ending one woken row in the same sweep that wakes the agent on another leaves the agent woken."""
    import uuid

    from doceditor import writer_line, writer_turns
    from infra.database import get_db, write_transaction

    scope, page, entry, path, context = doc
    artifact_id = page.identity["artifact_id"]
    await page.type_after("## Terms\n", "new ", entry.live)
    _queued(await _edit(path, context, "old terms", "agent terms"))
    await page.close()
    await _settled()
    [terms] = _line(artifact_id, "## Terms")
    assert terms["state"] == "woken" and _woken(scope["owner"])

    db = get_db()
    try:
        with write_transaction(db, scope="test_section_sweep") as connection:
            # The Terms wake could not reach the agent, so the sweep ends that row; the agent also waits in Scope's line.
            connection.execute("UPDATE messages SET delivery_state='failed' WHERE id=?", (terms["wake_message_id"],))
            connection.execute(
                """INSERT INTO doc_comment_writer_line(id,root_id,artifact_id,agent_thread_id,user_id,ops,saved_at)
                   VALUES (?,?,?,?,?,'[]','2026-11-07T00:00:00+00:00')""",
                (uuid.uuid4().hex, writer_turns.section_key(artifact_id, "## Scope"), artifact_id, scope["owner"],
                 terms["user_id"]))
        with write_transaction(db, scope="writer_line_sweep") as connection:
            writer_line.serve_waiting(connection)
    finally:
        db.close()

    assert _line(artifact_id, "## Terms") == []
    [scope_row] = _line(artifact_id, "## Scope")
    assert scope_row["state"] == "woken"
    assert _woken(scope["owner"])


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["edit", "suggest"])
async def test_a_write_to_the_closed_document_that_cannot_settle_its_wake_is_refused(doc, driven, monkeypatch, kind):
    """Nothing lands that the line does not know answered the wake: the write is refused, the wake stays
    unanswered, and the note when it drops at turn end says the turn ended without answering it."""
    from doceditor import live_runtime, section_writes, writer_line
    from infra.database import get_db, write_transaction
    from tools.documents import DocCommentCreateTool

    scope, page, entry, path, context = doc
    artifact_id = page.identity["artifact_id"]

    async def write() -> str:
        if kind == "edit":
            return await _edit(path, context, "old terms", "agent terms")
        return await DocCommentCreateTool().execute(
            body="Tighter", artifact_id=artifact_id, quote="old terms", replacement="terms", **context)

    await page.type_after("## Terms\n", "new ", entry.live)
    await write()
    await page.close()
    await _settled()
    [row] = _line(artifact_id, "## Terms")
    assert row["state"] == "woken"
    before = path.read_text()
    comments = len(_rows("SELECT id FROM doc_comments WHERE artifact_id=?", artifact_id))

    def broken(*_args):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(section_writes, "_settle_closed", broken)
    monkeypatch.setattr(live_runtime, "_manager", None)
    result = await write()
    assert result.startswith("Error") and "database is locked" in result
    assert path.read_text() == before
    assert len(_rows("SELECT id FROM doc_comments WHERE artifact_id=?", artifact_id)) == comments
    [still] = _line(artifact_id, "## Terms")
    assert still["state"] == "woken"

    db = get_db()
    try:
        with write_transaction(db, scope="test_section_turn_end") as connection:
            connection.execute("UPDATE messages SET delivery_state='delivered' WHERE id=?", (still["wake_message_id"],))
            writer_line.settle_turn_end(connection, scope["owner"], None)
    finally:
        db.close()
    assert _line(artifact_id, "## Terms") == []
    [note] = _rows("""SELECT summary FROM context_change_journal WHERE object_kind='thread_context' AND object_id=?
                      AND summary LIKE '%dropped out of line%'""", scope["owner"])
    assert writer_line.TURN_ENDED in note["summary"]
    assert "check the document" not in note["summary"]


@pytest.mark.asyncio
async def test_a_write_to_the_closed_document_whose_text_cannot_be_read_is_refused(doc, driven, monkeypatch):
    """A failed read is a failed settle: the write is refused and the wake stays unanswered."""
    from doceditor import live_runtime, section_writes

    scope, page, entry, path, context = doc
    artifact_id = page.identity["artifact_id"]
    await page.type_after("## Terms\n", "new ", entry.live)
    _queued(await _edit(path, context, "old terms", "agent terms"))
    await page.close()
    await _settled()
    [row] = _line(artifact_id, "## Terms")
    assert row["state"] == "woken"
    before = path.read_text()

    def unreadable(_path):
        raise ValueError("doc too large to anchor against")

    monkeypatch.setattr(section_writes, "read_doc_text", unreadable)
    monkeypatch.setattr(live_runtime, "_manager", None)
    result = await _edit(path, context, "old terms", "agent terms")
    assert result.startswith("Error") and "doc too large" in result
    assert path.read_text() == before
    [still] = _line(artifact_id, "## Terms")
    assert still["state"] == "woken"


@pytest.mark.asyncio
async def test_a_wake_on_one_document_never_refuses_a_write_to_another(doc, driven, monkeypatch):
    """A write to a document the agent has no woken row on cannot answer its wake, so that document's read is
    never needed: a file the settle could not read still takes the write, and the wake stays unanswered."""
    from doceditor import live_runtime, section_writes

    scope, page, entry, path, context = doc
    artifact_id = page.identity["artifact_id"]
    await page.type_after("## Terms\n", "new ", entry.live)
    _queued(await _edit(path, context, "old terms", "agent terms"))
    await page.close()
    await _settled()
    [row] = _line(artifact_id, "## Terms")
    assert row["state"] == "woken"
    _write(scope, "other.md", "## Other\nold other\n")
    other = scope["category"] / "other.md"

    def unreadable(_path):
        raise ValueError("doc too large to anchor against")

    monkeypatch.setattr(section_writes, "read_doc_text", unreadable)
    monkeypatch.setattr(live_runtime, "_manager", None)
    result = await _edit(other, context, "old other", "agent other")
    assert not result.startswith("Error"), result
    assert other.read_text() == "## Other\nagent other\n"
    [still] = _line(artifact_id, "## Terms")
    assert still["state"] == "woken"


@pytest.mark.asyncio
async def test_a_write_whose_settle_committed_lands_even_when_passing_the_line_on_fails(doc, driven, monkeypatch):
    """The settle committed, so the write is the agent's answer: failing to pass the line on is not the writer's
    to act on, so the write still lands, unlike a write whose settle failed."""
    from doceditor import agent_comment_writes, live_runtime

    scope, page, entry, path, context = doc
    artifact_id = page.identity["artifact_id"]
    await page.type_after("## Terms\n", "new ", entry.live)
    _queued(await _edit(path, context, "old terms", "agent terms"))
    await page.close()
    await _settled()
    [row] = _line(artifact_id, "## Terms")
    assert row["state"] == "woken"

    async def broken(*_args, **_kwargs):
        raise RuntimeError("wake failed")

    monkeypatch.setattr(agent_comment_writes, "_drive", broken)
    monkeypatch.setattr(live_runtime, "_manager", None)
    await _edit(path, context, "old terms", "agent terms")
    assert "agent terms" in path.read_text()
    assert _line(artifact_id, "## Terms") == []


@pytest.mark.asyncio
async def test_an_agent_write_nobody_holds_takes_no_write_transaction_to_enter(doc, driven, monkeypatch):
    """The scale budget: statements per call, and write transactions, for a write touching ``k`` sections."""
    from contextlib import contextmanager

    from doceditor import section_writes
    from tools.filesystem_edit import MultiEditTool

    scope, page, entry, path, context = doc
    statements: list[str] = []
    scopes: list[str] = []
    get_db, write_transaction = section_writes.get_db, section_writes.write_transaction

    def counted_db():
        db = get_db()
        db.set_trace_callback(statements.append)
        return db

    @contextmanager
    def counted_transaction(db, *, scope):
        scopes.append(scope)
        with write_transaction(db, scope=scope) as connection:
            yield connection

    monkeypatch.setattr(section_writes, "get_db", counted_db)
    monkeypatch.setattr(section_writes, "write_transaction", counted_transaction)
    await _read(scope, path, context)
    edits = [("Intro line", "Intro"), ("old terms", "terms"), ("old scope", "scope")]
    for touched in (1, 2, 3):
        statements.clear()
        scopes.clear()
        await MultiEditTool().execute(path=str(path), edits=[
            {"old_string": old, "new_string": new} for old, new in edits[:touched]], **context)
        assert scopes == ["section_line_finish"], scopes
        assert len(statements) <= touched + 6, (touched, statements)  # grows by one line read per section
        edits = [(new, old) for old, new in edits]
        await MultiEditTool().execute(path=str(path), edits=[
            {"old_string": old, "new_string": new} for old, new in edits[:touched]], **context)
        edits = [(new, old) for old, new in edits]


@pytest.mark.asyncio
async def test_a_write_touching_no_section_takes_no_turn_and_no_transaction(doc, driven, monkeypatch):
    from doceditor import section_writes

    scope, page, entry, path, context = doc
    opened: list[str] = []
    monkeypatch.setattr(section_writes, "get_db", lambda: opened.append("db") or pytest.fail("database used"))
    refused = await _edit(path, context, "text that is not there", "anything")
    assert refused.startswith("Error") and opened == []


# -- several sections, other agents, whole-document writes, suggestions ---------------------------


@pytest.mark.asyncio
async def test_a_write_touching_several_sections_waits_in_the_held_ones_line_and_takes_no_turn(doc, driven, steps):
    from doceditor import writer_turns
    from tools.filesystem_edit import MultiEditTool

    scope, page, entry, path, context = doc
    artifact_id = page.identity["artifact_id"]
    await page.type_after("## Scope\n", "new ", entry.live)
    edits = [{"old_string": "old terms", "new_string": "agent terms"}, {"old_string": "old scope", "new_string": "agent scope"}]

    saved = _queued(await MultiEditTool().execute(path=str(path), edits=edits, **context))
    assert saved["result"].startswith("Saved, 1st in line behind Pat.")
    assert "agent terms" not in entry.live.text and "agent scope" not in entry.live.text
    assert _line(artifact_id, "## Terms") == [] and len(_line(artifact_id, "## Scope")) == 1
    assert writer_turns.agent_writing(writer_turns.section_key(artifact_id, "## Terms")) is None

    await page.close()
    await _settled()
    assert [row["state"] for row in _line(artifact_id, "## Scope")] == ["woken"] and driven == [scope["owner"]]
    posted = await MultiEditTool().execute(path=str(path), edits=edits, **context)
    assert json.loads(posted).get("outcome") != "queued"
    assert "agent terms" in path.read_text() and "agent scope" in path.read_text()
    assert _outcomes(steps) == ["queued", "woken", "posted_as_is"]
    assert writer_turns.agent_writing(writer_turns.section_key(artifact_id, "## Terms")) is None


@pytest.mark.asyncio
async def test_turns_taken_for_earlier_sections_are_released_when_a_later_one_queues(doc, driven, monkeypatch):
    from doceditor import section_writes, writer_line, writer_turns
    from infra.database import get_db, write_transaction

    scope, page, entry, path, context = doc
    artifact_id, thread = page.identity["artifact_id"], scope["owner"]
    first, second = (writer_turns.section_key(artifact_id, name) for name in ("## Terms", "## Scope"))
    admit = writer_line.admit

    def admit_then_a_person_arrives(db, *, root_id, **rest):
        if root_id == second:  # between the two admissions, so the sort could not see it
            writer_turns.take(object(), second, writer_turns.PersonHold(user_id="u", name="Pat", artifact_id=artifact_id))
        return admit(db, root_id=root_id, **rest)

    monkeypatch.setattr(writer_line, "admit", admit_then_a_person_arrives)
    db = get_db()
    try:
        with write_transaction(db, scope="test_admit_all") as connection:
            admission = section_writes._admit_all(
                connection, root_ids=[first, second], artifact_id=artifact_id, thread_id=thread, user_id="u",
                op={"kind": "edit", "call": {"tool": "edit_file", "path": "p", "edits": []}, "base": {}})
    finally:
        db.close()
    assert isinstance(admission, writer_line.Queued) and admission.root_id == second
    assert writer_turns.agent_writing(first) is None and writer_turns.agent_writing(second) is None


@pytest.mark.asyncio
async def test_an_agent_waits_behind_another_agents_write_in_flight_in_its_section(doc, driven, monkeypatch, steps):
    from doceditor import live_tools, writer_turns

    scope, page, entry, path, context = doc
    artifact_id = page.identity["artifact_id"]
    key = writer_turns.section_key(artifact_id, "## Terms")
    rival = {"_thread_id": scope["conductor"], "_thread_project_id": "project-alpha"}
    landing, release = asyncio.Event(), asyncio.Event()
    submit = live_tools._submit

    async def slow_submit(entry, path, kwargs, thread_id, *rest):
        if thread_id == scope["conductor"]:
            landing.set()
            await release.wait()
        return await submit(entry, path, kwargs, thread_id, *rest)

    monkeypatch.setattr(live_tools, "_submit", slow_submit)
    await _read(scope, path, context)
    rival_write = asyncio.create_task(_edit(path, rival, "old terms", "old terms rival"))
    await asyncio.wait_for(landing.wait(), 10)
    assert writer_turns.agent_writing(key) == scope["conductor"]

    saved = _queued(await _edit(path, context, "old terms", "older terms"))
    assert saved["result"].startswith("Saved, 1st in line behind Conductor.")
    assert [row["state"] for row in _line(artifact_id, "## Terms")] == ["waiting"]

    release.set()
    assert json.loads(await rival_write).get("outcome") != "queued" and "old terms rival" in entry.live.text
    await _settled()
    [row] = _line(artifact_id, "## Terms")
    assert row["state"] == "woken" and driven == [scope["owner"]]
    assert "+old terms rival" in _wake(row)["content"]
    posted = await _edit(path, context, "old terms", "older terms")
    assert json.loads(posted).get("outcome") != "queued"
    assert "older terms rival" in entry.live.text and _line(artifact_id, "## Terms") == []
    assert _outcomes(steps) == ["queued", "woken", "posted_as_is"]


@pytest.mark.asyncio
async def test_a_whole_document_write_is_gated_by_the_sections_it_changes(doc, driven, steps):
    from tools.filesystem_rw import WriteFileTool

    scope, page, entry, path, context = doc
    artifact_id = page.identity["artifact_id"]
    await _read(scope, path, context)

    async def write(content: str) -> str:
        return await WriteFileTool(workspace=scope["workspace"]).execute(str(path), content, **context)

    elsewhere = await write(TEXT.replace("old scope", "agent scope"))  # no one holds a section yet
    assert json.loads(elsewhere).get("outcome") != "queued" and "agent scope" in entry.live.text

    await page.type_after("## Terms\n", "new ", entry.live)
    rewrite = TEXT.replace("old scope", "agent scope").replace("old terms", "agent terms")  # what the agent has read
    saved = _queued(await write(rewrite))
    assert saved["result"].startswith("Saved, 1st in line behind Pat.")
    assert "agent terms" not in entry.live.text
    [row] = _line(artifact_id, "## Terms")
    assert json.loads(row["ops"])[0]["kind"] == "write"

    await page.close()
    await _settled()
    wake = _wake(_line(artifact_id, "## Terms")[0])["content"]
    assert f"Rewrite the whole document (write_file, {len(rewrite)} characters)." in wake
    posted = await write(rewrite)
    assert json.loads(posted).get("outcome") != "queued"
    assert "agent terms" in path.read_text() and _line(artifact_id, "## Terms") == []
    assert _outcomes(steps) == ["queued", "woken", "posted_as_is"]


@pytest.mark.asyncio
async def test_a_suggestion_in_a_held_section_is_woken_and_posted_as_is_by_the_same_call(doc, driven, steps):
    from tools.documents import DocCommentCreateTool

    scope, page, entry, path, context = doc
    artifact_id = page.identity["artifact_id"]
    await page.type_after("## Terms\n", "new ", entry.live)

    async def suggest():
        return await DocCommentCreateTool().execute(
            body="Tighter", artifact_id=artifact_id, quote="old terms", replacement="terms", **context)

    assert (await suggest()).startswith("Saved, 1st in line behind Pat.")
    assert _rows("SELECT id FROM doc_comments WHERE artifact_id=? AND replacement IS NOT NULL", artifact_id) == []

    await page.close()
    await _settled()
    [row] = _line(artifact_id, "## Terms")
    wake = _wake(row)["content"]
    assert row["state"] == "woken" and driven == [scope["owner"]]
    assert 'Suggest replacing "old terms" with "terms" (doc_comment_create). Comment: "Tighter"' in wake
    assert json.loads(_wake(row)["metadata"])["kind"] == "section_writer_turn"

    posted = json.loads(await suggest())
    assert posted["comment"]["replacement"] == "terms"
    assert _line(artifact_id, "## Terms") == []
    assert len(_rows("SELECT id FROM doc_comments WHERE artifact_id=? AND replacement IS NOT NULL", artifact_id)) == 1
    assert _outcomes(steps) == ["queued", "woken", "posted_as_is"]


# -- sections ------------------------------------------------------------------------------------


def test_sections_are_named_by_heading_lines_and_fenced_hashes_are_text():
    from doceditor import doc_sections

    text = "start\n# One\n```\n# not a heading\n```\n## Two ##\nbody\n~~~~\n## also text\n~~~~\nend\n"
    assert [heading for _, heading in doc_sections.headings(text)] == ["", "# One", "## Two"]
    assert doc_sections.section_at(text, 0) == ""
    assert doc_sections.section_at(text, text.index("not a heading")) == "# One"
    assert doc_sections.section_at(text, text.index("## Two")) == "## Two"
    assert doc_sections.section_at(text, text.index("end")) == "## Two"
    assert doc_sections.sections_in(text, text.index("start"), text.index("body")) == ["", "# One", "## Two"]
    assert doc_sections.section_text(text, "# One") == "# One\n```\n# not a heading\n```\n"
    assert doc_sections.section_text(text, "## Missing") is None
    assert doc_sections.headings("# Top\nx") == [(0, ""), (0, "# Top")]


def test_changed_span_finds_one_edit_in_large_text():
    from doceditor import doc_sections

    before = "a" * 10_000 + "middle" + "b" * 10_000
    after = before.replace("middle", "centre!")
    assert doc_sections.changed_span(before, after) == (10_000, 10_006, 10_007)
    assert doc_sections.changed_span("same", "same") == (4, 4, 4)
    assert doc_sections.changed_span("abc", "abXc") == (2, 2, 3)


def test_section_log_counts_others_since_a_read_drops_unreadable_changes_and_caps(monkeypatch):
    from doceditor import section_log

    clock = iter(f"2026-11-06T00:00:{second:02d}" for second in range(60))
    monkeypatch.setattr(section_log, "now", lambda: next(clock))
    log = section_log.SectionLog()
    log.changed(["## A"])
    assert log.changes == ()
    log.read("agent")
    log.changed(["## A"])
    log.changed(["## B"], "agent")
    log.changed(["## A", "## B"], "other")
    assert log.since("agent") == {"## A": 2, "## B": 1}
    assert log.since("stranger") is None
    log.read("agent")
    assert log.changes == () and log.since("agent") == {}
    monkeypatch.setattr(section_log, "LIMIT", 2)
    log.changed(["## A", "## B", "## C"])
    assert [heading for _, heading, _ in log.changes] == ["## B", "## C"]
