"""A document section's writer turn: an agent edit or suggestion in a section a person is changing waits in line."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import pytest

from doceditor.live_runtime import LiveDocuments
from doceditor.live_text import clone
from tests.test_document_live_transport import Socket
from tests.test_shared_document_ownership import ownership_scope as ownership_scope, _write

TEXT = "Intro line\n## Terms\nold terms\n## Scope\nold scope\n"


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
            **self.identity, "type": "doc_update", "message_id": f"m{self.sent}",
            "update": base64.b64encode(self.peer.get_update(before)).decode()})

    async def close(self) -> None:
        from ws import document_control

        await document_control.handle_frame(self.socket, self.user, {**self.identity, "type": "doc_close"})


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


def _line(artifact_id: str, heading: str) -> list[dict]:
    from doceditor import writer_turns

    return _rows("SELECT * FROM doc_comment_writer_line WHERE root_id=? ORDER BY saved_at,id",
                 writer_turns.section_key(artifact_id, heading))


def _wake(row: dict) -> dict:
    [message] = _rows("SELECT * FROM messages WHERE id=?", row["wake_message_id"])
    return message


@pytest.fixture
async def doc(ownership_scope, tmp_path, monkeypatch):
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
    _write(scope, "sections.md", TEXT)
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
    from ws import document_control, section_turn

    scope, page, entry, path, context = doc
    artifact_id = page.identity["artifact_id"]
    if leaving == "idle":
        monkeypatch.setattr(section_turn, "IDLE_SECONDS", 0.01)
    await _read(scope, path, context)
    await page.type_after("## Terms\n", "new ", entry.live)
    _queued(await _edit(path, context, "old terms", "agent terms"))

    if leaving == "idle":
        await asyncio.sleep(0.05)
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
    assert not writer_turns.may_answer_section(scope["owner"])
    monkeypatch.setattr(live_runtime, "_manager", manager)


@pytest.mark.asyncio
async def test_a_woken_agents_next_call_answers_its_wake_even_when_that_call_is_refused(doc, driven, steps):
    """The row settles as the call is let through; the refusal is the call's own result, never a drop note."""
    from doceditor import writer_turns

    scope, page, entry, path, context = doc
    artifact_id = page.identity["artifact_id"]
    await page.type_after("## Terms\n", "new ", entry.live)
    _queued(await _edit(path, context, "old terms", "agent terms"))
    await page.close()
    await _settled()
    assert [row["state"] for row in _line(artifact_id, "## Terms")] == ["woken"]

    refused = await _edit(path, context, "text that is not there", "anything")

    assert "agent terms" not in entry.live.text and "anything" not in entry.live.text
    assert refused.startswith("Error"), refused
    assert _line(artifact_id, "## Terms") == []
    assert _outcomes(steps) == ["queued", "woken", "rewritten"]
    assert not writer_turns.may_answer_section(scope["owner"])
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
