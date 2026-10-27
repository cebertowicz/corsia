"""Outcome events: table, dedupe, retraction, attribute allowlist and secret intake (spine item 1)."""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

import infra.outcome_event_attributes as attribute_module
from infra.database import initialize_schema
from infra.database_migrations import migrate_to_current
from infra.outcome_event_attributes import ATTRIBUTE_ALLOWLISTS, MAX_ATTRIBUTES_BYTES
from infra.outcome_events import (
    count_outcome_event,
    record_outcome_event,
    retract_outcome_event,
)
from infra.schema import SCHEMA_VERSION
from tests.schema_rewind import rewind_to_v88_policy

EVENT = "test.outcome.recorded"
TS = "2026-09-30T00:00:00+00:00"


@pytest.fixture
def db():
    with closing(sqlite3.connect(":memory:")) as conn:
        initialize_schema(conn)
        yield conn


def _write(db, key, **kw):
    kw.setdefault("event_name", EVENT)
    kw.setdefault("ts", TS)
    return record_outcome_event(
        db, dedupe_key=key, source="corsia", provenance="observed", **kw,
    )


def _attributes(db):
    return dict(db.execute("SELECT dedupe_key, attributes FROM outcome_events"))


def _counters(db):
    return dict(db.execute(
        "SELECT counter, sum(n) FROM outcome_event_counters GROUP BY counter"
    ))


def _outcome_schema(conn):
    return {
        name: " ".join((sql or "").split()) for name, sql in conn.execute(
            "SELECT name,sql FROM sqlite_master WHERE name LIKE '%outcome_event%'"
        )
    }


def test_startup_migration_from_76_matches_the_fresh_schema(tmp_path):
    path = tmp_path / "v76.db"
    with closing(sqlite3.connect(path)) as conn:
        initialize_schema(conn)
        conn.execute("DROP TABLE outcome_events")
        conn.execute("DROP TABLE outcome_event_counters")
        rewind_to_v88_policy(conn)
        conn.execute('ALTER TABLE project_vendor_accounts DROP COLUMN switch_on_limit')
        conn.execute("DROP TABLE system_bulletins")
        conn.execute("DROP TABLE system_bulletin_identity")
        conn.execute("PRAGMA user_version=76")
        conn.commit()
    with closing(sqlite3.connect(path)) as conn:
        result = migrate_to_current(conn, SCHEMA_VERSION)
        assert (result.migrated, result.from_version, result.to_version) == (
            True, 76, SCHEMA_VERSION,
        )
        migrated = _outcome_schema(conn)
        assert not migrate_to_current(conn, SCHEMA_VERSION).migrated
    with closing(sqlite3.connect(":memory:")) as conn:
        initialize_schema(conn)
        fresh = _outcome_schema(conn)
    assert migrated == fresh
    assert set(fresh) >= {
        "outcome_events", "idx_outcome_events_name_ts", "idx_outcome_events_agent_ts",
        "idx_outcome_events_project_ts", "idx_outcome_events_subject",
    }


def test_first_write_wins_and_duplicates_are_counted(db):
    first = _write(db, "k1", attributes={"count": 1})
    again = _write(db, "k1", attributes={"count": 99})
    assert first.inserted and not again.inserted and again.event_id is None
    assert _attributes(db) == {"k1": '{"count":1}'}
    assert _counters(db) == {"duplicate_ignored": 1}


def test_invalid_provenance_is_refused(db):
    with pytest.raises(sqlite3.IntegrityError):
        record_outcome_event(
            db, dedupe_key="k", event_name=EVENT, source="corsia",
            provenance="made_up", ts=TS,
        )


def test_retraction_sets_timestamp_and_tombstones_attributes(db):
    _write(db, "k1", attributes={"ref_id": "pr:7", "count": 3})
    assert retract_outcome_event(db, "k1")
    assert not retract_outcome_event(db, "k1")
    assert not retract_outcome_event(db, "missing")
    attributes, retracted_at = db.execute(
        "SELECT attributes, retracted_at FROM outcome_events WHERE dedupe_key='k1'"
    ).fetchone()
    assert retracted_at and attributes == '{"retracted":true}'


def test_attributes_are_allowlisted_validated_and_counted(db):
    result = _write(db, "k1", attributes={
        "ref_id": "gh:pr#12", "count": 4, "result": "ok",
        "free_text": "hello", "extra": 1,
    })
    assert result.dropped_keys == ("free_text", "extra")
    bad_values = _write(db, "k2", attributes={
        "count": -1, "result": "maybe", "ref_id": "has space",
    })
    assert bad_values.dropped_keys == ("count", "result", "ref_id")
    unknown = _write(db, "k3", event_name="not.registered", attributes={"count": 1})
    assert unknown.dropped_keys == ("count",)
    assert _attributes(db) == {
        "k1": '{"ref_id":"gh:pr#12","count":4,"result":"ok"}', "k2": "{}", "k3": "{}",
    }
    assert _counters(db) == {"attribute_dropped": 2 + 3 + 1}


