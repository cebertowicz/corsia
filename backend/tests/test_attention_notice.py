"""The attention notice: measured facts about what a call put in front of the person.

Every case hands a real file over through the real plan boundary, so what is
under test is the state the notice reads, not a mock of it. Only the database
handle and the product event are replaced: the notice opens and closes its own
connection, and the fixture's in-memory one must outlive the call.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import core.attention_notice as mod
from plans import service
from plans.boundary import PlanActor
from registry import models as registry_models
from tests.plan_close_next_helpers import owner_plan, plan_update, task
from tests.test_plan_reconcile_release import db, seed_threads  # noqa: F401 - in-memory plan fixture


class _Open:
    """The fixture connection, minus the close the notice does on its own handle."""

    def __init__(self, connection):
        self._connection = connection

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def close(self) -> None:
        return None


@pytest.fixture
def events(db, monkeypatch):  # noqa: F811
    recorded: list[dict] = []
    monkeypatch.setattr(mod, "get_db", lambda: _Open(db))
    monkeypatch.setattr(
        mod, "emit_product_event",
        lambda category, action, **kwargs: recorded.append(
            {"category": category, "action": action, **kwargs}
        ),
    )
    return recorded


def _document(db, path: Path, text: str | bytes, owner: str = "owner") -> str:  # noqa: F811
    """Write ``text`` to ``path`` and register it as ``owner``'s document."""
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")
    artifact = registry_models.register_artifact(
        integration="local", type="document", external_id=str(path),
        parent_thread_id=owner, title=path.name, path=str(path),
        metadata={"zone": "workspace"}, db=db,
    )
    db.commit()
    return str(artifact["id"])


def _words(count: int) -> str:
    return " ".join(["word"] * count)


def _ask(db, plan_id: str, title: str, artifact_id: str) -> dict:  # noqa: F811
    """Block a new task on the person with a document to judge; return the call's arguments."""
    node = task(db, plan_id, title)
    params = {
        "node_id": node.id, "expected_revision": service._node_row(db, node.id)["revision"],
        "reason_key": "review", "reason": "Review it", "owner_kind": "user",
        "ask_artifact_id": artifact_id,
    }
    plan_update(db, plan_id, "block_node", params)
    return {"plan_id": plan_id, "operation": "block_node", "params": params}


def _notice(arguments: dict, **kwargs) -> str | None:
    return mod.attention_notice(
        thread_id="owner", tool_name="plan_update", arguments=arguments, **kwargs,
    )


def test_an_ask_states_the_documents_words_and_reading_time(db, events, tmp_path):  # noqa: F811
    plan = owner_plan(db)
    document = _document(db, tmp_path / "proposal.md", _words(500))

    notice = _notice(_ask(db, plan.id, "Proposal", document))

    assert notice == (
        "[CORSIA attention notice] The person now has 'proposal.md' to read before they "
        "can answer: 500 words, at least 3 minutes to read. If they could act on less, "
        "cut the file now, before they open it: put first what they must decide or do, "
        "and keep only what they need for it."
    )
    assert events == [{
        "category": "attention_notice", "action": "notice_emitted", "thread_id": "owner",
        "properties": {
            "handover": "ask", "measured": True, "words": 500, "characters": 0,
            "images": 0, "waiting_documents": 1,
        },
    }]


def test_a_short_document_is_stated_the_same_way_with_no_threshold(db, events, tmp_path):  # noqa: F811
    """No length is treated as too long or short enough: the facts are stated either way."""
    plan = owner_plan(db)
    document = _document(db, tmp_path / "note.md", _words(40))

    notice = _notice(_ask(db, plan.id, "Note", document))

    assert "'note.md' to read before they can answer: 40 words, at least 1 minute to read." in notice


def test_images_and_long_reads_are_counted(db, events, tmp_path):  # noqa: F811
    plan = owner_plan(db)
    text = _words(15000) + "\n![frame](a.png)\n<img src='b.png'>\n"
    document = _document(db, tmp_path / "design.md", text)

    notice = _notice(_ask(db, plan.id, "Design", document))

    # 15,000 words plus the seven in the image markup, at 238 a minute, rounded up.
    assert "15,007 words, at least 1 hour 4 minutes to read, 2 images." in notice


