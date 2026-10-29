"""A script event inherits only the artifact on its own running rule decision."""

import sqlite3
from types import SimpleNamespace

from events import intake
from events.intake import _rule_run_artifact


def test_rule_run_artifact_is_resolved_from_running_server_record():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE event_rules (id TEXT, script_id TEXT);
        CREATE TABLE event_route_decisions (
            id INTEGER, rule_id TEXT, outcome TEXT, artifact_integration TEXT,
            artifact_type TEXT, artifact_external_id TEXT
        );
        INSERT INTO event_rules VALUES ('r1', 's1');
        INSERT INTO event_route_decisions VALUES (7, 'r1', 'running', 'github', 'pr', 'a/b#3');
    """)
    assert _rule_run_artifact(db, 7, "script:s1") == {
        "integration": "github", "type": "pr", "external_id": "a/b#3"}
    assert _rule_run_artifact(db, 7, "script:s2") is None
    db.execute("UPDATE event_route_decisions SET outcome='ran'")
    assert _rule_run_artifact(db, 7, "script:s1") is None


def test_forged_artifact_fact_is_refused(monkeypatch):
    spec = SimpleNamespace(accepts=lambda value: isinstance(value, str))
    entry = SimpleNamespace(owner="owner", use="route", declared={"code": "id"}, specs={"code": spec})
    monkeypatch.setattr(intake, "resolve_event_type", lambda *_: (object(), entry))
    monkeypatch.setattr(intake, "_script_owner", lambda *_: "owner")
    original = {"integration": "github", "type": "pr", "external_id": "a/b#3"}
    forged = intake._declared_event(None, "qa.route", {"code": "ok", "artifact": "other"},
                                    "script:s1", "emitter", original)
    assert forged.refusal.code == "undeclared_fact"
    accepted = intake._declared_event(None, "qa.route", {"code": "ok"},
                                      "script:s1", "emitter", original)
    assert accepted.envelope.artifact_ref == original
    entry.declared["artifact"] = "id"
    entry.specs["artifact"] = spec
    accepted = intake._declared_event(None, "qa.route", {"code": "ok", "artifact": "other"},
                                      "script:s1", "emitter", original)
    assert accepted.envelope.artifact_ref == original
