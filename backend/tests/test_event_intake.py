"""Declared-event intake reaches the live evaluator and platform delivery path."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from automation import script_runner, script_schedules
from automation.script_readings import RunReport
from events import intake, rule_authoring
from events.intake import admit_event
from infra.database import get_db
from infra.measure_registry import REGISTRY, sync
from tests.event_routing_helpers import mk_session, read_statements, statements

OWNER, OTHER = "intake-owner", "intake-other"


def _declarations() -> str:
    return """schema: corsia.measures/1
namespace: qa
events:
  qa.route:
    attributes: {code: id}
    dedupe: [code]
    use: route
  qa.record:
    attributes: {code: id}
    dedupe: [code]
  qa.number:
    attributes: {cost: num}
    dedupe: [cost]
    use: route
  qa.both:
    attributes: {code: id}
    dedupe: [code, run_id]
    use: both
"""


def _schedule(db, tmp_path: Path):
    path = tmp_path / "events.py"
    path.write_text("print('event')")
    return script_schedules.create_script(
        OWNER, "project", script_path=path, script_sha256=script_schedules.file_sha256(path),
        cadence_seconds=600, series=[], timeout_seconds=30,
        now="2026-01-01T00:00:00+00:00", db=db,
    )


@pytest.fixture
def intake_world(isolated_db, tmp_path: Path):
    REGISTRY.reset()
    mk_session(OWNER, project_id="project-a")
    mk_session(OTHER, project_id="project-a")
    db = get_db()
    folder = tmp_path / ".threads" / OWNER
    folder.mkdir(parents=True)
    path = folder / "measures.yaml"
    text = _declarations()
    path.write_text(text)
    sync(db, owner_thread_id=OWNER, path=str(path), text=text)
    db.commit()
    return db


@pytest.fixture
def github_checks_type(monkeypatch):
    """A test-local github manifest type for the connector-origin branch; no github type is emitted that way yet."""
    from events.connectors import EventTypeDecl
    from events.connectors.github import MANIFEST

    monkeypatch.setitem(MANIFEST.event_types, "head.checks_completed", EventTypeDecl(
        "head.checks_completed", ("repo", "head_sha"),
        ("repo", "head_sha", "head_branch", "pr_number", "conclusion"),
        artifact_type=MANIFEST.event_types["check.completed"].artifact_type,
        artifact_integration=MANIFEST.event_types["check.completed"].artifact_integration,
        event_class="informational"))


def _admit(**kwargs):
    values = {
        "event_id": "event-1", "origin": "emitter", "producer": OWNER,
        "event_type": "qa.route", "facts": {"code": "ok"},
    }
    values.update(kwargs)
    return asyncio.run(admit_event(**values))


def _every_rule(db, *, rule_id: str, destination: str, mode: str = "wake") -> None:
    """A rule its writer (the destination) authored on qa.route."""
    db.execute(
        'INSERT INTO event_rules '
        '(id,created_at,updated_at,scope,connector,event_type,"match",action,"to",mode,origin,created_by,authority_ref) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (
            rule_id, "t", "t", "project:project-a", "qa", "route",
            json.dumps([{"field": "code", "op": "eq", "value": "ok"}]),
            "deliver", f"thread:{destination}", mode, f"agent:{destination}", destination, "self",
        ),
    )
    db.commit()


def _digested(db, thread: str, like: str) -> int:
    """How many events matching ``like`` reached the thread's digest."""
    return db.execute(
        "SELECT COUNT(DISTINCT json_extract(details_json,'$.event_id')) FROM context_change_journal "
        "WHERE object_kind='thread_context' AND object_id=? AND json_extract(details_json,'$.event_id') LIKE ?",
        (thread, like),
    ).fetchone()[0]


