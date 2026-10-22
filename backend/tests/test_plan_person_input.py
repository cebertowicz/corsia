"""The person's new words wake the owners of the running tasks they bear on (alignment AL4, part 2)."""

from __future__ import annotations

import asyncio
import json

import pytest

from doc_feedback_helpers import add_comment, seed_document
from infra.database import get_db
from infra.input_admission import accept_thread_input
from plans import admission, person_input, plan_wake, service

PROJECT = "proj-1"
EXPORT = "Rebuild the invoice exporter"
SAID = "Rebuild the invoice exporter so totals reconcile before Friday"


@pytest.fixture
def db(isolated_db, monkeypatch):
    monkeypatch.setattr("jobs.corrections_realtime.notify", lambda *_args: None)
    connection = get_db()
    try:
        yield connection
    finally:
        connection.close()


def _thread(db, thread_id: str, project: str | None = PROJECT) -> str:
    db.execute(
        "INSERT INTO threads (id,user_id,project_id,model,title,created_at,updated_at) "
        "VALUES (?,'u-test',?,'claude-code/opus',?,'t','t')", (thread_id, project, thread_id),
    )
    db.commit()
    return thread_id


def _task(
    db, owner: str, title: str, *, running: bool = True, started: bool = True, intent_node_id: str | None = None,
    criteria: str = "Exporter emits CSV; totals reconcile",
) -> str:
    plan = service.ensure_plan("thread", owner, db=db)
    node = service.add_node(
        plan.id, kind="task", title=title, acceptance_criteria=criteria,
        intent_node_id=intent_node_id, db=db,
    )
    if started:
        service.transition_node(node.id, "active", db=db)
    if running:
        admission.start(
            node.id, expected_revision=service.get_node(node.id, db=db).revision,
            actor_id=owner, provenance="test", db=db,
        )
    db.commit()
    return node.id


def _say(db, thread: str, text: str, message_id: str = "m1") -> str:
    accept_thread_input(
        thread, role="user", content=text, intent="apply_now", message_id=message_id, db=db,
    )
    db.commit()
    return message_id


def _decision(db, decision_id: str, *, thread: str, source_node: str, text: str = SAID) -> None:
    body = {
        "state": "answered", "thread_id": thread, "revision": 2, "answered_at": "2026-10-21T00:00:00Z",
        "title": "Which?", "source_node_id": source_node, "node_id": source_node,
        "answer": {"option_id": "other", "text": text},
    }
    db.execute(
        """INSERT INTO artifact_identities
           (id,integration,type,external_id,state,metadata,created_at,updated_at)
           VALUES (?,'local','plan-decision',?,'active',?,'t','t')""",
        (f"artifact-{decision_id}", decision_id, json.dumps({"plan_decision": body})),
    )
    db.commit()


def _wakes(db) -> list[tuple[str, str]]:
    return [
        (row["thread_id"], row["id"]) for row in db.execute(
            "SELECT id,thread_id FROM messages WHERE id LIKE 'plan-wake:intent_input:%' ORDER BY id"
        )
    ]


def _hear(db, kind: str, source_id: str) -> list[str]:
    db.commit()
    return person_input.hear(kind, source_id)


def test_running_task_owner_is_woken_with_the_words_and_a_stable_identity(db):
    owner, speaker = _thread(db, "owner"), _thread(db, "speaker")
    node = _task(db, owner, EXPORT)
    _say(db, speaker, SAID)

    woken = _hear(db, "message", "m1")

    assert woken == [f"plan-wake:intent_input:{node}:message:m1"]
    assert _wakes(db) == [("owner", woken[0])]
    row = db.execute("SELECT content,metadata FROM messages WHERE id=?", (woken[0],)).fetchone()
    line, payload = row["content"].split("\n", 1)
    assert EXPORT in line and "revision 1" in line and len(line.encode()) < 300
    assert json.loads(row["metadata"])["event_type"] == "plan.intent_input"
    facts = json.loads(payload)["intent_input"]
    assert facts["words"] == SAID and facts["source_kind"] == "message" and facts["said_at"]
    assert facts["title"] == EXPORT and facts["requirements_revision"] == 1


def test_replay_appends_nothing_new(db):
    owner, speaker = _thread(db, "owner"), _thread(db, "speaker")
    _task(db, owner, EXPORT)
    _say(db, speaker, SAID)

    first = _hear(db, "message", "m1")
    second = _hear(db, "message", "m1")

    assert first == second and len(_wakes(db)) == 1


