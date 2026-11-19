"""The attention notice on an update that links documents.

Each case saves a real update row and names real files in it, so what is
under test is what the notice reads back: the saved message, the files chat
shows the person as cards under it, and this agent's open asks.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import core.attention_notice as mod
from infra.queries import insert_message
from tests.plan_close_next_helpers import owner_plan
from tests.project_ref_helpers import project_ref, select_workspace
from tests.test_attention_notice import _ask, _document, _Open, _words, events  # noqa: F401 - shared fixture
from tests.test_plan_reconcile_release import db  # noqa: F401 - in-memory plan fixture
from tools.post_update_text import result_text


@pytest.fixture
def workspace(tmp_path, monkeypatch) -> Path:
    """The install's workspace root, which scopes the files an update outside a project may name."""
    root = tmp_path.resolve()
    select_workspace(monkeypatch, root)
    return root


def _post(db, text: str) -> tuple[dict, str]:  # noqa: F811
    """Save ``text`` as ``owner``'s update; return the call's arguments and its real receipt."""
    message_id = insert_message(
        "owner", "assistant", text, {"for_user": True, "turn_id": "turn-1"}, db=db,
    )
    db.commit()
    return {"text": text}, result_text(message_id, [], [], "owner")


def _notice(db, text: str) -> str | None:  # noqa: F811
    arguments, result = _post(db, text)
    return mod.attention_notice(
        thread_id="owner", tool_name="post_update", arguments=arguments, result=result,
    )


def test_an_update_that_links_one_document_states_its_words(db, events, workspace):  # noqa: F811
    _document(db, workspace / "plan.md", _words(6200))

    notice = _notice(db, "The plan is ready: [plan](plan.md).")

    assert (
        "Your update links 'plan.md' for the person to read: "
        "6,200 words, at least 27 minutes to read. If they could act on less, cut the "
        "file now, before they open it: put first what they must decide or do, and keep "
        "only what they need for it."
    ) in notice
    assert [{**event, "properties": {key: value for key, value in event["properties"].items()
                                      if not key.startswith("update_")}} for event in events] == [{
        "category": "attention_notice", "action": "notice_emitted", "thread_id": "owner",
        "properties": {
            "handover": "update", "measured": True, "words": 6200, "characters": 0,
            "images": 0, "waiting_documents": 1, "linked_documents": 1,
        },
    }]


def test_several_linked_documents_are_totalled_and_the_longest_named(db, events, workspace):  # noqa: F811
    """Registered or not, by link or bare path: each document the person gets a card for counts once."""
    _document(db, workspace / "one.md", _words(1000))
    (workspace / "notes").mkdir()
    (workspace / "notes" / "two.md").write_text(_words(4000), encoding="utf-8")
    (workspace / "three.txt").write_text(_words(500), encoding="utf-8")

    notice = _notice(
        db, "Read [one](one.md), then notes/two.md and three.txt. Again: [one](one.md), one.md.",
    )

    assert (
        "Your update links 3 documents for the person to read: "
        "5,500 words and at least 24 minutes in all. The longest is 'two.md': 4,000 words, "
        "at least 17 minutes to read. If they could act on less, link fewer or cut them "
        "now, before they open them: put first what they must decide or do, and keep only "
        "what they need for it."
    ) in notice
    assert {key: value for key, value in events[-1]["properties"].items()
            if not key.startswith("update_")} == {
        "handover": "update", "measured": True, "words": 5500, "characters": 0,
        "images": 0, "waiting_documents": 3, "linked_documents": 3,
    }


def test_open_asks_are_counted_with_what_the_update_links(db, events, workspace):  # noqa: F811
    plan = owner_plan(db)
    asked = _document(db, workspace / "design.md", _words(2000))
    _ask(db, plan.id, "Design", asked)
    _document(db, workspace / "plan.md", _words(1000))

    notice = _notice(db, "Also see plan.md.")

    assert "Your update links 'plan.md' for the person to read: 1,000 words" in notice
    assert (
        "You now have 2 documents waiting on them: 3,000 words and at least 13 minutes in all."
        in notice
    )
    assert events[-1]["properties"]["waiting_documents"] == 2