@pytest.mark.parametrize(("kwargs", "code"), [
    ({"facts": []}, "facts_not_object"),
    ({"facts": {"code": "x" * (65 * 1024)}}, "too_large"),
    ({"event_type": "qa.missing"}, "undeclared_type"),
    ({"facts": {"outside": "x"}}, "undeclared_fact"),
    ({"facts": {"code": 3}}, "bad_fact_kind"),
    ({"producer": OTHER}, "not_your_namespace"),
])
def test_declared_intake_refusal_codes(intake_world, kwargs, code):
    result = _admit(**kwargs)
    assert result.accepted is False and result.refusal and result.refusal.code == code


def test_record_only_type_is_refused_with_its_own_code_and_fix(intake_world):
    result = _admit(event_type="qa.record")
    assert result.refusal and result.refusal.code == "record_only"
    assert "use: record" in result.refusal.text and "qa.record" in result.refusal.text
    rendered = intake.format_event_error(result.refusal.code, result.refusal.text)
    assert rendered.count("use: route or both") == 1 and "measure_sync" in rendered


def test_refusal_messages_state_the_problem_once_then_one_next_step(intake_world):
    big = _admit(facts={"code": "x" * (65 * 1024)}).refusal
    too_large = intake.format_event_error(big.code, big.text)
    worker = asyncio.run(admit_event(
        event_id="worker-once", origin="agent", producer=OWNER, event_type="worker.result", facts={},
    ))

    assert too_large.count("fewer or shorter facts") == 1 and too_large.startswith("too_large: facts exceed")
    assert intake.format_event_error(worker.refusal.code, worker.refusal.text) == (
        "reserved_type: worker events are produced by CORSIA only. Emit a type you declared instead."
    )


@pytest.mark.parametrize("origin", ["connector", "emitter", "agent"])
def test_worker_events_are_reserved_outside_platform(intake_world, origin):
    result = asyncio.run(admit_event(
        event_id=f"worker-{origin}", origin=origin, producer=OWNER,
        event_type="worker.result", facts={},
    ))
    assert result.refusal and result.refusal.code == "reserved_type"


def test_accepted_retry_returns_duplicate_without_replaying_delivery(intake_world, monkeypatch):
    _every_rule(intake_world, rule_id="intake-every", destination=OTHER)
    wakes = []

    def record_wake(*args, **kwargs):
        wakes.append((args, kwargs))

    monkeypatch.setattr("events.platform.enqueue_event_wake", record_wake)
    accepted = _admit()
    duplicate = _admit()

    assert accepted.accepted and not accepted.duplicate
    assert duplicate.accepted and duplicate.duplicate
    assert accepted.rules_matched == duplicate.rules_matched == 1
    assert len(wakes) == 1


def test_declared_event_uses_the_live_every_evaluator_and_platform_path(intake_world, monkeypatch):
    _every_rule(intake_world, rule_id="intake-every", destination=OTHER)
    wakes = []

    def record_wake(*args, **kwargs):
        wakes.append((args, kwargs))

    monkeypatch.setattr("events.platform.enqueue_event_wake", record_wake)
    result = _admit(event_id="every-event")
    decisions = intake_world.execute(
        "SELECT rule_id,thread_id,outcome FROM event_route_decisions WHERE event_id=?",
        ("every-event",),
    ).fetchall()

    assert result.accepted and result.rules_matched == 1
    assert len(wakes) == 1
    # 2.A records only rules' outcomes; the owner's own delivery is today's routing, seen in its digest.
    assert {(row[0], row[1], row[2]) for row in decisions} == {("intake-every", OTHER, "woke")}
    assert _digested(intake_world, OWNER, "every-event") == 1


def test_refused_id_can_retry_after_fix_without_persisting_facts(intake_world):
    refused = _admit(facts={"outside": "secret"})
    accepted = _admit(facts={"code": "fixed"})
    stored = intake_world.execute(
        "SELECT event_id,outcome,rules_matched,refusal_code FROM event_intake_records "
        "WHERE event_id='event-1' ORDER BY id",
    ).fetchall()

    assert refused.refusal and accepted.accepted and not accepted.duplicate
    assert [tuple(row) for row in stored] == [
        ("event-1", "refused", 0, "undeclared_fact"), ("event-1", "accepted", 0, None),
    ]