@pytest.mark.parametrize("event_name", ["github.pr.opened", "github.pr.merged"])
@pytest.mark.parametrize("login", ["dependabot[bot]", "github-actions[bot]"])
def test_a_bot_author_login_is_kept_on_both_declaration_events(db, event_name, login):
    """A GitHub App's bot user writes its login with a ``[bot]`` suffix.

    Live users API, checked 2026-10-02: ``dependabot[bot]`` (id 49699333, type Bot)
    and ``github-actions[bot]`` (id 41898282, type Bot), and PR authors of that shape
    exist (``vs-code-engineering[bot]``, type Bot). The opaque-token pattern alone
    refused every one of them, so a bot-authored PR lost its author and the only
    trace was the aggregate counter. Both events that carry a declaration have to
    keep it: the opened row records the author, the merged row is what a later
    attribution read goes looking for.
    """
    kept = _write(db, "k1", event_name=event_name, attributes={
        "author_login": login, "declared_owner_id": "45c7d71c", "declaration_count": 1,
    })
    assert kept.dropped_keys == ()
    assert _attributes(db) == {
        "k1": (
            f'{{"author_login":"{login}","declared_owner_id":"45c7d71c",'
            '"declaration_count":1}'
        ),
    }
    assert _counters(db) == {}, "a kept login is not a dropped attribute"


def test_a_bracket_is_refused_on_every_field_but_the_login(db):
    """The bot suffix is a login fact, not a licence for brackets elsewhere.

    ``declared_owner_id`` is read out of the PR body, so it keeps the plain
    opaque-token shape, and a malformed login shape is dropped whole rather than
    half-accepted.
    """
    refused = _write(db, "k1", event_name="github.pr.opened", attributes={
        "author_login": "bad[name]", "declared_owner_id": "owner[bot]",
    })
    assert refused.dropped_keys == ("author_login", "declared_owner_id")
    assert _attributes(db) == {"k1": "{}"}
    assert _counters(db) == {"attribute_dropped": 2}, (
        "the total is per event name only: it says two values were dropped, not which row"
    )


@pytest.mark.parametrize("trigger", [
    "pull_request_target", "workflow_run", "issue_comment", "check_suite", "status",
    "dynamic", "image_version",
])
def test_a_registered_actions_trigger_reaches_the_row(db, trigger):
    """A run's trigger is a fact, so the vocabulary has to be GitHub's, not one sample's.

    The first five are documented triggers the one-sample enum refused. ``dynamic`` is
    measured and not in the reference: kubernetes/kubernetes run 36865116079,
    ``triggering_actor`` ``dependabot[bot]``, workflow path
    ``dynamic/dependabot/update-graph``. ``image_version`` is the reference's own
    section - it runs a workflow when a new version of a named image becomes available.
    """
    kept = _write(db, "k1", event_name="github.workflow_run.completed", attributes={
        "event": trigger, "conclusion": "failure", "attempt": 1,
    })
    assert kept.dropped_keys == ()
    assert _attributes(db) == {
        "k1": f'{{"event":"{trigger}","conclusion":"failure","attempt":1}}',
    }
    assert _counters(db) == {}


def test_an_absent_trigger_is_none_while_an_unregistered_one_leaves_no_key(db):
    """The two states a row can be in, and the limit of what the counter can tell you.

    The mapper writes ``none`` only when the payload carries no event at all, and
    ``none`` is a registered value, so that row keeps an ``event`` key. A trigger
    outside the registered set leaves no key at all. The row cannot say which value
    was lost and nothing records it per row: the only signal is the event name's
    ``attribute_dropped`` total moving, which is an aggregate loss measure, not a
    per-row audit.
    """
    absent = _write(db, "k1", event_name="github.workflow_run.completed", attributes={
        "event": "none",
    })
    assert absent.dropped_keys == ()
    unknown = _write(db, "k2", event_name="github.workflow_run.completed", attributes={
        "event": "some_future_trigger", "conclusion": "none",
    })
    assert unknown.dropped_keys == ("event",)
    assert _attributes(db) == {
        "k1": '{"event":"none"}', "k2": '{"conclusion":"none"}',
    }
    assert _counters(db) == {"attribute_dropped": 1}, (
        "one total for the event name; which run lost which trigger is not recorded"
    )


def test_a_present_invalid_value_is_never_stored_as_the_none_sentinel(db):
    """``none`` means the field was not there, and nothing else may claim that fact.

    A field that arrived with an unusable value - null, empty string, False, 0 -
    leaves no key and moves the counter. Kept as ``none`` it would sit in exactly the
    row state a payload that omitted the field produces, which is the one distinction
    the sentinel exists to preserve.
    """
    for index, bad in enumerate((None, "", False, 0)):
        result = _write(db, f"k{index}", event_name="github.workflow_run.completed",
                        attributes={"event": bad, "conclusion": "success"})
        assert result.dropped_keys == ("event",), f"a present {bad!r} is not an absent key"
    assert _attributes(db) == {
        f"k{index}": '{"conclusion":"success"}' for index in range(4)
    }
    assert _counters(db) == {"attribute_dropped": 4}, (
        "the loss is counted, which is the only place it survives"
    )


