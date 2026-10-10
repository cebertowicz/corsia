"""An operational store fault must not become deduped fact loss at receipt.

The candidate first caught every ``Exception`` in ``_write_raw_facts``, logged a
warning and counted ``mapping_failed``, so the delivery transaction committed with
its raw facts rolled back to the savepoint. A redelivery of that id is answered
``duplicate``, so the facts were never written again: a durability failure recorded
as a mapping statistic, and a ``200`` that made this delivery look complete. The
``500`` is not what puts a delivery on GitHub's record — a successful delivery is
recorded too and either kind can be redelivered by hand, since GitHub never starts a
redelivery itself. What differs is local: the ``200`` left a durable receipt whose
dedupe answers every later copy of that id ``duplicate``, so the rolled-back facts
stayed lost, while the abort leaves no receipt, so re-entry of the same id is
admitted and writes them.

Only the store's explicit ``OutcomeInputRefusal`` from its deliberate credential
or kept-attribute budget guard establishes an input refusal. Incidental
``ValueError``, SQL constraints, operational database errors and writer faults
abort the whole admission transaction, leave no delivery row, and permit the same
delivery id to be admitted successfully once the fault is repaired. A malformed
timestamp generated after valid source mapping is a producer invariant fault too.

Intentional refusals retain their separate proof: a ``PayloadRefusal`` and an
explicit canonical store refusal are counted ``mapping_failed`` while the receipt
commits. Generated invalid keys instead require outer rollback and same-id recovery.
The mapper's own half of that split is proven here too, because the two bands now
have to stay different: explicit ``PayloadRefusal`` describes GitHub's input,
while an incidental exception out of the mapper is a defect in this code, and it
aborts before the ingress transaction opens so no receipt is left behind.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from typing import Any

import pytest

import events.connectors.github_admission as admission_module
import events.connectors.github_outcomes as github_outcomes
import events.connectors.github_webhook as webhook_module
import infra.outcome_events as outcome_events_module
from events.connectors.github_admission import admit_github_delivery_async
from events.connectors.github_outcomes import map_github_outcomes
from infra import drain
from test_github_admission_cancellation import _Scheduler
from test_github_raw_facts_at_admission import _counters, _deliveries, _facts, delivery
from webhook_ingress_fixtures import (  # noqa: F401  (_isolated_db is the route's fixture)
    WEBHOOK_SECRET,
    _isolated_db,
    _make_signature,
)

# Three drafts, so the fault lands after a real first fact insert.
FAULTY_DELIVERY = "pr_introduced_by_merged"


@pytest.fixture(autouse=True)
def _own_admission_count(monkeypatch):
    """Each probe counts only its own admissions."""
    monkeypatch.setattr(drain, "_turn_admissions", 0)


@pytest.fixture
def scheduler(monkeypatch):
    stub = _Scheduler()
    monkeypatch.setattr(webhook_module, "schedule_github_webhook", stub)
    return stub


class _FaultyWriter:
    """Call the real store once, then raise ``failure``: the second write never happened."""

    def __init__(self, failure: BaseException) -> None:
        self._real = outcome_events_module.record_outcome_event
        self._failure = failure
        self.calls: list[str] = []

    def __call__(self, db, **fields: Any) -> Any:
        if self.calls:
            return self._real(db, **fields)  # the store works again for a later delivery
        self._real(db, **fields)
        self.calls.append(str(fields["dedupe_key"]))
        raise self._failure


def _assert_nothing_survives(main_db, fault: type[BaseException]) -> None:
    """The delivery, its facts and any counter must all be gone together."""
    assert _deliveries(main_db) == {}, (
        f"a {fault.__name__} is not an input refusal: the delivery row must roll back "
        "too, or its facts are lost to dedupe forever"
    )
    assert _facts(main_db) == {}
    assert _counters(main_db) == {}, (
        f"an operational {fault.__name__} is not a mapping failure and must not be "
        "counted as one"
    )
    assert drain._turn_admissions == 0, "the admission is released whatever happened"


def _assert_written_once(main_db, event_type, payload, delivery_id):
    """Once the store works again, the same delivery id lands its facts exactly once."""
    expected = {draft.dedupe_key for draft in map_github_outcomes(event_type, payload)}
    assert len(expected) == 3, "the fixture maps three facts"
    assert set(_facts(main_db)) == expected, "all three facts exist, exactly once"
    assert _counters(main_db) == {}, "a restored write is not a failure to count"
    assert _deliveries(main_db) == {delivery_id: "dispatched"}
    assert drain._turn_admissions == 0


async def test_an_operational_store_failure_aborts_the_admission_and_can_be_redelivered(
    main_db, monkeypatch, scheduler,
):
    event_type, payload = delivery(FAULTY_DELIVERY)
    writer = _FaultyWriter(sqlite3.OperationalError("database is locked"))
    monkeypatch.setattr(outcome_events_module, "record_outcome_event", writer)

    with pytest.raises(sqlite3.OperationalError):
        await admit_github_delivery_async(event_type, payload, "d-locked")

    _assert_nothing_survives(main_db, sqlite3.OperationalError)
    assert len(writer.calls) == 1, "the first fact really was written, then rolled back"
    assert scheduler.calls == [], "nothing is scheduled when the record transaction aborted"

    outcome = await admit_github_delivery_async(event_type, payload, "d-locked")

    assert outcome.content["status"] == "ok", "the same delivery id must succeed once restored"
    _assert_written_once(main_db, event_type, payload, "d-locked")


@pytest.mark.parametrize("fault", [RuntimeError, ValueError, sqlite3.IntegrityError])
async def test_an_unexpected_writer_fault_aborts_the_admission_and_can_be_redelivered(
    main_db, monkeypatch, scheduler, fault,
):
    event_type, payload = delivery(FAULTY_DELIVERY)
    writer = _FaultyWriter(fault("the writer broke"))
    monkeypatch.setattr(outcome_events_module, "record_outcome_event", writer)

    with pytest.raises(fault, match="the writer broke"):
        await admit_github_delivery_async(event_type, payload, "d-writer-fault")

    _assert_nothing_survives(main_db, fault)
    assert len(writer.calls) == 1
    assert scheduler.calls == []

    outcome = await admit_github_delivery_async(event_type, payload, "d-writer-fault")

    assert outcome.content["status"] == "ok", "the same delivery id must succeed once restored"
    _assert_written_once(main_db, event_type, payload, "d-writer-fault")


class _FaultyMapper:
    """Run the real mapper, then raise ``failure`` while ``failing`` is set."""

    def __init__(self, failure: BaseException) -> None:
        self._real = github_outcomes.map_github_outcomes
        self._failure = failure
        self.failing = True
        self.calls = 0

    def __call__(self, event_type: str, payload: dict[str, Any]) -> Any:
        drafts = self._real(event_type, payload)
        self.calls += 1
        if self.failing:
            raise self._failure
        return drafts


@pytest.mark.parametrize("fault", [KeyError, TypeError, AttributeError])
async def test_a_mapper_defect_aborts_before_the_receipt_so_the_id_can_be_admitted(
    main_db, monkeypatch, scheduler, caplog, fault,
):
    """Any exception outside the documented refusals is our defect, not GitHub's input.

    The candidate caught it with everything else: the delivery committed with its
    receipt, every later copy of that id answered ``duplicate``, and the rolled-back
    facts were never written again. Aborting before the transaction opens is what makes
    the next copy an admission rather than a dedupe.
    """
    event_type, payload = delivery(FAULTY_DELIVERY)
    mapper = _FaultyMapper(fault("'str' object has no attribute 'get'"))
    monkeypatch.setattr(github_outcomes, "map_github_outcomes", mapper)

    with pytest.raises(fault), caplog.at_level(
        logging.ERROR, logger=admission_module.log.name,
    ):
        await admit_github_delivery_async(event_type, payload, "d-mapper-defect")

    _assert_nothing_survives(main_db, fault)
    assert mapper.calls == 1, "the fault was raised after the real mapping, not before it"
    assert scheduler.calls == [], "nothing is scheduled when the mapper aborts the delivery"
    logged = "\n".join(f"{record.levelname} {record.getMessage()}" for record in caplog.records)
    assert "ERROR" in logged and "d-mapper-defect" in logged and fault.__name__ in logged
    assert "'str' object has no attribute 'get'" not in logged
    assert payload["repository"]["full_name"] not in logged, (
        "the log carries the delivery id and the exception's class name, never the payload"
    )

    mapper.failing = False
    outcome = await admit_github_delivery_async(event_type, payload, "d-mapper-defect")

    assert outcome.content["status"] == "ok", (
        "the same delivery id must be admitted once the mapper works again"
    )
    _assert_written_once(main_db, event_type, payload, "d-mapper-defect")


async def test_a_refused_payload_still_gets_its_receipt_and_its_count(
    main_db, caplog, scheduler,
):
    """The other band: a field the mapper needs is missing, so GitHub's input is the problem."""
    payload = {"action": "opened", "repository": {"full_name": "o/r"}, "pull_request": {}}
    with pytest.raises(admission_module.PayloadRefusal):
        map_github_outcomes("pull_request", payload)

    with caplog.at_level(logging.WARNING, logger=admission_module.log.name):
        outcome = await admit_github_delivery_async("pull_request", payload, "d-refused-input")

    assert outcome.content["status"] == "ok", "a refused payload is still a delivery we received"
    assert _deliveries(main_db) == {"d-refused-input": "dispatched"}, (
        "the receipt is written and dispatched even though no fact landed"
    )
    assert _facts(main_db) == {}
    assert _counters(main_db) == {("mapping_failed", "pull_request"): 1}, (
        "the refusal is counted where a reader can see the facts did not land"
    )
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "d-refused-input" in logged and "PayloadRefusal" in logged
    assert payload["repository"]["full_name"] not in logged