def test_connector_manifest_accepts_nullable_pr_number_and_refuses_unknown_fact(intake_world, github_checks_type):
    accepted = asyncio.run(admit_event(
        event_id="github-ok", origin="connector", producer="github",
        event_type="github.head.checks_completed",
        facts={"repo": "r", "head_sha": "s", "head_branch": "main", "pr_number": None, "conclusion": "success"},
    ))
    refused = asyncio.run(admit_event(
        event_id="github-bad", origin="connector", producer="github",
        event_type="github.head.checks_completed",
        facts={"repo": "r", "head_sha": "s", "outside": "x"},
    ))
    assert accepted.accepted and refused.refusal and refused.refusal.code == "undeclared_fact"


def _rule(db, rule_id: str, writer: str, *, connector: str = "qa", event_type: str = "route",
          match: list | None = None, to: str | None = None, mode: str = "wake") -> None:
    """A deliver rule ``writer`` authored in project-a; to itself unless ``to`` says otherwise."""
    db.execute(
        'INSERT INTO event_rules '
        '(id,created_at,updated_at,scope,connector,event_type,"match",action,"to",mode,origin,created_by,authority_ref) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (rule_id, "t", "t", "project:project-a", connector, event_type, json.dumps(match or []),
         "deliver", to or f"thread:{writer}", mode, f"agent:{writer}", writer, "self"),
    )
    db.commit()


def _decisions(db, event_id: str) -> set[tuple]:
    return {tuple(row) for row in db.execute(
        "SELECT rule_id,thread_id,outcome,reason FROM event_route_decisions WHERE event_id=?", (event_id,),
    ).fetchall()}


def test_a_declared_events_owner_is_the_registered_owner_rules_read(intake_world, monkeypatch):
    monkeypatch.setattr("events.platform.enqueue_event_wake", lambda *args, **kwargs: None)
    _rule(intake_world, "to-owner", OTHER, to="registered_owner", mode="digest")

    result = _admit(event_id="owned")

    # The declaring thread is the event's registered owner, so the rule rides its digest.
    assert result.accepted and result.rules_matched == 1
    assert _decisions(intake_world, "owned") == {("to-owner", OWNER, "digested", None)}


def test_a_rule_fires_when_intake_dead_letters_for_an_inactive_owner(intake_world, monkeypatch):
    wakes = []
    monkeypatch.setattr("events.platform.enqueue_event_wake", lambda *args, **kwargs: wakes.append(args[0]))
    _rule(intake_world, "self", OTHER)
    intake_world.execute("UPDATE threads SET lifecycle_state='purged' WHERE id=?", (OWNER,))
    intake_world.commit()

    result = _admit(event_id="orphaned")
    parked = intake_world.execute(
        "SELECT reason FROM dead_letter_events WHERE external_id='orphaned'").fetchall()

    # Intake still parks the event for its inactive owner; the rules of that owner's project add a wake.
    assert result.accepted and result.rules_matched == 1
    assert [row[0] for row in parked] == ["owner_inactive"] and wakes == [OTHER]
    assert _decisions(intake_world, "orphaned") == {("self", OTHER, "woke", None)}


@pytest.fixture
def tracking(monkeypatch, github_checks_type):
    """Projects tracking each repository; records whether each lookup ran inside a transaction."""
    calls: list[bool] = []

    def _tracking(repository):
        calls.append(get_db().in_transaction)
        return frozenset({"project-a"}) if repository == "o/r" else frozenset()

    monkeypatch.setattr(
        "events.connectors.github_notifications_feed.projects_tracking_github_repository", _tracking)
    return calls


def _checks_completed(event_id: str):
    return asyncio.run(admit_event(
        event_id=event_id, origin="connector", producer="github",
        event_type="github.head.checks_completed",
        facts={"repo": "o/r", "head_sha": "s", "conclusion": "success"},
    ))


