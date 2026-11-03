"""``explain`` tells a script rule's outcome in its own words: one wording per cause, and a
script that never started never reads as run or failed."""
from __future__ import annotations

import json

import pytest

from automation import script_schedules
from automation.script_sandbox import ScriptRun, github_lock_reason
from connections.github_agent import AgentAccountView
from infra.database import get_db
from tests.test_event_rules_run_script import (  # noqa: F401 - fixtures
    _START, OWNER, _emit, _explained, _outcomes, _rule, _script, _sweep, _tool, _write, runs, world,
)


def test_a_script_that_reports_a_breach_ran_and_is_counted_as_run(runs):
    path = runs["tmp"] / "breach.sh"
    path.write_text("# breach\n")
    db = get_db()
    try:
        script_id = script_schedules.create_script(
            OWNER, "project-a", script_path=path, script_sha256=script_schedules.file_sha256(path),
            cadence_seconds=None, series=script_schedules.parse_series([{"key": "cost", "max": 1}]),
            timeout_seconds=30, now=_START.isoformat(), db=db,
        ).id
    finally:
        db.close()
    rule = _rule(script_id)
    runs["outputs"]["breach.sh"] = ScriptRun(0, b'{"series": "cost", "value": 5}\n', b"", False)
    event_id = _emit()
    _sweep()

    assert _outcomes(rule["id"]) == [("ran", None)]
    assert _explained(event_id) == [("ran", f"rule {rule['id']} ran {OWNER}'s script with it")]
    [listed] = json.loads(_tool(OWNER, operation="list"))["rules"]
    assert (listed["starts_24h"], listed["skips_24h"]) == (1, 0)


def test_an_archived_creator_holds_a_script_rule_without_queueing(runs):
    rule = _rule(_script(runs, "gone.sh"))
    _write("UPDATE threads SET lifecycle_state='archived', archived_reason='user' WHERE id=?", OWNER)
    event_id = _emit()
    _sweep()

    reason = "creator unavailable"
    assert runs["calls"] == [] and _outcomes(rule["id"]) == [("held", reason)]
    assert _explained(event_id) == [
        ("held", f"rule {rule['id']} held it from {OWNER}: {reason}")]


_ACCOUNTS = {
    "person": (AgentAccountView("wrong_account", "person_account", home="/h"),
               "the agents' GitHub sign-in is the person's own GitHub account"),
    "unverified": (AgentAccountView("unverified", "own_login_unknown", home="/h"),
                   "CORSIA can't tell the agents' GitHub account from the person's own until the "
                   "person signs in to GitHub on this computer"),
    "suspended": (AgentAccountView("needs_auth", "token_rejected", home="/h"),
                  "GitHub rejected the agents' GitHub account's sign-in; sign it in again"),
    "absent": (AgentAccountView("not_installed", absent=True),
               "there is no agents' GitHub account"),
}

# Every cause github_agent.view names, with its state there: each reads as its own cause.
_CAUSES = {
    "read_failed": ("unverified", "CORSIA couldn't read the agents' GitHub account settings"),
    "multiple_accounts": ("needs_auth", "more than one agents' GitHub account is set up; remove the extra one"),
    "github_executable_missing": ("not_installed", "the gh program the agents' GitHub account uses is missing"),
    "signed_out": ("needs_auth", "the agents' GitHub account is signed out; sign it in again"),
    "credential_missing": ("needs_auth", "the agents' GitHub account's saved sign-in is missing; sign it in again"),
    "token_rejected": ("needs_auth", "GitHub rejected the agents' GitHub account's sign-in; sign it in again"),
    "verify_failed": ("unverified", "CORSIA couldn't reach GitHub to check the agents' GitHub account"),
    "verifying": ("unverified", "the agents' GitHub account is still being checked"),
    "own_login_unknown": ("unverified", _ACCOUNTS["unverified"][1]),
    "person_account": ("wrong_account", _ACCOUNTS["person"][1]),
}


@pytest.mark.parametrize("cause", sorted(_CAUSES))
def test_each_account_cause_reads_as_itself(cause):
    state, why = _CAUSES[cause]
    assert github_lock_reason(AgentAccountView(state, cause, home="/h")) == f"scripts get no GitHub login: {why}"
    assert len({text for _state, text in _CAUSES.values()}) == len(_CAUSES)


def test_only_an_unanswered_account_is_pending():
    pending = {cause for cause, (state, _why) in _CAUSES.items() if AgentAccountView(state, cause).answer_pending}
    assert pending == {"verifying", "own_login_unknown"}


@pytest.mark.parametrize("state", sorted(_ACCOUNTS))
def test_a_failed_run_without_a_github_login_says_why(runs, state):
    view, why = _ACCOUNTS[state]
    _rule(_script(runs, "locked.sh"))
    runs["outputs"]["locked.sh"] = ScriptRun(1, b"", b"", False, github_lock_reason(view))
    event_id = _emit()
    _sweep()

    [(outcome, text)] = _explained(event_id)
    assert outcome == "failed" and text.endswith(f"; scripts get no GitHub login: {why}")


def test_a_run_that_worked_without_a_github_login_adds_no_line(runs):
    view, _why = _ACCOUNTS["absent"]
    rule = _rule(_script(runs, "local.sh"))
    runs["outputs"]["local.sh"] = ScriptRun(0, b"", b"", False, github_lock_reason(view))
    event_id = _emit()
    _sweep()

    assert _explained(event_id) == [("ran", f"rule {rule['id']} ran {OWNER}'s script with it")]


def test_a_long_failure_and_its_login_note_fit_the_reason_limit(runs):
    from events.rule_runs import _REASON_LIMIT

    view, why = _ACCOUNTS["absent"]
    rule = _rule(_script(runs, "long.sh"))
    runs["outputs"]["long.sh"] = ScriptRun(1, b"", b"x" * 2000, False, github_lock_reason(view))
    _emit()
    _sweep()

    [(outcome, reason)] = _outcomes(rule["id"])
    assert outcome == "failed" and len(reason) <= _REASON_LIMIT
    assert reason.endswith(f"; scripts get no GitHub login: {why}")


def test_a_failed_locked_run_keeps_its_failure_and_names_the_missing_login(runs):
    view, why = _ACCOUNTS["unverified"]
    rule = _rule(_script(runs, "push.sh"))
    runs["outputs"]["push.sh"] = ScriptRun(128, b"", b"push refused", False, github_lock_reason(view))
    event_id = _emit()
    _sweep()

    [(outcome, text)] = _explained(event_id)
    assert outcome == "failed" and "push refused" in text
    assert text.endswith(f"; scripts get no GitHub login: {why}")


def test_a_run_as_the_verified_account_adds_no_line(runs):
    ready = AgentAccountView("ready", home="/h", login="agents-bot", user_id=1)
    rule = _rule(_script(runs, "ready.sh"))
    runs["outputs"]["ready.sh"] = ScriptRun(0, b"", b"", False, github_lock_reason(ready))
    event_id = _emit()
    _sweep()

    assert _explained(event_id) == [("ran", f"rule {rule['id']} ran {OWNER}'s script with it")]