def test_only_running_tasks_are_woken(db):
    speaker = _thread(db, "speaker")
    _task(db, _thread(db, "o-pending"), EXPORT, running=False, started=False)
    _task(db, _thread(db, "o-ready"), EXPORT, running=False)  # started, but not the selected task
    running = _task(db, _thread(db, "o-running"), EXPORT)
    _say(db, speaker, SAID)

    assert _hear(db, "message", "m1") == [f"plan-wake:intent_input:{running}:message:m1"]


def test_a_worker_step_resolves_to_its_canonical_task_and_its_owner(db):
    parent, worker, speaker = _thread(db, "parent"), _thread(db, "worker"), _thread(db, "speaker")
    canonical = _task(db, parent, EXPORT)
    _task(db, worker, EXPORT, intent_node_id=canonical)  # a running step that carries the parent's intent
    _say(db, speaker, SAID)

    woken = _hear(db, "message", "m1")

    assert woken == [f"plan-wake:intent_input:{canonical}:message:m1"]
    assert _wakes(db) == [("parent", woken[0])]


def test_the_thread_the_words_were_sent_to_is_not_woken(db):
    owner, other = _thread(db, "owner"), _thread(db, "other")
    _task(db, owner, EXPORT)
    other_node = _task(db, other, EXPORT)
    _say(db, owner, SAID)

    assert _hear(db, "message", "m1") == [f"plan-wake:intent_input:{other_node}:message:m1"]


def test_tasks_in_another_project_are_not_considered(db):
    _task(db, _thread(db, "elsewhere", project="proj-2"), EXPORT)
    _task(db, _thread(db, "unscoped", project=None), EXPORT)
    _say(db, _thread(db, "speaker"), SAID)

    assert _hear(db, "message", "m1") == []


def test_a_text_match_below_the_bar_wakes_nobody(db):
    _task(db, _thread(db, "owner"), EXPORT)
    _say(db, _thread(db, "speaker"), "Please rebuild it soon")  # one shared term: below Context & Memory's floor

    assert _hear(db, "message", "m1") == []
    assert person_input.TEXT_BAR[0] > 2  # the placeholder is stricter than the floor


def test_a_decision_answer_wakes_its_task_with_no_text_match_and_before_text_matches(db):
    speaker, linked_owner = _thread(db, "speaker"), _thread(db, "linked")
    linked = _task(db, linked_owner, "Unrelated nightly backup")
    text_node = _task(db, _thread(db, "texty"), EXPORT)
    _decision(db, "dec", thread=speaker, source_node=linked)

    woken = _hear(db, "plan_decision_answer", "dec")

    assert woken == [
        f"plan-wake:intent_input:{linked}:plan_decision_answer:dec:2",
        f"plan-wake:intent_input:{text_node}:plan_decision_answer:dec:2",
    ]


def test_a_decision_on_a_worker_step_reaches_the_canonical_owner(db):
    parent, worker = _thread(db, "parent"), _thread(db, "worker")
    canonical = _task(db, parent, "Unrelated nightly backup")
    step = _task(db, worker, "Unrelated nightly backup", intent_node_id=canonical)
    _decision(db, "dec", thread=worker, source_node=step, text="Use the second one")

    assert _hear(db, "plan_decision_answer", "dec") == [f"plan-wake:intent_input:{canonical}:plan_decision_answer:dec:2"]


def test_a_comment_on_a_document_a_task_asked_about_wakes_that_task(db):
    from plans import artifact_links

    owner, speaker = _thread(db, "owner"), _thread(db, "speaker")
    node = _task(db, owner, "Unrelated nightly backup")
    artifact = seed_document(db, speaker, name="ask.md")
    artifact_links.link(node, artifact, "attention", actor_id=owner, provenance="test", db=db)
    row, _ = add_comment(db, artifact, author_kind="user", author="Steve", author_id=None)
    db.commit()

    woken = _hear(db, "doc_comment", str(row["id"]))

    assert woken == [f"plan-wake:intent_input:{node}:doc_comment:{row['id']}"]


def test_at_most_three_wakes_explicit_first_then_by_score(db):
    speaker = _thread(db, "speaker")
    nodes = {
        "weak": _task(db, _thread(db, "o-weak"), "Rebuild invoice exporter tooling"),
        "strong": _task(db, _thread(db, "o-strong"), "Rebuild invoice exporter Friday"),
        "stronger": _task(db, _thread(db, "o-stronger"), "Rebuild invoice exporter Friday nightly"),
        "linked": _task(db, _thread(db, "o-linked"), "Unrelated nightly backup"),
        "third": _task(db, _thread(db, "o-third"), "Invoice exporter rebuild Friday"),
    }
    _decision(db, "dec", thread=speaker, source_node=nodes["linked"], text="Rebuild invoice exporter nightly Friday so totals reconcile")

    woken = _hear(db, "plan_decision_answer", "dec")

    assert len(woken) == person_input.MAX_WAKES == 3
    assert woken[0].split(":")[2] == nodes["linked"]
    ranked = [wake.split(":")[2] for wake in woken[1:]]
    assert ranked[0] == nodes["stronger"] and nodes["weak"] not in ranked


