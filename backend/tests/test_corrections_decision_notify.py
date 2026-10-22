"""The decision-answer route hands a free-text answer to the corrections worker once it committed.

``answer_plan_decision`` notifies on the application loop after the answer's
delivery succeeded, and only when the request carries free text: an Other
answer or a pick-any note, both sent as ``other_text``. ``plans`` itself
imports nothing from ``jobs``.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from plans import service
from tests.test_correction_notes import _version
from tests.test_plan_decision_answer import _client, _raised_decision
from tests.test_plan_decision_pick_any import _draft_any, _raise
from tests.test_plan_decisions import _DbProxy, _task, db  # noqa: F401 - shared fixture

CORRECTION = {"label": "correction", "truncated": False, "original_tokens": 9, "failure_reason": "none"}


@pytest.fixture
def seen(monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr("jobs.corrections_realtime.notify", lambda kind, source: calls.append((kind, source)))
    # Plans hears the same answer on a pool thread; with ``get_db`` patched to the one shared
    # fixture connection it would race the route's own pool job on it. Its tests are Plans'.
    monkeypatch.setattr("plans.public.person_words_arrived", lambda kind, source: None)
    return calls


def _post(client, decision, **answer):
    return client.post(
        f"/api/plans/decisions/{decision['id']}/answer",
        json={"expected_revision": decision["revision"], "mutation_id": answer.pop("mutation_id", "m-1"), **answer},
    )


@pytest.mark.parametrize(
    ("answer", "notified"),
    [({"option_id": "A"}, False), ({"option_id": "other", "other_text": "Neither."}, True)],
)
def test_the_route_notifies_only_an_answer_that_carries_text(db, monkeypatch, seen, answer, notified):
    _plan, _source, decision = _raised_decision(db)

    assert _post(_client(db, monkeypatch), decision, **answer).status_code == 200

    assert seen == ([("plan_decision_answer", decision["id"])] if notified else [])


def test_a_pick_any_note_is_notified_like_an_other_answer(db, monkeypatch, seen):
    from plans import decisions

    plan = service.ensure_plan("thread", "owner", db=db)
    source = _task(plan.id, "Pick tools", db=db)
    db.commit()
    decision = _raise(db, decisions, _draft_any(db, decisions, plan, source))

    response = _post(_client(db, monkeypatch), decision, option_ids=["A"], other_text="Not web search.")

    assert response.status_code == 200
    assert seen == [("plan_decision_answer", decision["id"])]


def test_a_refused_answer_notifies_nothing(db, monkeypatch, seen):
    _plan, _source, decision = _raised_decision(db)

    stale = {**decision, "revision": decision["revision"] + 99}
    response = _post(_client(db, monkeypatch), stale, option_id="other", other_text="Neither.")

    assert response.status_code == 409 and seen == []


def test_a_failing_notify_never_fails_the_committed_answer(db, monkeypatch):
    def broken(*_args):
        raise RuntimeError("worker gone")

    monkeypatch.setattr("jobs.corrections_realtime.notify", broken)
    _plan, _source, decision = _raised_decision(db)

    response = _post(_client(db, monkeypatch), decision, option_id="other", other_text="Neither.")

    assert response.status_code == 200 and response.json()["decision"]["state"] == "answered"


def test_a_replayed_answer_notifies_again_and_writes_no_second_note(db, monkeypatch, seen):
    """A replay re-sends the same subject; the fact's unique ``dedupe_key`` stops a second fact and note."""
    from jobs.corrections_recording import _record_fact, candidate_for_source

    monkeypatch.setattr("infra.passive_changes.clock_event", lambda *_args: None)
    monkeypatch.setattr("infra.database.get_db", lambda: _DbProxy(db))
    _plan, _source, decision = _raised_decision(db)
    client = _client(db, monkeypatch)
    for _ in range(2):
        assert _post(client, decision, option_id="other", other_text="Neither.", mutation_id="replay").status_code == 200
    db.commit()

    recorded = [
        _record_fact(replace(candidate_for_source(kind, source), arrived=True), CORRECTION, _version())
        for kind, source in seen
    ]

    assert len(seen) == 2 and recorded == [True, False]
    notes = [
        json.loads(row[0]) for row in db.execute(
            "SELECT details_json FROM context_change_journal WHERE object_kind='thread_context'"
        )
    ]
    assert [note["correction_subject"] for note in notes if note.get("kind") == "correction_note"] == [
        f"plan-decision-answer:{decision['id']}:{decision['revision'] + 1}",
    ]
