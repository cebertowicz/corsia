"""The local CLI and agent event tool use the declared-event intake."""
from __future__ import annotations

import asyncio
import io
import json
import sys
from pathlib import Path

import pytest

import cli as corsia_cli

from infra.database import get_db
from infra.measure_registry import REGISTRY, sync
from tests.event_routing_helpers import mk_session
from tools.event_rules import EventRulesTool, inspect_rules

OWNER, OTHER = "emit-owner", "emit-other"


@pytest.fixture
def emit_world(isolated_db, tmp_path: Path):
    REGISTRY.reset()
    mk_session(OWNER, project_id="project-a")
    mk_session(OTHER, project_id="project-a")
    text = """schema: corsia.measures/1
namespace: qa
events:
  qa.route:
    attributes: {code: id}
    dedupe: [code]
    use: route
  qa.followup:
    attributes: {code: id}
    dedupe: [code]
    use: route
"""
    folder = tmp_path / ".threads" / OWNER
    folder.mkdir(parents=True)
    path = folder / "measures.yaml"
    path.write_text(text)
    db = get_db()
    sync(db, owner_thread_id=OWNER, path=str(path), text=text)
    db.commit()
    db.close()


def _run(argv: list[str], capsys) -> tuple[int, str, str]:
    try:
        rc = corsia_cli.main(argv)
    except SystemExit as exc:
        rc = exc.code
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _cli(*args: str) -> list[str]:
    return ["event", "emit", *args, "--as-session", OWNER]


def test_cli_accepted_event_delivers_to_its_session_and_retries(emit_world, capsys):
    args = _cli("qa.route", "--facts", '{"code":"ok"}', "--id", "hook-1")
    rc, out, err = _run(args, capsys)
    again, repeat_out, repeat_err = _run(args, capsys)
    receipt = json.loads(out)

    assert rc == 0 and not err
    assert set(receipt) == {"event_id", "duplicate", "rules_matched"}
    assert receipt["duplicate"] is False
    assert again == 0 and not repeat_err
    assert json.loads(repeat_out) == {**receipt, "duplicate": True}
    db = get_db()
    rows = db.execute(
        "SELECT rule_id,thread_id FROM event_route_decisions WHERE event_id=?", (receipt["event_id"],)
    ).fetchall()
    db.close()
    assert {(row[0], row[1]) for row in rows} == {("builtin:0", OWNER)}


def test_cli_stdin_and_error_exit_contracts(emit_world, capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"code":"stdin"}'))
    ok, out, err = _run(_cli("qa.route", "--facts", "-"), capsys)
    refused, refused_out, refused_err = _run(_cli("qa.route", "--facts", '{"outside":"secret"}'), capsys)
    foreign, foreign_out, foreign_err = _run(
        ["event", "emit", "qa.route", "--facts", '{"code":"no"}', "--as-session", OTHER], capsys,
    )
    worker, worker_out, worker_err = _run(_cli("worker.result", "--facts", "{}"), capsys)
    usage, usage_out, usage_err = _run(_cli("qa.route"), capsys)

    assert ok == 0 and not err and json.loads(out)["duplicate"] is False
    assert refused == 1 and not refused_out
    assert refused_err.startswith("error: undeclared_fact:") and "secret" not in refused_err
    assert foreign == 1 and not foreign_out and foreign_err.startswith("error: not_your_namespace:")
    assert worker == 1 and not worker_out and worker_err.startswith("error: reserved_type:")
    assert usage == 2 and not usage_out
    assert usage_err.startswith("error: usage:")


def test_cli_bad_json_and_transient_exit_contracts(emit_world, capsys, monkeypatch):
    bad_json, out, err = _run(_cli("qa.route", "--facts", "{"), capsys)
    assert bad_json == 2 and not out and err.startswith("error: bad_json:")

    async def unavailable(**_kwargs):
        raise OSError("offline")

    monkeypatch.setattr("events.intake.admit_event", unavailable)
    transient, transient_out, transient_err = _run(_cli("qa.route", "--facts", '{"code":"ok"}'), capsys)
    assert transient == 3 and not transient_out and transient_err.startswith("error: intake_unreachable:")


def test_cli_recording_failure_keeps_the_permanent_refusal(emit_world, capsys, monkeypatch):
    def recording_failed(**_kwargs):
        raise OSError("database unavailable")

    monkeypatch.setattr("events.intake.record_refusal", recording_failed)
    rc, out, err = _run(_cli("worker.result", "--facts", "{}"), capsys)

    assert rc == 1 and not out
    assert err.startswith("error: reserved_type:") and "CORSIA only." in err