def test_an_unrouted_connector_event_reaches_a_self_rule_naming_its_repository(
        intake_world, monkeypatch, tracking):
    wakes = []
    monkeypatch.setattr("events.platform.enqueue_event_wake", lambda *args, **kwargs: wakes.append(args[0]))
    _rule(intake_world, "repo-self", OTHER, connector="github", event_type="head.checks_completed",
          match=[{"field": "repo", "op": "eq", "value": "o/r"}])
    _rule(intake_world, "unnamed", OWNER, connector="github", event_type="head.checks_completed")

    result = _checks_completed("unrouted")

    # Nothing owns the head, so only a writer's rule to itself naming the repository fires.
    assert result.accepted and result.rules_matched == 1 and wakes == [OTHER]
    assert _decisions(intake_world, "unrouted") == {("repo-self", OTHER, "woke", None)}
    # The projects tracking the repository were read once, before the write transaction.
    assert tracking == [False]


def test_an_unrouted_connector_event_with_no_self_rule_never_looks_up_its_repository(
        intake_world, tracking):
    _rule(intake_world, "other-type", OTHER, connector="github", event_type="pr.merged",
          match=[{"field": "repo", "op": "eq", "value": "o/r"}])
    _rule(intake_world, "to-owner", OTHER, connector="github", event_type="head.checks_completed",
          to="registered_owner", mode="digest")

    result = _checks_completed("no-self-rule")

    assert result.accepted and result.rules_matched == 0 and tracking == []


def test_an_unrouted_connector_event_fails_closed_when_its_repository_lookup_fails(
        intake_world, monkeypatch, github_checks_type):
    from events.connectors.github_notifications_feed import FeedError

    def _unreadable(_repository):
        raise FeedError("repo_scope_unavailable")

    wakes = []
    monkeypatch.setattr("events.platform.enqueue_event_wake", lambda *args, **kwargs: wakes.append(args[0]))
    monkeypatch.setattr(
        "events.connectors.github_notifications_feed.projects_tracking_github_repository", _unreadable)
    _rule(intake_world, "repo-self", OTHER, connector="github", event_type="head.checks_completed",
          match=[{"field": "repo", "op": "eq", "value": "o/r"}])

    result = _checks_completed("unreadable")

    # Not read as untracked: no rule fires, and the one that matched says why.
    assert result.accepted and result.rules_matched == 0 and wakes == []
    assert _decisions(intake_world, "unreadable") == {
        ("repo-self", OTHER, "dead_lettered", "repository lookup failed")}


def test_intake_reads_per_event(intake_world, monkeypatch, tracking):
    """DB reads and repository lookups (a policy query and git) each intake event pays."""
    from events import rules

    monkeypatch.setattr("events.platform.enqueue_event_wake", lambda *args, **kwargs: None)

    def run(admit) -> tuple[int, int]:
        tracking.clear()
        with statements() as seen:
            assert admit().accepted
        return len(read_statements(seen)), len(tracking)

    declared = iter(range(100))
    checks = iter(range(100))

    def emit():
        return _admit(event_id=f"declared-{next(declared)}")

    def check():
        return _checks_completed(f"checks-{next(checks)}")

    # Without rules: evaluation and the unrouted gate cost nothing.
    with patch.object(rules, "evaluate", lambda *_args, **_kwargs: None), \
            patch("events.intake.rule_reads.unrouted_projects", lambda *_args: frozenset()):
        run(emit)
        no_rules = run(emit)
        run(check)
        checks_no_rules = run(check)
    declared_none = run(emit)
    _rule(intake_world, "declared-self", OTHER, match=[{"field": "code", "op": "eq", "value": "ok"}])
    declared_matched = run(emit)
    checks_none = run(check)
    _rule(intake_world, "checks-self", OTHER, connector="github", event_type="head.checks_completed",
          match=[{"field": "repo", "op": "eq", "value": "o/r"}])
    checks_matched = run(check)
    intake_world.execute("UPDATE event_rules SET \"match\"=? WHERE id='checks-self'",
                         (json.dumps([{"field": "repo", "op": "eq", "value": "nobody/tracks"}]),))
    intake_world.commit()
    monkeypatch.setattr("events.connectors.github_notifications_feed.projects_tracking_github_repository",
                        lambda _repo: tracking.append(False) or frozenset())
    checks_untracked = run(check)

    # No rule: one read (declared: the scoped lookup; unrouted: the self-rule gate) and no lookup.
    assert declared_none == (no_rules[0] + 1, 0) and checks_none == (checks_no_rules[0] + 1, 0)
    # A self-rule for the type: the lookup runs once; an untracked repository then reads no rule.
    assert checks_untracked == (checks_none[0], 1) and checks_matched[1] == 1
    print(f"intake (DB reads, lookups): declared without rules=({no_rules[0]}, 0) "
          f"no_rule=+({declared_none[0] - no_rules[0]}, 0) "
          f"matched=+({declared_matched[0] - no_rules[0]}, {declared_matched[1]}); "
          f"unrouted connector without rules=({checks_no_rules[0]}, 0) "
          f"no_self_rule=+({checks_none[0] - checks_no_rules[0]}, {checks_none[1]}) "
          f"untracked=+({checks_untracked[0] - checks_no_rules[0]}, {checks_untracked[1]}) "
          f"matched=+({checks_matched[0] - checks_no_rules[0]}, {checks_matched[1]})")