def test_linking_a_document_already_asked_counts_it_once(db, events, workspace):  # noqa: F811
    plan = owner_plan(db)
    asked = _document(db, workspace / "design.md", _words(2000))
    _ask(db, plan.id, "Design", asked)

    notice = _notice(db, "The design is in design.md.")

    assert "Your update links 'design.md' for the person to read: 2,000 words" in notice
    assert "waiting on them" not in notice


def test_files_the_person_is_not_handed_to_read_are_not_counted(db, events, workspace, tmp_path_factory):  # noqa: F811
    """Code, an image, a missing file and a file outside the scope are not documents put before them."""
    (workspace / "runner.py").write_text(_words(900), encoding="utf-8")
    (workspace / "frame.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    outside = tmp_path_factory.mktemp("elsewhere") / "private.md"
    outside.write_text(_words(900), encoding="utf-8")

    notice = _notice(db, f"Fixed in runner.py:12, see frame.png, gone.md and {outside}.")

    assert notice is not None and "Your update links" not in notice
    assert events[-1]["properties"]["linked_documents"] == 0


def test_text_in_a_code_fence_links_nothing(db, events, workspace):  # noqa: F811
    _document(db, workspace / "plan.md", _words(1000))

    notice = _notice(db, "Ran:\n```\ncat plan.md\n```\nDone.")
    assert notice is not None and "Your update links" not in notice


def test_an_update_that_names_no_file_counts_its_words(db, events, workspace):  # noqa: F811
    notice = _notice(db, "Merged. Nothing needs you.")
    assert "Your update: 4 words" in notice
    assert events[-1]["properties"]["update_words"] == 4
    assert events[-1]["properties"]["linked_documents"] == 0


@pytest.mark.parametrize("result", [
    "Error: Nothing was saved. text must be non-empty after trimming.",
    None,
])
def test_an_update_that_was_not_saved_carries_no_notice(db, events, workspace, monkeypatch, result):  # noqa: F811
    _document(db, workspace / "plan.md", _words(1000))

    def _no_database():
        raise AssertionError("the notice opened the database for an update that was not saved")

    monkeypatch.setattr(mod, "get_db", _no_database)

    assert mod.attention_notice(
        thread_id="owner", tool_name="post_update", arguments={"text": "See plan.md."}, result=result,
    ) is None


def test_a_receipt_for_another_chats_message_links_nothing(db, events, workspace):  # noqa: F811
    _document(db, workspace / "plan.md", _words(1000))
    arguments, result = _post(db, "See plan.md.")

    assert mod.attention_notice(
        thread_id="worker", tool_name="post_update", arguments=arguments, result=result,
    ) is None
    assert events == []


def test_a_replayed_receipt_without_saved_text_still_names_the_update(db, events, workspace):  # noqa: F811
    """``tools.post_update_retry.replay_result`` falls back to the bare first line."""
    _document(db, workspace / "plan.md", _words(1000))
    arguments, result = _post(db, "See plan.md.")

    notice = mod.attention_notice(
        thread_id="owner", tool_name="post_update", arguments=arguments,
        result=result.split("\n", 1)[0],
    )

    assert "Your update links 'plan.md' for the person to read: 1,000 words" in notice


def test_a_linked_document_that_cannot_be_read_as_text_says_so(db, events, workspace):  # noqa: F811
    _document(db, workspace / "scan.md", b"\xff\xfe\x00\x89")

    notice = _notice(db, "See scan.md.")

    assert (
        "Your update links 'scan.md' for the person to read: its "
        "length could not be measured, because the file could not be read as text."
    ) in notice
    assert events[-1]["properties"]["measured"] is False


def test_a_failed_read_leaves_the_receipt_alone_and_is_logged(db, events, workspace, monkeypatch, caplog):  # noqa: F811
    import sqlite3

    _document(db, workspace / "plan.md", _words(1000))
    arguments, result = _post(db, "See plan.md.")

    class _Broken(_Open):
        def execute(self, *_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(mod, "get_db", lambda: _Broken(db))

    with caplog.at_level("WARNING", logger=mod.__name__):
        assert mod.attention_notice(
            thread_id="owner", tool_name="post_update", arguments=arguments, result=result,
        ) is None
    assert "could not project post_update" in caplog.text
    assert events == []


def test_a_project_update_counts_the_documents_chat_shows_as_cards(db, events, workspace):  # noqa: F811
    """The notice and the chat page resolve an update's file names the same way.

    A project chat's path resolves from the project folder and, when no file
    is there, from the workspace root. ``api.thread_cards.attach_cards`` owns
    that rule; this holds the notice to the cards it actually produces.
    """
    from api import thread_cards

    project = workspace / "projects" / "p"
    (project / "shared").mkdir(parents=True)
    (project / "notes").mkdir()
    (project / "shared" / "plan.md").write_text(_words(1000), encoding="utf-8")
    (project / "notes" / "brief.md").write_text(_words(2000), encoding="utf-8")
    (project / "notes" / "run.py").write_text(_words(50), encoding="utf-8")
    db.execute(
        "INSERT INTO projects (id, name, path, created_at, updated_at) VALUES ('p', 'P', ?, 't', 't')",
        (project_ref("p"),),
    )
    db.execute("UPDATE threads SET project_id='p' WHERE id='owner'")
    db.commit()
    text = "See shared/plan.md and projects/p/notes/brief.md; ran projects/p/notes/run.py."
    arguments, result = _post(db, text)

    notice = mod.attention_notice(
        thread_id="owner", tool_name="post_update", arguments=arguments, result=result,
    )
    row = dict(db.execute("SELECT * FROM messages WHERE thread_id='owner'").fetchone())
    thread_cards.attach_cards(db, "owner", "user", {"project_id": "p"}, [row])

    shown = sorted(card["name"] for card in row["cards"] if card["type"] == "document")
    assert shown == ["brief.md", "plan.md"]
    assert "Your update links 2 documents for the person to read: 3,000 words" in notice
    assert "The longest is 'brief.md': 2,000 words" in notice
    assert events[-1]["properties"]["linked_documents"] == len(shown)


def test_a_document_named_only_in_a_placed_comment_answer_is_not_counted(db, events, workspace):  # noqa: F811
    """Cards follow the chat text saved for an update that answers comments; so does the notice.

    The answer section moves to its comment thread, so chat draws no card for
    a file named only there. Both sides read the text
    ``infra.chat_comment_answers.VISIBLE_CONTENT`` names.
    """
    import json

    from api import thread_cards

    _document(db, workspace / "plan.md", _words(1000))
    _document(db, workspace / "brief.md", _words(2000))
    seq = db.execute("SELECT COALESCE(MAX(seq), -1) + 1 FROM messages WHERE thread_id='owner'").fetchone()[0]
    db.execute(
        "INSERT INTO messages(id,thread_id,role,content,metadata,created_at,seq,input_order) "
        "VALUES ('question','owner','user','text',?,'2026-01-01',?,1)",
        (json.dumps({"reply_context": {"comments": [{"cid": "c1.1"}]}}), seq),
    )
    db.execute("UPDATE threads SET next_message_seq=? WHERE id='owner'", (seq + 1,))
    db.commit()
    arguments, result = _post(db, "The plan is in plan.md.\n\n[c1.1] The detail is in brief.md.")

    notice = mod.attention_notice(
        thread_id="owner", tool_name="post_update", arguments=arguments, result=result,
    )
    row = dict(db.execute("SELECT * FROM messages WHERE thread_id='owner' AND role='assistant'").fetchone())
    assert db.execute(
        "SELECT content FROM chat_visible_content WHERE message_id=?", (row["id"],),
    ).fetchone()[0] == "The plan is in plan.md."
    thread_cards.attach_cards(db, "owner", "user", {"project_id": None}, [row])

    shown = sorted(card["name"] for card in row["cards"] if card["type"] == "document")
    assert shown == ["plan.md"]
    assert "Your update links 'plan.md' for the person to read: 1,000 words" in notice
    assert events[-1]["properties"]["linked_documents"] == len(shown)
