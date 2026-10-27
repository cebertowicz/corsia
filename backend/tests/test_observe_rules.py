"""Observe rules: rows that register an artifact named in shell output to its caller.

Each test drives the real entrypoints (the ``event_rules`` tool, the shell tool, the observer) on a
disposable database; only the shell's subprocess is stubbed.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone

import pytest

from events import connectors
from infra.database import get_db
from registry import models
from registry.observer import CommandArtifactRegistrationError, observe_command_result
from tests.event_routing_helpers import mk_session
from tests.registry_helpers import make_session
from tests.test_shell_pr_registration import _URL, _direct_shell_context, _stub_process
from tools.event_rules import EventRulesTool
from tools.shell import BashTool

P = "proj-a"
Q = "proj-b"
PR = "owner/repo#42"
SHIP = ["ship-pr", "--fast"]
OTHER_URL = "https://github.com/owner/repo/pull/43"
FOREIGN_URL = "https://github.com/elsewhere/repo/pull/7"
FOREIGN = "elsewhere/repo#7"



def _write(sql: str, *args) -> None:
    db = get_db()
    try:
        db.execute(sql, args)
        db.commit()
    finally:
        db.close()


def _rows(sql: str, *args) -> list[dict]:
    db = get_db()
    try:
        return [dict(row) for row in db.execute(sql, args).fetchall()]
    finally:
        db.close()


def _agent(thread_id: str, project: str = P, *, temporary_of: str | None = None) -> str:
    now = datetime.now(timezone.utc).isoformat()
    if not _rows("SELECT id FROM projects WHERE id=?", project):
        _write("INSERT INTO projects (id, name, created_at, updated_at) VALUES (?,?,?,?)",
               project, project, now, now)
    mk_session(thread_id, spawned_by=temporary_of, project_id=project)
    _write("UPDATE threads SET agent_enabled=? WHERE id=?", 0 if temporary_of else 1, thread_id)
    return thread_id


async def _tool(thread_id: str, **kwargs):
    out = await EventRulesTool().execute(_thread_id=thread_id, **kwargs)
    return out if out.startswith(("Refused:", "Error:")) else json.loads(out)


async def _observe_rule(thread_id: str, **kwargs):
    args = {"operation": "create", "kind": "observe", "tool": "bash", "argv_prefix": ["ship-pr"],
            "item_type": "pull-request", **kwargs}
    return await _tool(thread_id, **args)


def _run(thread_id: str, command: str, output: str = _URL + "\n", **kwargs):
    told: list[str] = []
    artifact = observe_command_result(thread_id, "bash", {"command": command}, output, told=told, **kwargs)
    return artifact, told


def _owner(external_id: str = PR) -> str | None:
    row = models.resolve("github", "pull-request", external_id)
    return row["parent_thread_id"] if row else None


@pytest.mark.asyncio
async def test_an_agent_writes_an_observe_row_that_registers_its_own_command_and_only_its_own(isolated_db):
    _agent("a")
    _agent("b")
    written = await _observe_rule("a")
    assert (written["kind"], written["tool"], written["argv_prefix"], written["item_type"]) == (
        "observe", "bash", ["ship-pr"], "pull-request")
    assert written["origin"] == "agent:a" and written["scope"] == f"project:{P}"

    other, other_told = _run("b", "ship-pr --fast")
    assert (other, other_told) == (None, []) and _owner() is None
    mine, told = _run("a", "ship-pr --fast")
    assert mine["parent_thread_id"] == "a" and _owner() == "a"
    assert told == ["Registered pull request owner/repo#42 to you."]


@pytest.mark.asyncio
async def test_a_command_the_rule_does_not_name_registers_nothing(isolated_db):
    _agent("a")
    await _observe_rule("a")
    for command in ("ship-prs", "echo ship-pr", "ship-pr && echo $(date)"):
        assert _run("a", command) == (None, [])
    assert _run("a", "ship-pr", is_error=True) == (None, [])
    assert _owner() is None


@pytest.mark.asyncio
async def test_create_refuses_what_an_observe_row_cannot_do(isolated_db):
    _agent("a")
    _agent("w", temporary_of="a")
    connected = await _observe_rule("a", tool="mcp__github__create_pull_request")
    assert connected.startswith("Refused: Observe rules can't watch a connected app's actions.")
    assert "bash" in connected and "yet" not in connected and "stamp" not in connected
    unknown = await _observe_rule("a", item_type="calendar-event")
    assert unknown.startswith("Refused: no connector reports on 'calendar-event'")
    assert "pull-request" in unknown
    assert (await _observe_rule("a", connector="github", item_type="calendar-event")).startswith(
        "Refused: no connector reports on 'calendar-event'")
    wrong = await _observe_rule("a", connector="missing", item_type="pull-request")
    assert wrong.startswith("Refused: connector missing does not report on 'pull-request'")
    for stray in ("to", "mode", "note", "action", "match"):
        refused = await _observe_rule("a", **{stray: "thread:a" if stray == "to" else "deliver"})
        assert refused.startswith("Refused:") and stray in refused
    assert (await _observe_rule("a", argv_prefix=[])).startswith("Refused: argv_prefix")
    assert (await _observe_rule("w")).startswith("Refused: A temporary agent can't write rules")
    await _observe_rule("a")
    assert (await _observe_rule("a")).startswith("Refused: You already have that observe rule")
    assert len(_rows("SELECT id FROM event_rules WHERE kind='observe' AND origin LIKE 'agent:%'")) == 1


@pytest.mark.asyncio
async def test_shipped_list_stats_are_scoped_to_each_project(isolated_db):
    _agent("a", P)
    _agent("b", Q)
    _run("a", "gh pr create", _URL + "\n")
    _run("b", "gh pr create", OTHER_URL + "\n")
    _write("UPDATE event_route_decisions SET at=? WHERE thread_id=?", "2026-01-01T00:00:00+00:00", "a")
    _write("UPDATE event_route_decisions SET at=? WHERE thread_id=?", "2026-02-01T00:00:00+00:00", "b")
    now = datetime.now(timezone.utc).isoformat()
    for thread in ("a", "b", "b"):
        _write("INSERT INTO event_route_decisions (at,event_id,connector,event_type,watch_event,rule_id,thread_id,outcome,via) "
               "VALUES (?,?, 'github', 'observe:bash', '', 'ruleset_github_pr_create', ?, 'skipped', 'rule')",
               now, f"skip-{thread}", thread)
    for thread, expected, skips in (("a", "2026-01-01T00:00:00+00:00", 1),
                                    ("b", "2026-02-01T00:00:00+00:00", 2)):
        [row] = (await _tool(thread, operation="list"))["rule_sets"]
        assert (row["last_fired"], row["skips_24h"]) == (expected, skips)
        assert len((await _tool(thread, operation="explain", artifact=PR))["decisions"]) == (thread == "a")


@pytest.mark.parametrize("kind,extra", [
    ("observe", {"event_type": "event"}),
    ("route", {"tool": "bash"}),
    ("observe", {"workflow_id": "workflow"}),
    ("observe", {"request_identity_fact": "fact"}),
])
def test_storage_rejects_mixed_rule_fields(isolated_db, kind, extra):
    db = get_db()
    values = {"id": "bad", "created_at": "now", "updated_at": "now", "kind": kind,
              "scope": "project:p", "connector": "github", "origin": "agent:a",
              "created_by": "a", "authority_ref": "self"}
    values.update({"tool": "bash", "argv_prefix": '["gh"]', "item_type": "pull-request"} if kind == "observe" else
                  {"event_type": "event", "action": "drop", "to": "thread:a"})
    values.update(extra)
    columns = ', '.join(f'"{name}"' for name in values)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(f"INSERT INTO event_rules ({columns}) VALUES ({','.join('?' for _ in values)})", tuple(values.values()))
    db.close()


def test_the_shipped_rule_registers_gh_pr_create_for_every_caller(isolated_db):
    _agent("a")
    mk_session("loner")  # in no project: the shipped rule still covers it
    for thread, url, external_id in ("a", _URL, PR), ("loner", OTHER_URL, "owner/repo#43"):
        artifact, told = _run(thread, "gh pr create --title x", url + "\n")
        assert artifact["parent_thread_id"] == thread == _owner(external_id)
        assert told == [f"Registered pull request {external_id} to you."]
    [shipped] = _rows("SELECT * FROM event_rules WHERE origin LIKE 'rule_set:%'")
    assert (shipped["id"], shipped["scope"], shipped["kind"], shipped["connector"], shipped["tool"]) == (
        "ruleset_github_pr_create", "global", "observe", "github", "bash")
    assert (json.loads(shipped["argv_prefix"]), shipped["item_type"]) == (["gh", "pr", "create"], "pull-request")
    assert connectors.artifact_extractor(shipped["connector"], shipped["item_type"]) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["native", "codex"])
async def test_shell_execution_and_result_settlement_observe_once(isolated_db, monkeypatch, harness):
    from tests import test_registry_command_observer as seam

    _agent("a")
    _stub_process(monkeypatch, stdout="no url here\n")
    monkeypatch.setattr(seam, "_URL", "no url here")
    await BashTool().execute("gh pr create --title durable-seam", **_direct_shell_context("a"))
    db = get_db()
    await seam._drive_durable_result_seam(harness, "a", db)
    decisions = _rows("SELECT outcome, reason FROM event_route_decisions WHERE thread_id='a'")
    assert len(decisions) == 1
    assert decisions[0]["outcome"] == "not_registered"


def test_missing_extractor_is_reported_and_recorded(isolated_db, monkeypatch):
    _agent("a")
    monkeypatch.setattr(connectors, "artifact_extractor", lambda *_: None)
    artifact, told = _run("a", "gh pr create")
    reason = "connector github cannot read pull request from command output"
    assert artifact is None
    assert told == [f"Not registered: {reason}."]
    [decision] = _rows("SELECT outcome, reason FROM event_route_decisions WHERE thread_id='a'")
    assert decision == {"outcome": "not_registered", "reason": reason}


@pytest.mark.asyncio
async def test_agent_row_can_register_a_pr_named_by_read_only_command(isolated_db):
    _agent("a")
    await _observe_rule("a", argv_prefix=["gh", "pr", "view"])
    artifact, told = _run("a", "gh pr view 7", FOREIGN_URL + "\n")
    assert artifact["parent_thread_id"] == "a" == _owner(FOREIGN)
    assert told == [f"Registered pull request {FOREIGN} to you."]


def test_the_outcome_is_written_through_the_callers_open_transaction(isolated_db):
    _agent("a")
    db = get_db()
    db.execute("BEGIN")
    db.execute("UPDATE threads SET title='mid-transaction' WHERE id='a'")
    assert db.in_transaction
    told: list[str] = []
    artifact = observe_command_result("a", "bash", {"command": "gh pr create"}, _URL + "\n", db=db, told=told)
    assert artifact["parent_thread_id"] == "a" and told == ["Registered pull request owner/repo#42 to you."]
    rows = db.execute("SELECT outcome, thread_id FROM event_route_decisions WHERE artifact_external_id=?",
                      (PR,)).fetchall()
    assert [(row["outcome"], row["thread_id"]) for row in rows] == [("registered", "a")]
    db.commit()


@pytest.mark.asyncio
async def test_an_observe_row_never_takes_an_owned_artifact_from_its_owner(isolated_db):
    _agent("owner")
    _agent("rival")
    assert _run("owner", "gh pr create")[0]["parent_thread_id"] == "owner"
    await _observe_rule("rival")
    claimed, told = _run("rival", "ship-pr --fast")
    assert _owner() == "owner"
    assert claimed is not None and claimed["external_id"] == PR  # the follower's registration is returned
    assert told == ["Not registered to you: pull request owner/repo#42 already belongs to another chat, "
                    "which keeps it; you follow it."]
    [row] = _rows("SELECT relationship_state FROM artifact_registrations WHERE parent_thread_id='rival'")
    assert row["relationship_state"] == "subscriber-recent"


@pytest.mark.asyncio
async def test_a_second_run_tells_the_owner_and_a_follower_apart(isolated_db):
    _agent("owner")
    _agent("rival")
    await _observe_rule("rival")
    _run("owner", "gh pr create")
    _run("rival", "ship-pr --fast")
    assert _run("owner", "gh pr create") == (None, ["pull request owner/repo#42 is already registered to you."])
    again, told = _run("rival", "ship-pr --fast")
    assert (again, _owner()) == (None, "owner")
    assert told == ["Not registered to you: pull request owner/repo#42 belongs to another chat; "
                    "you already follow it."]


def test_a_follower_gets_the_pane_refresh_through_the_shell_tool(isolated_db, monkeypatch):
    _agent("owner")
    # In no project, as the shell tool's own tests are, and the owner's user, so it can follow.
    rival = make_session(get_db(), prefix="rival", user_id="user-1")
    _run("owner", "gh pr create")
    notified: list[dict] = []
    monkeypatch.setattr("tools.shell.notify_changed", notified.append)
    _stub_process(monkeypatch, stdout=_URL + "\n")
    result = asyncio.run(BashTool().execute("gh pr create --title t", **_direct_shell_context(rival)))
    assert "Registered" not in result and _owner() == "owner"
    assert [item["external_id"] for item in notified] == [PR]


@pytest.mark.parametrize("fail_second", [False, True])
def test_shell_notifies_each_registration_even_if_a_later_one_fails(isolated_db, monkeypatch, fail_second):
    from registry import observer

    thread = make_session(get_db(), prefix="multi")
    _write("INSERT INTO event_rules (id,created_at,updated_at,enabled,kind,scope,connector,origin,created_by,"
           "authority_ref,tool,argv_prefix,item_type) VALUES ('second','zz','zz',1,'observe','global',"
           "'github','rule_set:test','system','self','bash','[\"gh\",\"pr\",\"create\"]','other')")
    original_extractor = connectors.artifact_extractor
    monkeypatch.setattr(connectors, "artifact_extractor", lambda connector, item: (
        (lambda _: connectors.ObservedArtifactDecl("github", "pull-request", "owner/repo#43"))
        if item == "other" else original_extractor(connector, item)))
    original_register = observer.register_observed_artifact
    def register(*args, **kwargs):
        if fail_second and kwargs["external_id"] == "owner/repo#43":
            raise ValueError("registration failed")
        return original_register(*args, **kwargs)
    monkeypatch.setattr(observer, "register_observed_artifact", register)
    notified = []
    monkeypatch.setattr("tools.shell.notify_changed", notified.append)
    _stub_process(monkeypatch, stdout=_URL + "\n")
    result = asyncio.run(BashTool().execute("gh pr create", **_direct_shell_context(thread)))
    assert [item["external_id"] for item in notified] == ([PR] if fail_second else [PR, "owner/repo#43"])
    assert ("Warning" in result) == fail_second


@pytest.mark.asyncio
async def test_only_a_rules_writer_turns_off_an_observe_row_and_a_shipped_one_stays_on(isolated_db):
    _agent("a")
    _agent("b")
    _agent("c", project=Q)
    mine = (await _observe_rule("a"))["id"]
    assert (await _tool("b", operation="disable", rule_id=mine)).startswith(
        "Refused: Only the rule's writer can turn off an observe rule")
    assert (await _tool("c", operation="disable", rule_id=mine)).startswith("Refused: no rule")
    shipped = await _tool("a", operation="disable", rule_id="ruleset_github_pr_create")
    assert shipped == "Refused: This is one of CORSIA's built-in rules, so it can't be turned off here."
    assert _rows("SELECT enabled FROM event_rules WHERE id='ruleset_github_pr_create'") == [{"enabled": 1}]
    assert (await _tool("a", operation="disable", rule_id=mine))["enabled"] is False
    assert _run("a", "ship-pr --fast") == (None, [])


@pytest.mark.asyncio
async def test_another_projects_agent_rows_never_cover_this_project(isolated_db):
    _agent("a")
    _agent("c", project=Q)
    await _observe_rule("c")
    assert _run("a", "ship-pr --fast") == (None, [])


@pytest.mark.asyncio
async def test_the_caller_is_told_in_the_shell_result_what_the_rules_did(isolated_db, monkeypatch):
    thread = make_session(get_db(), prefix="told")  # in no project, as the shell tool's own tests are
    _stub_process(monkeypatch, stdout=_URL + "\n")
    result = await BashTool().execute("gh pr create --title t", **_direct_shell_context(thread))
    assert result.startswith(_URL) and "Registered pull request owner/repo#42 to you." in result
    assert "Warning" not in result
    again = await BashTool().execute("gh pr create --title t", **_direct_shell_context(thread))
    assert "pull request owner/repo#42 is already registered to you." in again

    _stub_process(monkeypatch, stdout="no url here\n")
    nothing = await BashTool().execute("gh pr create --title t", **_direct_shell_context(thread))
    assert "Not registered: the command's output named no pull request." in nothing

    _stub_process(monkeypatch, stdout="", stderr="boom", returncode=1)
    failed = await BashTool().execute("gh pr create --title t", **_direct_shell_context(thread))
    assert "Registered" not in failed and "Not registered" not in failed


@pytest.mark.asyncio
async def test_list_explain_and_types_show_both_kinds(isolated_db):
    _agent("a")
    await _tool("a", operation="create", connector="github", event_type="pr.merged", to="self",
                note="merged")
    await _observe_rule("a")
    listed = await _tool("a", operation="list")
    assert sorted(rule["kind"] for rule in listed["rules"]) == ["observe", "route"]
    [route] = [rule for rule in listed["rules"] if rule["kind"] == "route"]
    assert route["event_type"] == "pr.merged"
    assert [rule["id"] for rule in listed["rule_sets"]] == ["ruleset_github_pr_create"]
    assert listed["rule_sets"][0]["argv_prefix"] == ["gh", "pr", "create"]

    _run("a", "ship-pr --fast")
    [why] = (await _tool("a", operation="explain", artifact=PR))["decisions"]
    assert (why["outcome"], why["thread_id"]) == ("registered", "a")
    assert why["explanation"].startswith("rule rule_") and "registered the artifact" in why["explanation"]
    outsider = _agent("c", project=Q)
    assert (await _tool(outsider, operation="explain", artifact=PR))["decisions"] == []

    [github] = [item for item in (await _tool("a", operation="types", connector="github"))["connectors"]]
    [pull] = [item for item in github["artifact_types"] if item["item_type"] == "pull-request"]
    assert pull["extractor"] is True
    assert pull["shipped_commands"] == [{"tool": "bash", "argv_prefix": ["gh", "pr", "create"]}]
    assert any(item["event_type"] == "pr.merged" and "pr_number" in item["filterable_fields"]
               for item in github["event_types"])
    assert (await _tool("a", operation="types", connector="nope")).startswith("Refused: no connector")
    assert {item["connector"] for item in (await _tool("a", operation="types"))["connectors"]} >= {"github"}


def test_explain_distinguishes_a_named_artifact_from_empty_output(isolated_db):
    from events.rule_explain import decision

    _agent("owner")
    _agent("rival")
    _run("owner", "gh pr create")
    _run("rival", "gh pr create")
    _run("rival", "gh pr create", "no URL")
    db = get_db()
    rows = db.execute("SELECT d.*, r.action FROM event_route_decisions d JOIN event_rules r ON r.id=d.rule_id "
                      "WHERE d.thread_id='rival' ORDER BY d.id").fetchall()
    named, empty = (decision(db, row)["explanation"] for row in rows)
    assert "artifact the command's output named" in named and "created" not in named
    assert "artifact" not in empty and "created" not in empty
    db.close()


@pytest.mark.asyncio
async def test_types_shows_exactly_the_enabled_shipped_rows(isolated_db):
    _agent("a")

    async def shown() -> list[dict]:
        [github] = (await _tool("a", operation="types", connector="github"))["connectors"]
        return [command for item in github["artifact_types"] for command in item["shipped_commands"]]

    assert await shown() == [{"tool": "bash", "argv_prefix": ["gh", "pr", "create"]}]
    _write("UPDATE event_rules SET enabled=0 WHERE id='ruleset_github_pr_create'")
    assert await shown() == []  # the row is the only home: nothing declared elsewhere remains
    _write("UPDATE event_rules SET enabled=1, argv_prefix='[\"gh\",\"pr\",\"new\"]' WHERE origin LIKE 'rule_set:%'")
    assert await shown() == [{"tool": "bash", "argv_prefix": ["gh", "pr", "new"]}]
    assert _run("a", "gh pr new", OTHER_URL + "\n")[0]["parent_thread_id"] == "a"  # the observer applies that row


def test_a_registration_the_registry_refuses_is_recorded_and_raised(isolated_db):
    _agent("a")
    _write("UPDATE threads SET user_id='someone-else' WHERE id='a'")
    mk_session("owner")
    _write("UPDATE threads SET user_id='steve' WHERE id='owner'")
    assert _run("owner", "gh pr create")[0] is not None
    with pytest.raises(CommandArtifactRegistrationError):
        _run("a", "gh pr create")
    [why] = _rows("SELECT outcome, reason FROM event_route_decisions WHERE thread_id='a'")
    assert why["outcome"] == "not_registered" and why["reason"]
