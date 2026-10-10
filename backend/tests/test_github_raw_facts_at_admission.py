"""Raw GitHub facts are written by the delivery's own admission transaction.

Every probe calls the real ``record_delivery_for_admission``: the delivery row
and its raw ``outcome_events`` rows commit together or not at all, with no
background leg, no network read and no in-memory tally.
"""

from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

import events.connectors.github_outcomes as outcomes_module
import infra.outcome_events as outcome_events_module
from events.connectors.github_admission import record_delivery_for_admission
from events.connectors.github_outcomes import OutcomeDraft, map_github_outcomes

FIXTURES = Path(__file__).parent / "fixtures/github_outcomes"


def delivery(name: str) -> tuple[str, dict[str, Any]]:
    recorded = json.loads((FIXTURES / f"{name}.json").read_text())
    return recorded["event"], recorded["payload"]


def admit(event_type: str, payload: dict[str, Any], delivery_id: str):
    repo = payload.get("repository", {}).get("full_name")
    return record_delivery_for_admission(
        event_type, payload.get("action"), delivery_id, repo, payload,
    )


def _facts(db: sqlite3.Connection) -> dict[str, tuple[Any, Any, Any]]:
    return {
        row[0]: (row[1], row[2], row[3])
        for row in db.execute(
            "SELECT dedupe_key, agent_thread_id, attribution, project_id FROM outcome_events"
        )
    }


def _counters(db: sqlite3.Connection) -> dict[tuple[str, str], int]:
    return {
        (row[0], row[1]): row[2]
        for row in db.execute("SELECT counter, event_name, n FROM outcome_event_counters")
    }


def _deliveries(db: sqlite3.Connection) -> dict[str, str]:
    return {
        row[0]: row[1]
        for row in db.execute("SELECT delivery_id, status FROM webhook_deliveries")
    }


def _hold_new_work(monkeypatch) -> None:
    from automation import self_restart

    monkeypatch.setattr(self_restart, "restart_holds_new_work", lambda *, db=None: True)


def test_a_normal_delivery_writes_its_facts_once_and_unattributed(main_db):
    event_type, payload = delivery("pr_opened")
    expected = {draft.dedupe_key for draft in map_github_outcomes(event_type, payload)}
    assert expected, "the fixture maps to at least one fact"

    record, _, held = admit(event_type, payload, "d-normal")

    assert record.created and not held
    facts = _facts(main_db)
    assert set(facts) == expected
    assert set(facts.values()) == {(None, None, None)}, (
        "a raw fact carries no agent, attribution or project until derivation"
    )
    assert _counters(main_db) == {}


def test_a_duplicate_delivery_writes_nothing_and_counts_nothing(main_db):
    event_type, payload = delivery("pr_opened")
    admit(event_type, payload, "d-dup")
    before = _facts(main_db)
    assert before, "the first delivery wrote its facts"

    record, _, _ = admit(event_type, payload, "d-dup")

    assert not record.created
    assert _facts(main_db) == before
    assert _counters(main_db) == {}, "a refused delivery is not a duplicate fact write"


def test_a_comment_seen_by_webhook_and_poller_writes_no_facts(main_db):
    payload = {
        "action": "created",
        "repository": {"full_name": "o/r"},
        "issue": {"number": 7},
        "comment": {"id": 9001, "body": "hello"},
    }

    first, _, _ = admit("issue_comment", payload, "d-comment")
    second, _, _ = admit("issue_comment", payload, "gh-notifications:issue_comment:9001")

    assert first.created and not second.created, "one comment object, one delivery row"
    assert _facts(main_db) == {}
    assert _counters(main_db) == {}


def test_a_payload_the_mapper_refuses_is_counted_and_still_recorded(main_db):
    payload = {"action": "opened", "repository": {"full_name": "o/r"}, "pull_request": {}}
    from events.connectors.github_outcome_input import PayloadRefusal

    with pytest.raises(PayloadRefusal):
        map_github_outcomes("pull_request", payload)

    record, _, _ = admit("pull_request", payload, "d-unmappable")

    assert record.created, "a mapping failure never refuses the delivery"
    assert _deliveries(main_db) == {"d-unmappable": "received"}
    assert _facts(main_db) == {}
    assert _counters(main_db) == {("mapping_failed", "pull_request"): 1}


