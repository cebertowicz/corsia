"""Texts through email's gate: draft, each gate refusal with kind ``message``, claim and release, send outcomes."""

from __future__ import annotations

import json

import pytest

from connections.connected_accounts import MESSAGES_PROVIDER, create_account
from delegation_fixtures import deleg_db  # noqa: F401
from doceditor.document_scope import revision as document_revision
from infra.database import get_db
from tests.messages_fixtures import fake_osascript, make_chat_db
from tests.test_action_gate import THREAD, _admit, _bind, workspace  # noqa: F401
import tools.messages_send as messages_send
import tools.messages_texts as texts
from tools import action_gate, messages_db
from tools.action_drafts import create_draft
from tools.messages_draft_tools import MessagesDraftTool, MessagesSendTool

ALL = {"mode": "all", "project_ids": [], "outside_projects": False}


@pytest.fixture
def env(deleg_db, workspace, tmp_path, monkeypatch):  # noqa: F811
    from infra import config
    from tools import sandbox_config

    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RW", [workspace])
    monkeypatch.setattr(config, "WORKSPACE_SELECTION", config._resolve_workspace_selection(workspace))
    path = make_chat_db(tmp_path / "chat.db")
    monkeypatch.setattr(messages_db, "CHAT_DB", path)
    monkeypatch.setattr(messages_db, "is_mac", lambda: True)
    monkeypatch.setattr(messages_send, "CONFIRM_SECONDS", 0.2)
    monkeypatch.setattr(messages_send, "POLL_SECONDS", 0.05)
    create_account(MESSAGES_PROVIDER, "This computer", reach=ALL, created_by=("person", "steve"), db=deleg_db)
    deleg_db.commit()

    def osascript(mode: str):
        binary, argv = fake_osascript(tmp_path, path, mode)
        monkeypatch.setattr(messages_send, "OSASCRIPT", str(binary))
        return argv

    return osascript


def _row(draft_id: str) -> dict:
    return dict(get_db().execute("SELECT * FROM action_drafts WHERE id=?", (draft_id,)).fetchone())


async def _draft(**args) -> dict:
    result = await MessagesDraftTool().execute(_thread_id=THREAD, **{"body": "hello", **args})
    assert not result.startswith("Error"), result
    return json.loads(result)


async def _send(draft: dict, **override) -> str:
    return await MessagesSendTool().execute(_thread_id=THREAD, **{"draft_id": draft["draft_id"], "revision": draft["revision"], **override})


def _approved_turn(db, turn="t1") -> None:
    _admit(db, turn)
    _bind(db, turn)


async def test_draft_names_the_resolved_recipient_and_stores_the_text_as_a_document(env, workspace) -> None:
    draft = await _draft(to="+1 (555) 555-0101")
    assert draft["to"] == "+15555550101" and draft["next"] == texts.AFTER_DRAFT
    assert (workspace / draft["path"]).read_text() == "To: +15555550101 · iMessage\n\nhello"
    group = await _draft(chat_id="2", body="see you at 7")
    assert group["to"] == "Book club (group chat, 3 people)"
    assert (workspace / group["path"]).read_text().startswith("To: Book club (group chat, 3 people) · iMessage")
    replaced = await _draft(to="+15555550101", body="hello again", draft_id=draft["draft_id"])
    assert replaced["draft_id"] == draft["draft_id"] and replaced["revision"] != draft["revision"]