def test_secret_shaped_values_never_reach_the_stored_row(db):
    github_token = "ghp_" + "a1B2" * 9
    api_key = "sk-ant-" + "x9Y8" * 8
    result = _write(db, "k1", subject_key="github:pr:r#1", attributes={
        "ref_id": github_token, "result": api_key, "count": 2,
    })
    assert result.dropped_keys == ("ref_id", "result")
    stored = " ".join(
        str(value) for row in db.execute("SELECT * FROM outcome_events") for value in row
    )
    assert github_token not in stored and api_key not in stored
    assert _attributes(db) == {"k1": '{"count":2}'}


def test_secret_shaped_identity_fields_are_refused(db):
    token = "ghp_" + "a1B2" * 9
    with pytest.raises(ValueError):
        _write(db, token)
    for field in ("subject_key", "project_id", "agent_thread_id", "corroborated_by"):
        with pytest.raises(ValueError):
            _write(db, "k1", **{field: token})
    assert _attributes(db) == {}


def test_kept_values_pass_the_canonical_sanitizer(db, monkeypatch):
    seen = []
    monkeypatch.setattr(
        attribute_module, "sanitize_credential_data",
        lambda value: seen.append(value) or {"ref_id": "sanitized"},
    )
    _write(db, "k1", attributes={"ref_id": "abc"})
    assert seen == [{"ref_id": "abc"}]
    assert _attributes(db) == {"k1": '{"ref_id":"sanitized"}'}


def test_oversize_attributes_are_refused(db, monkeypatch):
    monkeypatch.setattr(
        attribute_module, "sanitize_credential_data",
        lambda value: {"ref_id": "x" * (MAX_ATTRIBUTES_BYTES + 1)},
    )
    with pytest.raises(ValueError):
        _write(db, "k1", attributes={"ref_id": "abc"})
    assert _attributes(db) == {}


def test_only_registered_events_are_the_test_seed_and_the_github_intake():
    assert set(ATTRIBUTE_ALLOWLISTS) == {
        EVENT,
        "github.pr.opened", "github.pr.pushed", "github.pr.reopened", "github.pr.ready", "github.pr.drafted",
        "github.pr.closed", "github.pr.retargeted", "github.pr.merged",
        "github.pr.introduced_by", "github.pr.closes_issue",
        "github.check_suite.completed", "github.workflow_run.completed",
        "github.workflow_job.completed", "github.check_run.completed",
        "github.pr.review.submitted", "github.pr.review.dismissed",
        "github.pr.review_comment.created", "github.pr.comment.created",
        "github.issue.comment.created", "github.issue.assigned", "github.status", "github.release.published",
        "github.issue.opened", "github.issue.closed", "github.issue.reopened",
        "github.issue.labeled", "github.issue.unlabeled", "github.pr.labeled", "github.pr.unlabeled",
        "measurement.size_rating",
        "measurement.correction", "measurement.lesson_gate",
        "learning_loop.correction_triage", "learning_loop.correction_outcome",
        "measurement.verdict.planned", "measurement.verdict.reached",
        "lesson.carry_in.saved", "lesson.carry_in.saved_processed",
        "lesson.carry_in.pending", "lesson.carry_in.claimed", "lesson.carry_in.waiting",
        "lesson.carry_in.result", "lesson.carry_in.retired", "lesson.carry_in.coverage",
    }


def test_ts_is_normalized_to_utc_text_and_garbage_is_refused(db):
    for key, ts in (("a", "2026-09-30T01:00:00Z"), ("b", "2026-09-29T18:00:00-07:00"),
                    ("c", "2026-09-30T01:00:00")):
        record_outcome_event(
            db, dedupe_key=key, event_name=EVENT, source="github",
            provenance="imported", ts=ts,
        )
    stored = dict(db.execute("SELECT dedupe_key, ts FROM outcome_events"))
    assert set(stored.values()) == {"2026-09-30T01:00:00+00:00"}
    with pytest.raises(ValueError):
        _write(db, "d", ts="yesterday")


def test_adapters_can_count_mapping_failures_and_unknown_counters_are_refused(db):
    count_outcome_event(db, "mapping_failed", "github.pr.merged")
    count_outcome_event(db, "mapping_failed", "github.pr.merged", 2)
    assert _counters(db) == {"mapping_failed": 3}
    with pytest.raises(sqlite3.IntegrityError):
        count_outcome_event(db, "made_up", "github.pr.merged")


@pytest.mark.parametrize("n", [0, -1])
def test_counters_only_grow(db, n):
    count_outcome_event(db, "mapping_failed", "github.pr.merged", 2)
    with pytest.raises(ValueError):
        count_outcome_event(db, "mapping_failed", "github.pr.merged", n)
    assert _counters(db) == {"mapping_failed": 2}