def test_an_invalid_generated_key_aborts_after_a_real_write_and_can_be_reentered(
    main_db, monkeypatch,
):
    event_type, payload = delivery("pr_opened")
    good = map_github_outcomes(event_type, payload)
    invalid = OutcomeDraft(
        dedupe_key=" ", event_name="github.pr.opened", ts=good[0].ts,
        subject_key=good[0].subject_key,
    )
    real_write = outcome_events_module.record_outcome_event
    written = []

    def track_write(db, **fields):
        result = real_write(db, **fields)
        written.append(fields["dedupe_key"])
        return result

    with monkeypatch.context() as patch:
        patch.setattr(outcomes_module, "map_github_outcomes", lambda *_: [*good, invalid])
        patch.setattr(outcome_events_module, "record_outcome_event", track_write)
        with pytest.raises(sqlite3.IntegrityError):
            admit(event_type, payload, "d-invalid-generated-key")

    assert written == [good[0].dedupe_key], "a real first write preceded the SQL invariant fault"
    assert _deliveries(main_db) == {}, "producer corruption must leave no deduping receipt"
    assert _facts(main_db) == {}, "the real first write rolls back with the outer transaction"
    assert _counters(main_db) == {}, "an invariant fault is not a mapping failure"

    record, _, _ = admit(event_type, payload, "d-invalid-generated-key")
    assert record.created
    assert set(_facts(main_db)) == {draft.dedupe_key for draft in good}
    assert _deliveries(main_db) == {"d-invalid-generated-key": "received"}
    duplicate, _, _ = admit(event_type, payload, "d-invalid-generated-key")
    assert not duplicate.created and _counters(main_db) == {}


class _Interrupted(BaseException):
    """Stands in for an interrupt between the fact writes and the commit."""


def test_a_failed_transaction_leaves_nothing_and_a_redelivery_writes_once(
    main_db, monkeypatch,
):
    event_type, payload = delivery("pr_opened")
    expected = {draft.dedupe_key for draft in map_github_outcomes(event_type, payload)}
    real = outcome_events_module.record_outcome_event

    def write_then_interrupt(db, **fields):
        real(db, **fields)
        raise _Interrupted

    with monkeypatch.context() as patched:
        patched.setattr(outcome_events_module, "record_outcome_event", write_then_interrupt)
        with pytest.raises(_Interrupted):
            admit(event_type, payload, "d-lost-commit")

    assert _deliveries(main_db) == {}, "no delivery row without its transaction"
    assert _facts(main_db) == {}
    assert _counters(main_db) == {}

    record, _, _ = admit(event_type, payload, "d-lost-commit")

    assert record.created, "the same delivery id is admitted once its first try left nothing"
    assert set(_facts(main_db)) == expected
    assert _counters(main_db) == {}


async def test_a_held_delivery_writes_its_facts_at_hold_and_replay_writes_none(
    main_db, monkeypatch,
):
    from events.connectors.github_webhook import dispatch_github_webhook_for_replay

    event_type, payload = delivery("issue_opened")
    expected = {draft.dedupe_key for draft in map_github_outcomes(event_type, payload)}
    assert expected
    _hold_new_work(monkeypatch)

    record, _, held = admit(event_type, payload, "d-held")

    assert record.created and held
    assert _deliveries(main_db) == {"d-held": "held_restart"}
    assert set(_facts(main_db)) == expected, "the facts do not wait for the replay"

    await dispatch_github_webhook_for_replay(event_type, payload, delivery_id="d-held")

    assert set(_facts(main_db)) == expected
    assert _counters(main_db) == {}, "replay does not try the facts a second time"


def test_repository_case_is_normalized_once_across_every_key_and_relation():
    event_type, payload = delivery("pr_closes_merged")
    payload = copy.deepcopy(payload)
    lowered = payload["repository"]["full_name"].lower()
    payload["repository"]["full_name"] = payload["repository"]["full_name"].upper()

    drafts = map_github_outcomes(event_type, payload)

    assert drafts
    for draft in drafts:
        assert draft.dedupe_key == draft.dedupe_key.lower(), draft.dedupe_key
        assert draft.subject_key == draft.subject_key.lower(), draft.subject_key
        assert draft.attributes["repo"] == lowered
    closes = [d for d in drafts if d.event_name == "github.pr.closes_issue"]
    assert closes, "the fixture states a closes relation"
    assert {d.attributes["issue_repo"] for d in closes} == {lowered}