async def test_draft_refusals(env, deleg_db, tmp_path, monkeypatch) -> None:
    run = MessagesDraftTool().execute
    assert await run(_thread_id=THREAD, body="x") == texts.NEEDS_ONE
    assert await run(_thread_id=THREAD, body="x", chat_id="1", to="+15555550101") == texts.NEEDS_ONE
    assert await run(_thread_id=THREAD, chat_id="1", body=" ") == texts.NEEDS_BODY
    for bad in ("John", "5555550101", "+1 555"):
        assert await run(_thread_id=THREAD, to=bad, body="x") == texts.BAD_TO
    assert await run(_thread_id=THREAD, chat_id="99", body="x") == f"Error: {texts.NO_CHAT}"
    assert await run(_thread_id=THREAD, chat_id="1", body="x", draft_id="nope") == f"Error: {texts.NO_OPEN_DRAFT}"
    make_chat_db(tmp_path / "dup.db", duplicate_number=True)
    monkeypatch.setattr(messages_db, "CHAT_DB", tmp_path / "dup.db")
    assert await run(_thread_id=THREAD, to="+15555550101", body="x") == texts.AMBIGUOUS.format(ids="1, 4")


async def test_send_after_the_persons_reply_sends_through_argv_and_marks_sent(env) -> None:
    argv = env("write")
    draft = await _draft(chat_id="1", body='say "hi" $(date); -e')
    _approved_turn(get_db())
    assert await _send(draft) == texts.SENT.format(to="+15555550101", service="iMessage")
    row = _row(draft["draft_id"])
    assert (row["state"], row["external_id"]) == ("sent", "sent-guid")
    args = json.loads(argv.read_text().splitlines()[0])
    assert args[-2:] == ["iMessage;-;+15555550101", 'say "hi" $(date); -e']
    assert all("hi" not in part for part in args[:-2])


async def test_new_number_goes_to_a_new_chat_and_is_confirmed(env) -> None:
    argv = env("write")
    draft = await _draft(to="+15555550177")
    _approved_turn(get_db())
    assert await _send(draft) == texts.SENT.format(to="+15555550177", service="iMessage")
    assert json.loads(argv.read_text().splitlines()[0])[-2:] == ["+15555550177", "hello"]


@pytest.mark.parametrize(("code", "fixed"), [("-1743", texts.NO_AUTOMATION), ("-600", texts.NO_MESSAGES_APP), ("-1728", texts.NO_SUCH_CHAT)])
async def test_proven_not_sent_releases_the_claim_and_a_retry_can_send(env, code, fixed) -> None:
    env(f"fail:{code}")
    draft = await _draft(chat_id="1")
    _approved_turn(get_db())
    assert await _send(draft) == f"Error: Not sent: {fixed}"
    assert _row(draft["draft_id"])["state"] == "open"
    env("write")
    assert await _send(draft) == texts.SENT.format(to="+15555550101", service="iMessage")


async def test_unconfirmed_send_stays_claimed_and_a_retry_refuses(env) -> None:
    env("silent")
    draft = await _draft(chat_id="1")
    _approved_turn(get_db())
    assert await _send(draft) == f"Error: {texts.UNCONFIRMED}"
    assert _row(draft["draft_id"])["state"] == "sending"
    assert await _send(draft) == f"Error: {texts.STILL_SENDING}"


async def test_an_unrecognized_failure_is_unknown_not_proven(env) -> None:
    env("fail:-9999")
    draft = await _draft(chat_id="1")
    _approved_turn(get_db())
    assert await _send(draft) == f"Error: {texts.UNCONFIRMED}"
    assert _row(draft["draft_id"])["state"] == "sending"


async def test_gate_refuses_an_agent_started_turn(env) -> None:
    draft = await _draft(chat_id="1")
    _admit(get_db(), "t1", metadata={"sender_kind": "agent", "from_thread": "x"})
    _bind(get_db(), "t1")
    assert await _send(draft) == f"Error: {action_gate.NOT_PERSON}"
    assert _row(draft["draft_id"])["state"] == "open"


async def test_gate_refuses_when_the_person_has_not_replied_since_the_save(env) -> None:
    _admit(get_db(), "t1")
    draft = await _draft(chat_id="1")
    _bind(get_db(), "t1")
    assert await _send(draft) == f"Error: {action_gate.NOT_SEEN}"


