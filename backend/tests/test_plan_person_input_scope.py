"""The person's words reach a task owner only inside the scope Context & Memory already enforces for them."""

from __future__ import annotations

import pytest

from infra.database import get_db
from test_plan_person_input import PROJECT, SAID, _hear, _say, _task

EXPORT = "Rebuild the invoice exporter"


@pytest.fixture
def db(isolated_db, monkeypatch):
    monkeypatch.setattr("jobs.corrections_realtime.notify", lambda *_args: None)
    connection = get_db()
    try:
        yield connection
    finally:
        connection.close()


def _thread(db, thread_id: str, *, user: str = "u-test", project: str | None = PROJECT, local_only: int = 0) -> str:
    db.execute(
        "INSERT INTO threads (id,user_id,project_id,model,title,created_at,updated_at,local_only) "
        "VALUES (?,?,?,'claude-code/opus',?,'t','t',?)", (thread_id, user, project, thread_id, local_only),
    )
    db.commit()
    return thread_id


def test_the_same_scope_still_wakes_the_owner(db):
    owner = _task(db, _thread(db, "owner"), EXPORT)
    _say(db, _thread(db, "speaker"), SAID)

    assert _hear(db, "message", "m1") == [f"plan-wake:intent_input:{owner}:message:m1"]


def test_words_from_a_local_only_chat_do_not_wake_a_cloud_owner(db):
    _task(db, _thread(db, "owner"), EXPORT)
    _say(db, _thread(db, "speaker", local_only=1), SAID)

    assert _hear(db, "message", "m1") == []


def test_words_from_a_local_only_chat_still_wake_a_local_only_owner(db):
    owner = _task(db, _thread(db, "owner", local_only=1), EXPORT)
    _say(db, _thread(db, "speaker", local_only=1), SAID)

    assert _hear(db, "message", "m1") == [f"plan-wake:intent_input:{owner}:message:m1"]


def test_another_users_task_is_not_woken(db):
    _task(db, _thread(db, "owner", user="u-other"), EXPORT)
    _say(db, _thread(db, "speaker"), SAID)

    assert _hear(db, "message", "m1") == []


def test_scoring_runs_before_the_write_transaction_opens(db, monkeypatch):
    from contextlib import contextmanager

    import infra.database
    from plans import person_input

    owner = _task(db, _thread(db, "owner"), EXPORT)
    _say(db, _thread(db, "speaker"), SAID)
    seen: list[tuple[str, bool]] = []
    score, write = person_input.scored, infra.database.write_transaction

    def scored(read, nodes, text):
        seen.append(("scored", read.in_transaction))
        return score(read, nodes, text)

    @contextmanager
    def watched(connection, *, scope):
        seen.append(("write", False))
        with write(connection, scope=scope) as opened:
            yield opened

    monkeypatch.setattr(person_input, "scored", scored)
    monkeypatch.setattr(infra.database, "write_transaction", watched)

    assert _hear(db, "message", "m1") == [f"plan-wake:intent_input:{owner}:message:m1"]
    assert seen == [("scored", False), ("write", False)]


def test_a_relayed_message_wakes_nobody(db):
    _task(db, _thread(db, "owner"), EXPORT)
    from infra.input_admission import accept_thread_input

    accept_thread_input(
        _thread(db, "speaker"), role="user", content=SAID, intent="apply_now", message_id="m1",
        metadata={"sender_kind": "agent"}, db=db,
    )
    db.commit()

    assert _hear(db, "message", "m1") == []


def test_a_no_project_chat_does_not_reach_another_users_no_project_plan(db):
    _task(db, _thread(db, "owner", user="u-other", project=None), EXPORT)
    _say(db, _thread(db, "speaker", project=None), SAID)

    assert _hear(db, "message", "m1") == []