def test_refused_cli_identity_can_be_corrected_and_is_explainable(emit_world, capsys):
    failed, _, _ = _run(_cli("qa.route", "--facts", '{"outside":"secret"}', "--id", "fix-me"), capsys)
    fixed, out, err = _run(_cli("qa.route", "--facts", '{"code":"fixed"}', "--id", "fix-me"), capsys)
    receipt = json.loads(out)
    db = get_db()
    refusals = inspect_rules(db, action="explain", project_id="project-a", event_type="qa.route")["refusals"]
    stored = db.execute("SELECT event_id,outcome,refusal_code,producer,origin,event_type FROM event_intake_records").fetchall()
    db.close()

    assert failed == 1 and fixed == 0 and not err and receipt["duplicate"] is False
    assert refusals[0]["refusal_code"] == "undeclared_fact"
    assert {(row[1], row[2], row[3], row[4], row[5]) for row in stored} >= {
        ("refused", "undeclared_fact", OWNER, "emitter", "qa.route"),
        ("accepted", None, OWNER, "emitter", "qa.route"),
    }


def test_agent_emit_sets_origin_and_rejects_reserved_and_foreign_types(emit_world):
    tool = EventRulesTool()
    accepted = json.loads(asyncio.run(tool.execute(
        action="emit", event_type="qa.route", facts={"code": "agent"}, _thread_id=OWNER,
    )))
    foreign = asyncio.run(tool.execute(
        action="emit", event_type="qa.route", facts={"code": "no"}, _thread_id=OTHER,
    ))
    worker = asyncio.run(tool.execute(
        action="emit", event_type="worker.result", facts={}, _thread_id=OWNER,
    ))
    db = get_db()
    rows = db.execute("SELECT origin,refusal_code FROM event_intake_records WHERE outcome='refused'").fetchall()
    db.close()

    assert accepted["duplicate"] is False
    assert foreign.startswith("Error: not_your_namespace:")
    assert worker.startswith("Error: reserved_type:")
    assert {tuple(row) for row in rows} == {("agent", "not_your_namespace"), ("agent", "reserved_type")}


def test_agent_emit_identity_is_scoped_to_declared_event_type(emit_world):
    tool = EventRulesTool()
    first = json.loads(asyncio.run(tool.execute(
        action="emit", event_type="qa.route", facts={"code": "one"}, event_id="shared-id",
        _thread_id=OWNER,
    )))
    second = json.loads(asyncio.run(tool.execute(
        action="emit", event_type="qa.followup", facts={"code": "two"}, event_id="shared-id",
        _thread_id=OWNER,
    )))
    retry = json.loads(asyncio.run(tool.execute(
        action="emit", event_type="qa.route", facts={"code": "one"}, event_id="shared-id",
        _thread_id=OWNER,
    )))
    db = get_db()
    delivered = db.execute(
        "SELECT event_id FROM event_route_decisions WHERE event_id IN (?,?) ORDER BY event_id",
        (first["event_id"], second["event_id"]),
    ).fetchall()
    db.close()

    assert first["duplicate"] is False and second["duplicate"] is False
    assert first["event_id"] != second["event_id"]
    assert retry == {**first, "duplicate": True}
    assert {row[0] for row in delivered} == {first["event_id"], second["event_id"]}


def test_agent_test_and_emit_share_declared_type_resolution(emit_world):
    preview = asyncio.run(EventRulesTool().execute(
        action="test", event={"connector": "qa", "type": "route", "facts": {"code": "preview"}},
        _thread_id=OWNER,
    ))
    missing = asyncio.run(EventRulesTool().execute(
        action="test", event={"connector": "qa", "type": "missing", "facts": {}}, _thread_id=OWNER,
    ))
    invalid = asyncio.run(EventRulesTool().execute(action="other", _thread_id=OWNER))

    assert json.loads(preview)["delivered"] is False
    assert missing == "Error: event type is not declared"
    assert invalid == "Error: action must be list, test, explain, or emit; rules are read-only here and emit sends an event"


def test_agent_emit_does_not_accept_a_caller_origin_or_relabel_refusal(emit_world, monkeypatch):
    result = asyncio.run(EventRulesTool().execute(
        action="emit", event_type="qa.route", facts={"code": "agent"}, origin="platform", _thread_id=OWNER,
    ))

    def recording_failed(**_kwargs):
        raise OSError("database unavailable")

    monkeypatch.setattr("events.intake.record_refusal", recording_failed)
    refused = asyncio.run(EventRulesTool().execute(
        action="emit", event_type="worker.result", facts={}, _thread_id=OWNER,
    ))
    db = get_db()
    origin = db.execute("SELECT origin FROM event_intake_records WHERE outcome='accepted'").fetchone()[0]
    db.close()

    assert json.loads(result)["duplicate"] is False
    assert origin == "agent"
    assert refused.startswith("Error: reserved_type:")
    assert "CORSIA only." in refused


def test_blank_emit_identity_is_a_fresh_event_not_a_shared_retry_key():
    from events.intake import emitted_event_id

    assert emitted_event_id("qa.done", "") != emitted_event_id("qa.done", "")
    assert emitted_event_id("qa.done", "r1") == emitted_event_id("qa.done", "r1")