def _attributes(db: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    return {
        row[0]: json.loads(row[1])
        for row in db.execute("SELECT dedupe_key, attributes FROM outcome_events")
    }


OWNER = "0a1b2c3d"
OTHER = "4e5f6a7b"


def _opened_with(body: str, login: Any) -> tuple[str, dict[str, Any]]:
    event_type, payload = delivery("pr_opened")
    payload = copy.deepcopy(payload)
    payload["pull_request"]["body"] = body
    payload["pull_request"]["user"] = {"login": login}
    return event_type, payload


def test_the_same_fact_from_a_second_delivery_is_ignored_and_counted(main_db):
    event_type, payload = delivery("pr_opened")
    admit(event_type, payload, "d-first")
    before = _facts(main_db)

    record, _, _ = admit(event_type, payload, "d-second")

    assert record.created, "a different delivery id is a new delivery"
    assert _facts(main_db) == before
    assert _counters(main_db) == {("duplicate_ignored", "github.pr.opened"): 1}


def test_a_declared_owner_is_recorded_as_observed_and_never_becomes_attribution(
    main_db, monkeypatch,
):
    from events.connectors import github_ownership

    main_db.execute(
        "INSERT INTO threads (id, user_id, title, created_at, updated_at, agent_enabled)"
        " VALUES (?, 'steve', 'Self-Improvement Infrastructure', 't', 't', 1)",
        (OWNER,),
    )
    main_db.commit()
    monkeypatch.setattr(github_ownership, "_OWN_LOGIN", "corsia-bot")
    event_type, payload = _opened_with(f"Summary.\n\nOwner thread ID: {OWNER}\n", "corsia-bot")

    admit(event_type, payload, "d-declared")

    (attributes,) = _attributes(main_db).values()
    assert attributes["author_login"] == "corsia-bot"
    assert attributes["declared_owner_id"] == OWNER
    assert attributes["declaration_count"] == 1
    assert set(_facts(main_db).values()) == {(None, None, None)}, (
        "a matching login, title prefix and live agent thread still infer nothing"
    )


def test_two_declarations_keep_the_count_and_name_no_owner(main_db):
    body = f"Owner thread ID: {OWNER}\nOwner thread ID: {OTHER}\n"
    event_type, payload = _opened_with(body, "corsia-bot")

    admit(event_type, payload, "d-two-owners")

    (attributes,) = _attributes(main_db).values()
    assert attributes["declaration_count"] == 2
    assert "declared_owner_id" not in attributes


def test_another_account_claiming_an_owner_is_stored_as_its_own_claim(main_db):
    event_type, payload = _opened_with(f"Owner thread ID: {OWNER}\n", "mallory")

    admit(event_type, payload, "d-spoof")

    (attributes,) = _attributes(main_db).values()
    assert attributes["author_login"] == "mallory", "the claim keeps the name of who made it"
    assert attributes["declared_owner_id"] == OWNER
    assert set(_facts(main_db).values()) == {(None, None, None)}


def test_no_pr_prose_is_stored_and_an_unsafe_login_is_dropped_and_counted(main_db):
    secret = "ghp_" + "a" * 36
    body = f"Token {secret} leaked here.\nOwner thread ID: {OWNER}\n"
    event_type, payload = _opened_with(body, "not a login; drop me")
    payload["pull_request"]["title"] = f"[Bugs] {secret}"

    admit(event_type, payload, "d-prose")

    (attributes,) = _attributes(main_db).values()
    assert not {"title", "body", "author_login"} & set(attributes)
    assert secret not in json.dumps(attributes)
    assert attributes["declared_owner_id"] == OWNER
    assert _counters(main_db) == {("attribute_dropped", "github.pr.opened"): 1}


def _stored_attributes(db: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    return {
        row[0]: json.loads(row[1])
        for row in db.execute("SELECT dedupe_key, attributes FROM outcome_events")
    }


# ------------------------------------------------ the ``none`` sentinel contract
#
# ``none`` is registered so a run whose payload carried no event or conclusion can
# be told apart from one that carried a value we do not model. That only holds while
# ``none`` means exactly one thing: the key was absent. A value that is present but
# unusable - null, empty string, False, 0 - is a different fact, and rewriting it
# into ``none`` turns a bad payload into a plausible row. These probes map a real
# fixture and then write it through admission, so the contract is proven on the path
# a delivery actually takes, not on hand-built attribute dicts.

_SENTINELS = [
    ("workflow_run", "event", "github.workflow_run.completed"),
    ("workflow_run", "conclusion", "github.workflow_run.completed"),
    ("check_suite", "conclusion", "github.check_suite.completed"),
]


def _sentinel_payload(container: str, field: str, value: Any = None, *, drop: bool = False):
    name = "workflow_run_success" if container == "workflow_run" else "check_suite_success"
    event_type, payload = delivery(name)
    if drop:
        payload[container].pop(field, None)
    else:
        payload[container][field] = value
    return event_type, payload


@pytest.mark.parametrize("container,field,outcome_event", _SENTINELS)
def test_a_missing_sentinel_field_is_recorded_as_none(main_db, container, field, outcome_event):
    event_type, payload = _sentinel_payload(container, field, drop=True)
    mapped = map_github_outcomes(event_type, payload)
    assert [draft.attributes[field] for draft in mapped] == ["none"], (
        "the mapper fills none for a key that is not there, and for nothing else"
    )

    admit(event_type, payload, "d-sentinel-absent")

    assert _counters(main_db) == {}, "an absent field is not a dropped attribute"
    assert list(_stored_attributes(main_db).values())[0][field] == "none"


@pytest.mark.parametrize("container,field,value", [
    ("workflow_run", "event", "pull_request_target"),
    ("workflow_run", "event", "dynamic"),
    ("workflow_run", "conclusion", "success"),
    ("check_suite", "conclusion", "action_required"),
])
def test_a_supported_sentinel_value_is_recorded_verbatim(main_db, container, field, value):
    """A value the allowlist registers must reach the row exactly as the payload said it."""
    event_type, payload = _sentinel_payload(container, field, value)

    admit(event_type, payload, "d-sentinel-kept")

    stored = list(_stored_attributes(main_db).values())[0]
    assert stored[field] == value
    assert _counters(main_db) == {}


@pytest.mark.parametrize("container,field,outcome_event", _SENTINELS)
@pytest.mark.parametrize("bad", [None, "", False, 0])
def test_a_present_invalid_sentinel_is_dropped_and_never_rewritten_to_none(
    main_db, container, field, outcome_event, bad,
):
    event_type, payload = _sentinel_payload(container, field, bad)

    admit(event_type, payload, "d-sentinel-invalid")

    stored = list(_stored_attributes(main_db).values())
    assert stored, "the run itself is still a fact worth a row"
    assert field not in stored[0], f"a present {bad!r} is not an absent key"
    assert _counters(main_db) == {("attribute_dropped", outcome_event): 1}, (
        "the loss is counted, which is the only place it survives"
    )


def test_an_unregistered_trigger_is_dropped_and_counted_on_the_written_row(main_db):
    """A trigger we have never modelled leaves no key; the per-event total is the signal."""
    event_type, payload = _sentinel_payload("workflow_run", "event", "some_future_trigger")

    admit(event_type, payload, "d-sentinel-unknown")

    stored = list(_stored_attributes(main_db).values())[0]
    assert "event" not in stored
    assert _counters(main_db) == {("attribute_dropped", "github.workflow_run.completed"): 1}


@pytest.mark.parametrize("bad", [None, 0, -1, "", "1", 1.5, True, False])
def test_a_malformed_attempt_is_refused_as_input_and_never_becomes_attempt_one(main_db, bad):
    """A guessed attempt is a guessed identity, and the receipt must not hide it.

    ``attempt`` sits inside the run's dedupe key, so rewriting a present 0 into 1 wrote a row
    for an identity the payload never stated - and the run's genuine first attempt would then
    count ``duplicate_ignored`` against a row it did not create. This is a payload problem, so
    it goes through the band an unmappable payload already uses: counted as ``mapping_failed``,
    delivery recorded, no facts. Not one of these values may produce a row, and none of them
    may reach the store's own refusal band.
    """
    event_type, payload = delivery("workflow_run_success")
    payload["workflow_run"]["run_attempt"] = bad
    with pytest.raises(outcomes_module.shape.PayloadRefusal):
        map_github_outcomes(event_type, payload)

    delivery_id = f"d-attempt-invalid-{type(bad).__name__}"
    record, _, _ = admit(event_type, payload, delivery_id)

    assert record.created, "a mapping failure never refuses the delivery"
    assert _deliveries(main_db) == {delivery_id: "received"}
    assert _facts(main_db) == {}, "no row may claim an attempt this payload did not state"
    assert _counters(main_db) == {("mapping_failed", event_type): 1}


def test_a_failure_that_cannot_be_counted_rolls_the_delivery_back(main_db, monkeypatch):
    payload = {"action": "opened", "repository": {"full_name": "o/r"}, "pull_request": {}}

    def refuse(*_args, **_kwargs):
        raise sqlite3.OperationalError("counter table is unwritable")

    with monkeypatch.context() as patched:
        patched.setattr(outcome_events_module, "count_outcome_event", refuse)
        with pytest.raises(sqlite3.OperationalError):
            admit("pull_request", payload, "d-uncounted")

    assert _deliveries(main_db) == {}, "no delivery row with neither facts nor a count"
    assert _counters(main_db) == {}

    record, _, _ = admit("pull_request", payload, "d-uncounted")

    assert record.created
    assert _deliveries(main_db) == {"d-uncounted": "received"}
    assert _counters(main_db) == {("mapping_failed", "pull_request"): 1}