def test_the_words_are_bounded_in_the_wake(db):
    _task(db, _thread(db, "owner"), EXPORT)
    _say(db, _thread(db, "speaker"), SAID + " " + "very long " * 2000)

    (woken,) = _hear(db, "message", "m1")

    content = db.execute("SELECT content FROM messages WHERE id=?", (woken,)).fetchone()["content"]
    words = json.loads(content.split("\n", 1)[1])["intent_input"]["words"]
    assert words.startswith(SAID) and len(words.encode()) <= person_input.WORDS_MAX_BYTES


def _quote(db, node: str, text: str, *, removed: bool = False) -> None:
    db.execute(
        """INSERT INTO plan_node_intent_words
           (node_id,author,source_kind,source_id,quote,role,requirements_revision,added_at,removed_at,removed_in_revision)
           VALUES (?,'person','message','src',?,'bears_on',1,'t',?,?)""",
        (node, text, "t2" if removed else None, 1 if removed else None),
    )
    db.commit()


def test_a_quoted_word_counts_until_it_is_removed(db):
    node = _task(db, _thread(db, "owner"), EXPORT, criteria="done")
    _quote(db, node, "ledger reconciliation nightly")
    _say(db, _thread(db, "speaker"), "Rebuild invoice ledger reconciliation please")

    assert len(_hear(db, "message", "m1")) == 1

    db.execute("DELETE FROM messages WHERE id LIKE 'plan-wake:%'")
    db.execute("UPDATE plan_node_intent_words SET removed_at='t2',removed_in_revision=1")
    db.commit()

    assert _hear(db, "message", "m1") == []


def test_a_removed_quote_is_not_used_even_when_it_was_there_from_the_start(db):
    node = _task(db, _thread(db, "owner"), EXPORT, criteria="done")
    _quote(db, node, "ledger reconciliation nightly", removed=True)
    _say(db, _thread(db, "speaker"), "Rebuild invoice ledger reconciliation please")

    assert _hear(db, "message", "m1") == []


def test_the_hook_schedules_matching_and_returns_before_it_runs(db, monkeypatch):
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(person_input, "hear", lambda kind, source_id: seen.append((kind, source_id)) or [])

    async def send():
        person_input.arrived("message", "m1")
        assert seen == []  # not inline
        await asyncio.gather(*person_input._tasks)

    asyncio.run(send())

    assert seen == [("message", "m1")]


def test_the_hook_never_raises_with_no_loop_or_a_failing_matcher(db, monkeypatch):
    person_input.arrived("message", "m1")  # no running loop: dropped, not raised

    def broken(*_args):
        raise RuntimeError("db gone")

    monkeypatch.setattr(person_input, "hear", broken)

    async def send():
        person_input.arrived("message", "m1")
        await asyncio.gather(*person_input._tasks)

    asyncio.run(send())


def test_each_owner_words_point_hands_plans_the_arrival_and_survives_a_plans_failure(monkeypatch):
    from types import SimpleNamespace

    from jobs import corrections_recording as recording

    heard: list[tuple[str, str]] = []
    monkeypatch.setattr("jobs.corrections_realtime.notify", lambda *_args: None)
    monkeypatch.setattr("plans.public.person_words_arrived", lambda kind, source_id: heard.append((kind, source_id)))
    recording.notify_if_owner(
        SimpleNamespace(duplicate=False, message_id="m1"), role="user", content="Hello there", metadata="{}",
    )
    recording.notify_doc_comment("c1")
    recording.notify_decision_answer("d1", "free text")
    recording.notify_decision_answer("d2", "")

    assert heard == [("message", "m1"), ("doc_comment", "c1"), ("plan_decision_answer", "d1")]

    def broken(*_args):
        raise RuntimeError("plans gone")

    monkeypatch.setattr("plans.public.person_words_arrived", broken)
    recording.notify_doc_comment("c1")  # raises nothing


def test_the_wake_kind_is_a_named_plan_wake():
    assert plan_wake.INTENT_INPUT == "intent_input"