def test_missing_resolved_envelope_is_refused_without_an_assertion(intake_world, monkeypatch):
    monkeypatch.setattr(
        intake, "_declared_event",
        lambda *_args: intake._ResolvedEvent(None, None, None, None),
    )

    result = _admit()

    assert result.refusal and result.refusal.code == "unresolved_event"
    assert result.refusal.text == "event could not be resolved; this is a platform fault"


def _stored(db, name: str) -> list[tuple]:
    return [tuple(row) for row in db.execute(
        "SELECT agent_thread_id, project_id, json_extract(attributes,'$.code'), json_extract(attributes,'$.run_id') "
        "FROM outcome_events WHERE event_name=?", (name,),
    ).fetchall()]


def test_a_both_event_is_stored_once_and_routed(intake_world):
    result = _admit(event_type="qa.both", event_id="both-1")
    retry = _admit(event_type="qa.both", event_id="both-1")
    assert result.accepted and not result.duplicate and retry.duplicate
    assert _stored(intake_world, "qa.both") == [(OWNER, "project-a", "ok", "both-1")]
    assert _digested(intake_world, OWNER, "both-1") == 1
    assert _admit(event_id="route-1").accepted and _stored(intake_world, "qa.route") == []


def test_a_both_event_the_store_refuses_is_not_routed(intake_world):
    # Valid for routing (no undeclared fact) but the store's dedupe needs code.
    result = _admit(event_type="qa.both", event_id="both-refused", facts={})
    routed = _digested(intake_world, OWNER, "both-refused")
    receipts = intake_world.execute(
        "SELECT outcome, refusal_code FROM event_intake_records WHERE event_id='both-refused'",
    ).fetchall()

    assert result.refusal and result.refusal.code == "record_refused" and "dedupe needs" in result.refusal.text
    assert routed == 0 and [tuple(row) for row in receipts] == [("refused", "record_refused")]