def test_several_waiting_documents_are_totalled(db, events, tmp_path):  # noqa: F811
    plan = owner_plan(db)
    first = _document(db, tmp_path / "one.md", _words(1000))
    second = _document(db, tmp_path / "two.md", _words(2000))
    _ask(db, plan.id, "One", first)

    notice = _notice(_ask(db, plan.id, "Two", second))

    assert "'two.md' to read before they can answer: 2,000 words, at least 9 minutes to read." in notice
    assert (
        "You now have 2 documents waiting on them: 3,000 words and at least 13 minutes in all."
        in notice
    )
    assert events[-1]["properties"]["waiting_documents"] == 2


def test_another_agents_waiting_documents_are_not_counted(db, events, tmp_path):  # noqa: F811
    db.execute(
        "INSERT INTO threads (id,user_id,spawned_by,model,created_at,updated_at) "
        "VALUES ('other', 'user', NULL, 'test/model', 't', 't')"
    )
    other_plan = service.ensure_plan("thread", "other", db=db)
    theirs = _document(db, tmp_path / "theirs.md", _words(900), owner="other")
    node = task(db, other_plan.id, "Theirs")
    plan_update(db, other_plan.id, "block_node", {
        "node_id": node.id, "expected_revision": service._node_row(db, node.id)["revision"],
        "reason_key": "review", "reason": "Review it", "owner_kind": "user",
        "ask_artifact_id": theirs,
    }, actor=PlanActor("other", "user", None))
    plan = owner_plan(db)
    mine = _document(db, tmp_path / "mine.md", _words(300))

    notice = _notice(_ask(db, plan.id, "Mine", mine))

    assert "waiting on them" not in notice


def test_a_call_that_did_not_take_effect_carries_no_notice(db, events, tmp_path):  # noqa: F811
    """The notice reads Plans' state, so a refused or failed ask says nothing."""
    plan = owner_plan(db)
    document = _document(db, tmp_path / "proposal.md", _words(500))
    node = task(db, plan.id, "Proposal")
    arguments = {"plan_id": plan.id, "operation": "block_node", "params": {
        "node_id": node.id, "expected_revision": 0, "reason_key": "review",
        "reason": "Review it", "owner_kind": "user", "ask_artifact_id": document,
    }}

    assert _notice(arguments) is None
    assert events == []


def test_an_ask_another_agent_holds_on_the_document_is_not_this_agents(db, events, tmp_path):  # noqa: F811
    plan = owner_plan(db)
    document = _document(db, tmp_path / "proposal.md", _words(500))
    arguments = _ask(db, plan.id, "Proposal", document)

    assert mod.attention_notice(
        thread_id="worker", tool_name="plan_update", arguments=arguments,
    ) is None


def test_a_person_step_states_its_instructions_document(db, events, tmp_path):  # noqa: F811
    plan = owner_plan(db)
    document = _document(db, tmp_path / "setup.md", _words(700))
    params = {
        "kind": "task", "title": "Set up the workspace", "acceptance_criteria": "It is ready.",
        "size": "unknown", "assignee": "user",
        "description": "Run this setup. Already tried: I used the supported agent route.",
        "instructions_artifact_id": document,
    }
    plan_update(db, plan.id, "add", params)

    notice = _notice({"plan_id": plan.id, "operation": "add", "params": params})

    assert notice.startswith(
        "[CORSIA attention notice] The person now has 'setup.md' to read before they can "
        "do their step: 700 words, at least 3 minutes to read."
    )
    assert events[-1]["properties"]["handover"] == "step"


def test_a_person_step_that_was_not_created_carries_no_notice(db, events, tmp_path):  # noqa: F811
    plan = owner_plan(db)
    document = _document(db, tmp_path / "setup.md", _words(700))
    params = {"assignee": "user", "instructions_artifact_id": document, "title": "Set up"}

    assert _notice({"plan_id": plan.id, "operation": "add", "params": params}) is None