async def test_gate_refuses_a_stale_revision_an_outside_edit_and_a_finished_draft(env, workspace) -> None:
    draft = await _draft(chat_id="1")
    _approved_turn(get_db())
    assert await _send(draft, revision="0" * 64) == f"Error: {action_gate.STALE_REVISION}"
    (workspace / draft["path"]).write_text("To: +15555550101 · iMessage\n\nsomething else")
    assert await _send(draft) == f"Error: {action_gate._changed_outside('message')}"
    assert "messages_draft" in action_gate._changed_outside("message")


async def test_gate_refuses_a_sent_draft_a_foreign_kind_and_no_turn(env, workspace) -> None:
    env("write")
    draft = await _draft(chat_id="1")
    _approved_turn(get_db())
    assert (await _send(draft)).startswith("Sent to")
    assert await _send(draft) == f"Error: {action_gate.ALREADY_DONE}"
    (workspace / "other.md").write_text("To: sam@example.com\n\nhi")
    email = create_draft(THREAD, "email", "other.md", b"To: sam@example.com\n\nhi", {}, db=get_db())
    get_db().commit()
    assert await _send(email) == f"Error: {action_gate.NO_DRAFT}"
    from infra import state

    state._current_turn_identity.set(None)
    other = await _draft(chat_id="1", body="again")
    assert await _send(other) == f"Error: {action_gate.NO_TURN}"


async def test_send_checks_the_reach_and_the_draft_format(env, workspace) -> None:
    draft = await _draft(chat_id="1")
    (workspace / draft["path"]).write_bytes(b"no header here")
    get_db().execute("UPDATE action_drafts SET recorded_revision=? WHERE id=?", (document_revision(b"no header here"), draft["draft_id"]))
    get_db().commit()
    _approved_turn(get_db())
    revision = _row(draft["draft_id"])["recorded_revision"]
    assert await MessagesSendTool().execute(_thread_id=THREAD, draft_id=draft["draft_id"], revision=revision) == f"Error: {texts.BAD_DOCUMENT}"
    get_db().execute("DELETE FROM connected_accounts")
    get_db().commit()
    assert await MessagesSendTool().execute(_thread_id=THREAD, draft_id=draft["draft_id"], revision=revision) == f"Error: Not sent: {texts.NOT_CONNECTED}"


def test_migration_170_to_171_keeps_rows_and_admits_message_drafts() -> None:
    import sqlite3

    from infra.migration_170_to_171_message_drafts import migrate_170_to_171

    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY)")
    db.execute(
        "CREATE TABLE action_drafts (id TEXT PRIMARY KEY, thread_id TEXT NOT NULL REFERENCES threads(id), "
        "kind TEXT NOT NULL CHECK (kind IN ('email', 'invite')), path TEXT NOT NULL, "
        "target_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(target_json)), state TEXT NOT NULL DEFAULT 'open' "
        "CHECK (state IN ('open', 'sending', 'sent', 'discarded')), recorded_revision TEXT NOT NULL, "
        "recorded_at TEXT NOT NULL, created_at TEXT NOT NULL, sent_at TEXT, external_id TEXT)"
    )
    before = [("a", "t", "email", "a.md", '{"x":1}', "sent", "r1", "2026-01-01", "2026-01-01", "2026-01-02", "ext"),
              ("b", "t", "invite", "b.md", "{}", "open", "r2", "2026-01-03", "2026-01-03", None, None)]
    db.executemany("INSERT INTO action_drafts VALUES (?,?,?,?,?,?,?,?,?,?,?)", before)
    migrate_170_to_171(db)
    assert db.execute("SELECT * FROM action_drafts ORDER BY id").fetchall() == before
    db.execute("INSERT INTO action_drafts (id,thread_id,kind,path,recorded_revision,recorded_at,created_at) "
               "VALUES ('c','t','message','c.md','r3','now','now')")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE action_drafts SET kind='sms' WHERE id='c'")
    assert db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'action_drafts%'").fetchall() == [("action_drafts",)]