def test_script_event_lines_route_and_a_refusal_becomes_an_owner_error(intake_world, tmp_path):
    path = tmp_path / "events.py"
    path.write_text("print('event')")
    schedule = script_schedules.create_script(
        OWNER, "project", script_path=path, script_sha256=script_schedules.file_sha256(path),
        cadence_seconds=600, series=[], timeout_seconds=30,
        now="2026-01-01T00:00:00+00:00", db=intake_world,
    )
    accepted = asyncio.run(script_runner._admit_events(
        schedule, RunReport("ok", [], [], events=[{"type": "qa.route", "facts": {"code": "ok"}}], run_id="run-1"),
    ))
    refused = asyncio.run(script_runner._admit_events(
        schedule, RunReport("ok", [], [], events=[{"type": "qa.route", "facts": {"outside": "x"}}], run_id="run-2"),
    ))

    both = asyncio.run(script_runner._admit_events(
        schedule, RunReport("ok", [], [], events=[{"type": "qa.both", "facts": {"code": "ok"}}], run_id="run-3"),
    ))

    assert accepted.status == "ok" and both.status == "ok"
    undeclared_fact = intake.format_event_error(
        "undeclared_fact", "facts include a name not declared for this event type")
    assert refused.status == "error" and refused.error == f"event line refused: {undeclared_fact}"
    assert refused.signals[-1].kind == "error" and undeclared_fact in refused.signals[-1].summary
    # The run's stamp, the same one a measure line from this run gets, so the two dedupe together.
    assert _stored(intake_world, "qa.both") == [(OWNER, "project", "ok", f"script:{schedule.id}:run-3")]


def test_a_script_run_with_many_refusal_kinds_names_two_with_their_fixes_and_counts_the_rest(
    intake_world, tmp_path,
):
    path = tmp_path / "events.py"
    path.write_text("print('event')")
    schedule = script_schedules.create_script(
        OWNER, "project", script_path=path, script_sha256=script_schedules.file_sha256(path),
        cadence_seconds=600, series=[], timeout_seconds=30,
        now="2026-01-01T00:00:00+00:00", db=intake_world,
    )
    events = [
        {"type": "qa.route", "facts": {"outside": "x"}},
        {"type": "qa.route", "facts": {"outside": "y"}},
        {"type": "qa.number", "facts": {"cost": "not a number"}},
        {"type": "qa.missing", "facts": {}},
    ]
    report = asyncio.run(script_runner._admit_events(
        schedule, RunReport("ok", [], [], events=events, run_id="run-many"),
    ))

    assert report.status == "error"
    assert report.error.startswith("event line refused: bad_fact_kind: ")
    assert intake._ERROR_FIXES["bad_fact_kind"] in report.error
    assert intake._ERROR_FIXES["undeclared_fact"] in report.error
    assert report.error.count("undeclared_fact:") == 1 and report.error.endswith(" and 1 more")
    assert "undeclared_type" not in report.error
    assert len(report.signals) == 1 and report.error in report.signals[0].summary


def test_a_script_refusal_is_explainable_by_type_for_the_owners_project(intake_world, tmp_path, monkeypatch):
    schedule = _schedule(intake_world, tmp_path)
    events = [{"type": "qa.route", "facts": {"outside": "x"}}]
    refused = asyncio.run(script_runner._admit_events(
        schedule, RunReport("ok", [], [], events=events, run_id="run-explain"),
    ))
    found = rule_authoring.explain(intake_world, project_id="project-a", event_type="qa.route")
    elsewhere = rule_authoring.explain(intake_world, project_id="project-b", event_type="qa.route")

    assert [(row["refusal_code"], row["producer"], row["origin"], row["event_id"]) for row in found["refusals"]] == [
        ("undeclared_fact", f"script:{schedule.id}", "emitter", f"script:{schedule.id}:run-explain:0"),
    ]
    assert elsewhere["refusals"] == []

    def recording_failed(**_kwargs):
        raise OSError("database unavailable")

    monkeypatch.setattr("events.intake.record_refusal", recording_failed)
    again = asyncio.run(script_runner._admit_events(
        schedule, RunReport("ok", [], [], events=events, run_id="run-explain-2"),
    ))
    assert again.error == refused.error and again.status == "error"


