"""Observe rules: the rows that register the artifact a shell command created to its caller.

Each test drives the real entrypoints (the ``event_rules`` tool, the shell tool, the observer) on a
disposable database; only the shell's subprocess is stubbed.
"""

from __future__ import annotations

import asyncio
import json
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
    assert told == ["Registered pull-request owner/repo#42 to you."]


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
    assert connected.startswith("Refused:") and "connected app" in connected and "bash" in connected
    unknown = await _observe_rule("a", item_type="calendar-event")
    assert unknown.startswith("Refused: no connector reports on 'calendar-event'")
    assert "pull-request" in unknown
    for stray in ("to", "mode", "note", "action", "match"):
        refused = await _observe_rule("a", **{stray: "thread:a" if stray == "to" else "deliver"})
        assert refused.startswith("Refused:") and stray in refused
    assert (await _observe_rule("a", argv_prefix=[])).startswith("Refused: argv_prefix")
    assert (await _observe_rule("w")).startswith("Refused: A temporary agent can't write rules")
    await _observe_rule("a")
    assert (await _observe_rule("a")).startswith("Refused: You already have that observe rule")
    assert len(_rows("SELECT id FROM event_rules WHERE kind='observe' AND origin LIKE 'agent:%'")) == 1


def test_the_shipped_rule_registers_gh_pr_create_for_every_caller_and_matches_its_declaration(isolated_db):
    _agent("a")
    mk_session("loner")  # in no project: the shipped rule still covers it
    for thread, url, external_id in ("a", _URL, PR), ("loner", OTHER_URL, "owner/repo#43"):
        artifact, told = _run(thread, "gh pr create --title x", url + "\n")
        assert artifact["parent_thread_id"] == thread == _owner(external_id)
        assert told == [f"Registered pull-request {external_id} to you."]
    [shipped] = _rows("SELECT * FROM event_rules WHERE origin LIKE 'rule_set:%'")
    [declared] = connectors.registered_command_triggers("bash")
    connector, trigger = declared
    assert (shipped["scope"], shipped["kind"], shipped["connector"], shipped["tool"]) == (
        "global", "observe", connector, trigger.tool_name)
    assert (json.loads(shipped["argv_prefix"]), shipped["item_type"]) == (
        list(trigger.argv_prefix), trigger.item_type)
    assert shipped["id"] == "ruleset_github_pr_create"
    assert connectors.artifact_extractor(connector, trigger.item_type) is not None


@pytest.mark.asyncio
async def test_an_observe_row_never_takes_an_owned_artifact_from_its_owner(isolated_db):
    _agent("owner")
    _agent("rival")
    assert _run("owner", "gh pr create")[0]["parent_thread_id"] == "owner"
    await _observe_rule("rival")
    claimed, told = _run("rival", "ship-pr --fast")
    assert _owner() == "owner"
    assert claimed is not None and claimed["external_id"] == PR  # the follower's registration is returned
    assert told == ["Not registered to you: pull-request owner/repo#42 already belongs to another chat, "
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
    assert _run("owner", "gh pr create") == (None, ["pull-request owner/repo#42 is already registered to you."])
    again, told = _run("rival", "ship-pr --fast")
    assert (again, _owner()) == (None, "owner")
    assert told == ["Not registered to you: pull-request owner/repo#42 belongs to another chat; "
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
    assert shipped.startswith("Refused: A shipped rule set's rule applies to every agent")
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
    assert result.startswith(_URL) and "Registered pull-request owner/repo#42 to you." in result
    assert "Warning" not in result
    again = await BashTool().execute("gh pr create --title t", **_direct_shell_context(thread))
    assert "pull-request owner/repo#42 is already registered to you." in again

    _stub_process(monkeypatch, stdout="no url here\n")
    nothing = await BashTool().execute("gh pr create --title t", **_direct_shell_context(thread))
    assert "Not registered: the command's output named no pull-request." in nothing

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