def _rows(db_path) -> dict[str, list]:
    conn = sqlite3.connect(str(db_path))
    try:
        return {
            "deliveries": [
                row[0] for row in conn.execute("SELECT delivery_id FROM webhook_deliveries")
            ],
            "facts": [
                row[0] for row in conn.execute("SELECT dedupe_key FROM outcome_events")
            ],
            "counters": [
                row[0] for row in conn.execute("SELECT counter FROM outcome_event_counters")
            ],
        }
    finally:
        conn.close()


@pytest.mark.parametrize("fault", [sqlite3.OperationalError, ValueError, sqlite3.IntegrityError])
def test_the_route_answers_500_and_the_same_delivery_is_admitted_afterwards(
    _isolated_db, monkeypatch, fault,
):
    """The real webhook route, not the admission function: a locked store fails loudly.

    Swallowing the fault made the route answer 200, so the delivery committed with a
    durable receipt while its facts were gone with the rolled-back savepoint, and every
    later copy of that id — including a manual redelivery, which is how any recorded
    delivery comes back since GitHub never redelivers on its own — was answered
    ``duplicate``. The existing 500 contract does not create or preserve GitHub's record
    of the delivery; it withholds our local receipt, so re-entry of the same id is
    admitted instead of deduped. No new handler is added for it, and no test here claims
    the GitHub-side redelivery itself is exercised.
    """
    from fastapi.testclient import TestClient
    from main import app

    monkeypatch.setattr("api.webhooks._get_webhook_secret", lambda: WEBHOOK_SECRET)

    def schedule(event_type, payload, *, delivery_id=None, on_settled=None):
        if on_settled is not None:
            on_settled()
        return "webhook.github.pr_opened"

    monkeypatch.setattr(webhook_module, "schedule_github_webhook", schedule)
    client = TestClient(app, raise_server_exceptions=False)

    # A recorded pull_request object, so the outcome mapper really produces a draft
    # and the store is reached: the shared webhook fixture is too thin to map.
    event_type, payload = delivery("pr_opened")
    body = json.dumps(payload).encode()
    delivery_id = f"d-route-{uuid.uuid4()}"
    headers = {
        "X-GitHub-Event": event_type,
        "X-GitHub-Delivery": delivery_id,
        "X-Hub-Signature-256": _make_signature(body),
        "Content-Type": "application/json",
    }

    with monkeypatch.context() as locked_store:
        locked_store.setattr(
            outcome_events_module, "record_outcome_event",
            _FaultyWriter(fault("the store failed"))
        )
        locked = client.post("/webhooks/github", content=body, headers=headers)

    assert locked.status_code == 500, "a locked store must not be answered 200"
    assert _rows(_isolated_db) == {"deliveries": [], "facts": [], "counters": []}, (
        "the delivery row, its facts and any counter roll back together"
    )
    assert drain._turn_admissions == 0, "the aborted admission releases what it took"

    redelivered = client.post("/webhooks/github", content=body, headers=headers)

    assert redelivered.status_code == 200, "the same delivery id is admitted once the store works"
    assert redelivered.json()["status"] == "ok"
    rows = _rows(_isolated_db)
    assert rows["deliveries"] == [delivery_id]
    assert len(rows["facts"]) == 1, "the delivery's one fact exists exactly once"
    assert rows["counters"] == [], "a restored write is not a failure to count"
    assert drain._turn_admissions == 0