def test_a_both_row_from_a_workflow_without_a_version_is_counted_unversioned_once(intake_world, tmp_path):
    path = tmp_path / "events.py"
    path.write_text("print('event')")
    schedule = script_schedules.create_script(
        OWNER, "project", script_path=path, script_sha256=script_schedules.file_sha256(path),
        cadence_seconds=600, series=[], timeout_seconds=30,
        now="2026-01-01T00:00:00+00:00", db=intake_world, workflow="retired-flow",
    )
    line = {"type": "qa.both", "facts": {"code": "ok"}}
    # Two lines, two event_ids, one run stamp: the store dedupes the second, which still routes.
    report = asyncio.run(script_runner._admit_events(
        schedule, RunReport("ok", [], [], events=[line, line], run_id="run-1"),
    ))
    counted = intake_world.execute(
        "SELECT agent_thread_id, n FROM outcome_event_counters WHERE counter='unversioned' AND event_name='qa.both'",
    ).fetchall()
    routed = _digested(intake_world, OWNER, f"script:{schedule.id}:run-1:%")

    assert report.status == "ok" and len(_stored(intake_world, "qa.both")) == 1
    assert [tuple(row) for row in counted] == [(OWNER, 1)] and routed == 2


def test_event_lines_share_the_measure_line_cap(intake_world):
    from automation.script_lines import MAX_MEASURE_LINES, parse_output

    event = json.dumps({"event": {"type": "qa.route", "facts": {"code": "ok"}}})
    measure = json.dumps({"measure": {"event.name": "qa.record", "attributes": {"code": "ok"}}})
    stdout = "\n".join([measure] * 150 + [event] * 55).encode()
    parsed = parse_output(stdout, {})

    assert len(parsed.measures) + len(parsed.events) == MAX_MEASURE_LINES
    assert len(parsed.events) == 50 and parsed.over_cap == 5 and parsed.problems == []


def test_every_registered_connector_namespace_is_reserved_against_declaration():
    from events.connectors import registered_connectors
    from infra.measure_validation import _RESERVED

    assert set(registered_connectors()) | {"worker"} <= _RESERVED


@pytest.mark.parametrize("namespace", ["gmail", "plan", "worker"])
def test_sync_refuses_a_connector_or_worker_namespace(intake_world, tmp_path, namespace):
    folder = tmp_path / ".threads" / OWNER
    path = folder / "measures.yaml"
    text = f"schema: corsia.measures/1\nnamespace: {namespace}\nevents:\n  {namespace}.spoof:\n    attributes: {{code: id}}\n    dedupe: [code]\n    use: route\n"
    path.write_text(text)

    with pytest.raises(ValueError, match="non-reserved"):
        sync(intake_world, owner_thread_id=OWNER, path=str(path), text=text)


def _synced_row(db, name: str) -> None:
    """An already-synced declaration row, as one from before the namespace was reserved."""
    from infra.measure_registry import _INSERT

    spec = json.dumps({"attributes": {"code": "id"}, "dedupe": ["code"], "use": "route"})
    db.execute(_INSERT, (OWNER, "event", name, 1, spec, "measures.yaml", "sha", "2026-01-01T00:00:00+00:00", 0))
    db.commit()
    REGISTRY.reset()


def test_a_declared_row_in_a_connector_namespace_never_shadows_the_manifest(intake_world):
    from events import rule_declarations

    _synced_row(intake_world, "gmail.email.received")
    _synced_row(intake_world, "worker.result")

    assert REGISTRY.ensure_loaded(intake_world).events["gmail.email.received"].owner == OWNER
    assert rule_declarations.declared_event(intake_world, "gmail", "email.received") is None
    assert rule_declarations.declared_event(intake_world, "worker", "result") is None
    declaration, entry = intake.resolve_event_type(intake_world, "gmail.email.received")
    assert entry is None and declaration.identity_facts == ("account_id", "message_id")


def test_an_emitter_cannot_emit_a_connector_manifest_type_and_no_rule_fires(intake_world, monkeypatch):
    wakes = []
    monkeypatch.setattr("events.platform.enqueue_event_wake", lambda *args, **kwargs: wakes.append(args[0]))
    _synced_row(intake_world, "gmail.email.received")
    _rule(intake_world, "gmail-self", OTHER, connector="gmail", event_type="email.received")

    result = _admit(event_id="spoofed", event_type="gmail.email.received")

    assert result.refusal and result.refusal.code == "not_your_namespace"
    assert wakes == [] and _decisions(intake_world, "spoofed") == set()