@pytest.mark.parametrize("outcome", ["requested", "link", "already_open"])
def test_opening_a_document_for_the_person_states_it(db, events, tmp_path, outcome):  # noqa: F811
    document = _document(db, tmp_path / "report.md", _words(476))

    notice = mod.attention_notice(
        thread_id="owner", tool_name="document_open", arguments={"artifact_id": document},
        result=json.dumps({"outcome": outcome, "reason": None}),
    )

    assert notice.startswith(
        "[CORSIA attention notice] You put 'report.md' in front of the person: "
        "476 words, at least 2 minutes to read."
    )
    assert events[-1]["properties"]["handover"] == "open"


def test_opening_by_path_resolves_against_the_tools_workspace(db, events, tmp_path):  # noqa: F811
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "report.md").write_text(_words(100), encoding="utf-8")

    notice = mod.attention_notice(
        thread_id="owner", tool_name="document_open", arguments={"path": "shared/report.md"},
        result=json.dumps({"outcome": "requested"}), workspace=tmp_path,
    )

    assert "You put 'report.md' in front of the person: 100 words" in notice


@pytest.mark.parametrize("result", [
    json.dumps({"outcome": "refused", "reason": "not yours"}),
    "Error: pass exactly one of path or artifact_id",
    None,
])
def test_a_document_that_was_not_opened_carries_no_notice(db, events, tmp_path, result):  # noqa: F811
    document = _document(db, tmp_path / "report.md", _words(476))

    assert mod.attention_notice(
        thread_id="owner", tool_name="document_open", arguments={"artifact_id": document},
        result=result,
    ) is None
    assert events == []


def test_text_without_spaces_is_counted_in_characters_with_no_reading_time(db, events, tmp_path):  # noqa: F811
    plan = owner_plan(db)
    document = _document(db, tmp_path / "keikaku.md", "計画の概要です。" * 100 + " plan")

    notice = _notice(_ask(db, plan.id, "Plan", document))

    assert "'keikaku.md' to read before they can answer: 700 characters." in notice
    assert "minute" not in notice


@pytest.mark.parametrize("content", [None, b"\x89PNG\r\n\x1a\n\xff\xfe\x00"])
def test_a_file_that_cannot_be_read_says_so(db, events, tmp_path, content):  # noqa: F811
    """Unmeasured is stated, never silent, and no cut is suggested with nothing measured."""
    plan = owner_plan(db)
    path = tmp_path / "frame.png"
    document = _document(db, path, content or b"")
    if content is None:
        path.unlink()

    notice = _notice(_ask(db, plan.id, "Frame", document))

    assert notice == (
        "[CORSIA attention notice] The person now has 'frame.png' to read before they can "
        "answer: its length could not be measured, because the file could not be read as text."
    )
    assert events[-1]["properties"]["measured"] is False


def test_a_failed_read_leaves_the_result_alone_and_is_logged(db, events, tmp_path, monkeypatch, caplog):  # noqa: F811
    """The ask is already raised: a database error here must not become the call's result."""
    import sqlite3

    plan = owner_plan(db)
    document = _document(db, tmp_path / "proposal.md", _words(500))
    arguments = _ask(db, plan.id, "Proposal", document)

    class _Broken(_Open):
        def execute(self, *_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(mod, "get_db", lambda: _Broken(db))

    with caplog.at_level("WARNING", logger=mod.__name__):
        assert _notice(arguments) is None
    assert "could not read what plan_update handed over" in caplog.text
    assert events == []


@pytest.mark.parametrize("tool_name, arguments", [
    ("read_file", {"path": "shared/report.md"}),
    ("plan_update", {"operation": "add", "params": {"title": "Mine", "assignee": None}}),
    ("plan_update", {"operation": "block_node", "params": {"owner_kind": "external"}}),
    ("plan_update", {"operation": "block_node", "params": "not-an-object"}),
    ("plan_update", None),
])
def test_other_calls_read_nothing(monkeypatch, tool_name, arguments):
    """Only a hand-over call opens the database: every other result costs nothing."""
    def _no_database():
        raise AssertionError("the notice opened the database for a call that hands nothing over")

    monkeypatch.setattr(mod, "get_db", _no_database)

    assert mod.attention_notice(
        thread_id="owner", tool_name=tool_name, arguments=arguments, result="ok",
    ) is None
