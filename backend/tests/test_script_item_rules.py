"""Which rule-started runs of a script with saved items get them: only the rules the person's approval names,
each unchanged since. Real intake, evaluation, runner and settlement; only the sandboxed process is faked."""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from automation import schedules, script_decisions, script_runner, script_schedules, script_tree
from automation.script_readings import ITEMS_WITHHELD
from automation.script_sandbox import ScriptRun
from infra import key_store
from infra.database import get_db
from tests.test_event_rules_run_script import OWNER, _emit, _outcomes, _rule, _sweep, world  # noqa: F401

NAME = "billing-key"
VALUE = "Fake-billing-value-0123456789"  # gitleaks:allow - a test fixture, not a credential
_SAVED = [{"name": NAME, "label": "Billing key", "place": "api.example.com"}]


@dataclass
class Call:
    rule_id: str | None
    withheld: bool | None


@pytest.fixture
def pending(world, tmp_path, monkeypatch):  # noqa: F811 - the imported fixture
    """A script with a saved item, registered and waiting for the person; every run's items recorded."""
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setattr("infra.config.STATE_ROOT", root)
    key_store.add_entry(NAME, "Billing key", "", added_by_thread_id=None, added_by_name=None)
    key_store.set_value(NAME, VALUE)
    folder = tmp_path / "script"
    folder.mkdir()
    path = folder / "uses.sh"
    path.write_text("# uses an item\n")
    auth = {"state": "pending", "application_hash": "a" * 64, "purpose": "work", "user_request": "please",
            "trigger": {"cadence_seconds": None}, "review": {"reason": "fine"}, "saved_items": _SAVED,
            "tree_sha256": script_tree.tree_sha256(script_tree.read_tree(path))}
    db = get_db()
    try:
        schedule = script_schedules.create_script(
            OWNER, "project-a", script_path=path, script_sha256=script_schedules.file_sha256(path),
            cadence_seconds=None, series=[], timeout_seconds=30, now="2026-01-01T00:00:00+00:00", db=db,
            authentication=auth)
        db.commit()
        user = db.execute("SELECT user_id FROM threads WHERE id=?", (OWNER,)).fetchone()[0]
    finally:
        db.close()
    calls: list[Call] = []
    exits: list[int] = []

    async def fake(thread_id, project_id, script, *, run_id, timeout_seconds, authenticated=False,
                   saved_items=None, stdin=None, trace=(), rule_id=None, github_repo=None):
        assert authenticated
        calls.append(Call(rule_id, None if saved_items is None else saved_items.withheld))
        return ScriptRun(exits[0] if exits else 0, b"", b"", False)

    monkeypatch.setattr(script_runner, "run_script", fake)
    return {"id": schedule.id, "user": user, "calls": calls, "exits": exits}


def _loaded(pending) -> dict:  # noqa: F811
    return next(row for row in script_decisions.pending(pending["user"], None) if row["id"] == pending["id"])


def test_a_rule_on_a_pending_script_is_accepted_and_starts_no_run(pending):
    rule = _rule(pending["id"])
    assert rule["script_id"] == pending["id"]
    _emit()
    _sweep()
    assert pending["calls"] == []
    assert _outcomes(rule["id"])[0][0] != "ran"


def test_approval_binds_the_rules_it_was_shown(pending):
    rule = _rule(pending["id"])
    loaded = _loaded(pending)
    assert loaded["saved_items"] == [{"label": "Billing key", "place": "api.example.com"}]
    added = _rule(pending["id"], event_type="two")  # a rule added after the panel loaded
    with pytest.raises(ValueError, match="The rules that start this script changed. Look again."):
        script_decisions.decide(pending["id"], pending["user"], "a" * 64, True, loaded["rules_hash"])
    loaded = _loaded(pending)
    script_decisions.decide(pending["id"], pending["user"], "a" * 64, True, loaded["rules_hash"])
    db = get_db()
    try:
        stored = schedules.get(pending["id"], db=db).payload["authentication"]
    finally:
        db.close()
    assert stored["state"] == "approved"
    assert sorted(r["id"] for r in stored["rules"]) == sorted([rule["id"], added["id"]])
    assert all(len(r["fingerprint"]) == 64 for r in stored["rules"])


def test_a_deny_needs_no_rules_match(pending):
    loaded = _loaded(pending)
    _rule(pending["id"])
    assert script_decisions.decide(pending["id"], pending["user"], "a" * 64, False, loaded["rules_hash"])[1] == "denied"


def test_only_an_approved_unchanged_rule_gets_the_items(pending):
    approved = _rule(pending["id"])
    script_decisions.decide(pending["id"], pending["user"], "a" * 64, True, _loaded(pending)["rules_hash"])
    _emit(code="first")
    _sweep()
    assert pending["calls"] == [Call(approved["id"], False)]

    later = _rule(pending["id"], event_type="two")  # added after the approval
    _emit(event_type="qa.two", code="second")
    _sweep()
    assert pending["calls"][-1] == Call(later["id"], True)

    db = get_db()
    db.execute("UPDATE event_rules SET \"match\"=? WHERE id=?",
               ('[{"field":"code","op":"eq","value":"third"}]', approved["id"]))
    db.commit()
    db.close()
    _emit(code="third")
    _sweep()
    assert pending["calls"][-1] == Call(approved["id"], True)  # changed since the approval


def test_turning_a_rule_off_and_on_keeps_it_approved(pending):
    approved = _rule(pending["id"])
    script_decisions.decide(pending["id"], pending["user"], "a" * 64, True, _loaded(pending)["rules_hash"])
    db = get_db()
    db.execute("UPDATE event_rules SET enabled=0 WHERE id=?", (approved["id"],))
    db.execute("UPDATE event_rules SET enabled=1 WHERE id=?", (approved["id"],))
    db.commit()
    db.close()
    _emit()
    _sweep()
    assert pending["calls"] == [Call(approved["id"], False)]


def test_a_withheld_run_that_fails_says_why_in_its_rule_run(pending):
    # As a missing GitHub login does: the note explains a failed run, and is noise on one that worked.
    _rule(pending["id"])
    script_decisions.decide(pending["id"], pending["user"], "a" * 64, True, _loaded(pending)["rules_hash"])
    later = _rule(pending["id"], event_type="two")
    pending["exits"].append(1)
    _emit(event_type="qa.two")
    _sweep()
    [(outcome, reason)] = _outcomes(later["id"])
    assert outcome == "failed" and ITEMS_WITHHELD in reason
