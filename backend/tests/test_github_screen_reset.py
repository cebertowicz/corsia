"""Regressions for installed screening, provenance and CORSIA-owned failures."""
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from tests.rule_worker_helpers import OWNER, fenced, run_scripts, settle_all, starts, world  # noqa: F401
from tests.test_github_screen_path import _route, screen  # noqa: F401

LABEL = "from an untrusted agent; contains outside content"


def _diff(path, text="hello"):
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -0,0 +1 @@\n+{text}\n"


@pytest.mark.parametrize("hidden", ["\U000e0000", "\U000e007f", "\u2061", "\u2064", "\u00ad"])
def test_hidden_instruction_carriers_are_flagged(hidden):
    from events.connectors.github_screen.floor import floor_hits

    assert "floor.hidden_chars" in floor_hits(_diff("a.txt", hidden + "instructions"))


@pytest.mark.parametrize("suffix", ["py", "sh", "json", "yaml"])
def test_executable_skill_files_are_instructions_but_context_code_is_not(suffix):
    from events.connectors.github_screen.floor import floor_hits

    assert "floor.agent_instructions" in floor_hits(_diff(f"nested/skills/task/file.{suffix}"))
    assert "floor.agent_instructions" not in floor_hits(_diff(f"src/context/file.{suffix}"))
    assert "floor.agent_instructions" in floor_hits(_diff("backend/prompts/custom.txt"))


def test_conductor_designation_installs_screen_without_manual_sync(world):
    import subprocess
    from events.connectors.github_screen import install
    from infra.conductor import set_project_conductor
    from infra.database import get_db
    from infra.project_repo_policy import register_repo_policy

    repo = world["root"] / "source" / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/o/r.git"], check=True)
    db = get_db()
    register_repo_policy(world["project"], "repo", repo, pinned_branch=None,
                         protect_base_checkout=False, db=db)
    db.commit()
    assert set_project_conductor(world["project"], OWNER, db=db)
    key = install._key(world["project"])
    rules = db.execute("SELECT id FROM event_rules WHERE id LIKE ?", (key + "%",)).fetchall()
    ids = {row[0] for row in rules}
    assert {key + "_opened", key + "_synchronize", key + "_start"} <= ids
    assert {key + '_triage_' + suffix for suffix in ('issue_opened', 'issue_comment_created',
            'issue_comment_edited', 'pr_comment_created', 'pr_comment_edited', 'review_submitted',
            'review_edited', 'start')} <= ids
    assert len(ids) == 11
    assert set_project_conductor(world["project"], None, db=db)
    assert not db.execute("SELECT 1 FROM event_rules WHERE id LIKE ?", (key + "%",)).fetchone()


@pytest.mark.asyncio
async def test_result_delivery_renders_sign_text_without_changing_facts(screen):
    from core.injection_rules import INJECTION_RULES
    from infra.database import get_db

    screen["reply"].write_text(fenced({"payload": {"verdict": "safe", "categories": [], "findings": []},
                                       "injection_signs": ["R3", "R4"]}))
    await _route(screen["state"]["sha"])
    await run_scripts()
    await settle_all()
    event = next(item for item in screen["state"]["admitted"] if item["event_type"] == "worker.result")
    assert event["facts"]["injection_signs"] == ["R3", "R4"]
    messages = [row[0] for row in get_db().execute("SELECT content FROM messages WHERE thread_id='pr-owner'")]
    text = next(message for message in messages if "[EVENT: result]" in message)
    for sign, rule in INJECTION_RULES:
        if sign in ("R3", "R4"):
            assert f"{sign} ({rule})" in text


@pytest.mark.parametrize("event_type", ["worker.result", "worker.no_result"])
def test_worker_signs_render_in_wake_and_digest_without_mutating_facts(event_type):
    from core.injection_rules import INJECTION_RULES
    from events.delivery import format_event_input
    from events.envelope import normalize
    from events.platform import _note_line

    facts = {"source": "outside", "injection_signs": ["R3", "R4"]}
    event = normalize("worker", {"run_id": "run-1", **facts}, event_type.removeprefix("worker."))
    wake = format_event_input(event_type, "worker", facts)
    note = _note_line(event, ())
    for sign, rule in INJECTION_RULES:
        if sign in facts["injection_signs"]:
            assert f"{sign} ({rule})" in wake and f"{sign} ({rule})" in note
    assert facts["injection_signs"] == event.facts["injection_signs"] == ["R3", "R4"]


@pytest.mark.asyncio
@pytest.mark.parametrize("valid", [True, False])
async def test_result_and_no_result_keep_label_in_agent_delivery(screen, valid):
    from infra.database import get_db

    if not valid:
        screen["reply"].write_text(fenced({"bad": "result"}))
    await _route(screen["state"]["sha"])
    await run_scripts()
    await settle_all()
    db = get_db()
    delivered = [row[0] for row in db.execute("SELECT content FROM messages WHERE thread_id='pr-owner'")]
    assert delivered and any(LABEL in text for text in delivered), delivered
    event = next(item for item in screen["state"]["admitted"] if item["event_type"].startswith("worker."))
    assert event["facts"]["source"] == "outside"
    assert event["facts"]["injection_signs"] == ([] if valid else None)
    for key in ("source", "item", "item_version", "kind", "injection_signs"):
        assert any(key in text for text in delivered), (key, delivered)
    if not valid:
        requester = [row[0] for row in db.execute("SELECT content FROM messages WHERE thread_id=?", (OWNER,))]
        notice = next(text for text in requester if "[EVENT: worker.no_result]" in text)
        assert LABEL in notice
        for key in ("source", "item", "item_version", "kind", "injection_signs"):
            assert f'"{key}":' in notice
        assert '"injection_signs": null' in notice


@pytest.mark.asyncio
async def test_floor_capture_failure_does_not_start_worker(screen, monkeypatch):
    from automation import worker_runner
    from tools import delegate_task

    await _route(screen["state"]["sha"])
    await run_scripts()
    for path in (screen["folder"] / "staging").glob("screen-*/floor.json"):
        path.write_text("not JSON")
    spawn = AsyncMock(return_value={"id": "must-not-run"})
    monkeypatch.setattr(delegate_task, "_spawn", spawn)
    assert await worker_runner.drive() == 0
    spawn.assert_not_awaited()
    row = starts(screen["key"] + "_start")[0]
    assert row[0] == "refused" and "Couldn’t read" in row[1]
    assert "The run ends without a result; a later event starts a new one" in row[1]
    event = next(item for item in screen["state"]["admitted"] if item["event_type"] == "worker.no_result")
    assert event["facts"]["source"] == "outside" and event["facts"]["injection_signs"] is None
    assert not list((screen["folder"] / "staging").glob("screen-*"))


@pytest.mark.asyncio
async def test_validator_defect_is_corsia_failure_not_model_invalid(screen, monkeypatch):
    from events.connectors.github_screen import result

    def broken(*_args):
        raise TypeError("CORSIA validator defect")

    monkeypatch.setattr(result, "validate_captured_result", broken)
    await _route(screen["state"]["sha"])
    await run_scripts()
    await settle_all()
    row = starts(screen["key"] + "_start")[0]
    assert row[0] == "no_result" and "Couldn’t validate" in row[1]
    event = next(item for item in screen["state"]["admitted"] if item["event_type"] == "worker.no_result")
    assert event["facts"]["reason"] != "invalid"


@pytest.mark.asyncio
async def test_missing_shipped_version_never_uses_workspace_workflow(screen, monkeypatch):
    from automation import worker_runner
    from infra.database import write_transaction
    from infra.db_async import run_in_db
    from tools import delegate_task

    folder = screen["root"] / "skills" / "github-pr-screen"
    folder.mkdir(parents=True)
    original = screen["root"] / "skills" / "worker-flow"
    (folder / "SKILL.md").write_text((original / "SKILL.md").read_text().replace("worker-flow", "github-pr-screen"))
    (folder / "corsia.yaml").write_text((original / "corsia.yaml").read_text())
    await _route(screen["state"]["sha"])
    await run_scripts()

    def stale():
        with write_transaction(None, scope="stale_screen_origin") as db:
            db.execute("UPDATE event_rules SET origin='rule_set:github@missing' WHERE id=?", (screen["key"] + "_start",))

    await run_in_db(stale)
    spawn = AsyncMock(return_value={"id": "must-not-run"})
    monkeypatch.setattr(delegate_task, "_spawn", spawn)
    assert await worker_runner.drive() == 0
    spawn.assert_not_awaited()
    row = starts(screen["key"] + "_start")[0]
    assert row[0] == "refused" and "shipped" in row[1]


@pytest.mark.parametrize("action", ["run_script", "start_worker"])
def test_script_and_worker_inputs_keep_outside_label_and_identity(isolated_db, action):
    from events import rule_runs, worker_starts
    from events.rule_outcomes import Rule, RuleEvent
    from infra.database import get_db

    facts = {"source": "outside", "repo": "o/r", "item": "o/r#1", "item_version": "a" * 40,
             "kind": "pull request", "injection_signs": None, "request_identity": "req"}
    event = RuleEvent("event", "worker", "no_result", "worker.no_result", facts, None, origin="platform")
    rule = Rule("rule", "project:project", "worker", "no_result", (), action, "thread:owner",
                None, None, "owner", workflow_id="flow", request_identity_fact="request_identity")
    queue = rule_runs.queue if action == "run_script" else worker_starts.queue
    [outcome] = queue(get_db(), event, [rule], {})
    delivered = json.loads(outcome.run_input)["event"]
    assert delivered["content_label"] == LABEL
    assert delivered["facts"] == facts and delivered["origin"] == "platform"


@pytest.mark.asyncio
async def test_model_repo_field_cannot_rebind_result_consumer(screen, monkeypatch):
    from dataclasses import replace
    from automation import script_runner, script_schedules
    from automation.script_sandbox import ScriptRun
    from events.connectors.github_screen import workflow
    from infra.db_async import run_in_db
    from tests.rule_worker_helpers import atool

    original = workflow.workflow

    def declaring_repo(project_id):
        skill = original(project_id)
        corsia = json.loads(json.dumps(skill.corsia))
        corsia["workflow"]["result"]["properties"]["payload"]["properties"]["repo"] = {"type": "string"}
        return replace(skill, corsia=corsia)

    monkeypatch.setattr(workflow, "workflow", declaring_repo)
    screen["reply"].write_text(fenced({"payload": {"verdict": "safe", "categories": [], "findings": [],
                                                  "repo": "other/private"}, "injection_signs": []}))
    path = screen["folder"] / "repo-consumer.sh"
    path.write_text("# record input\n")
    schedule = await run_in_db(script_schedules.create_script, OWNER, screen["project"],
                               script_path=path, script_sha256=script_schedules.file_sha256(path),
                               cadence_seconds=None, series=[], timeout_seconds=30,
                               now="2026-01-01T00:00:00+00:00", db=None)
    created = await atool(OWNER, operation="create", connector="worker", event_type="result",
                          action="run_script", script_id=schedule.id,
                          match=[{"field": "requester", "op": "present"}])
    assert isinstance(created, dict), created
    previous = script_runner.run_script
    seen = []

    async def run(thread, project, script, **kwargs):
        if Path(script) == path:
            seen.append((kwargs.get("github_repo"), json.loads(kwargs["stdin"])))
            return ScriptRun(0, b"", b"", False)
        return await previous(thread, project, script, **kwargs)

    monkeypatch.setattr(script_runner, "run_script", run)
    await _route(screen["state"]["sha"])
    await run_scripts()
    await settle_all()
    await run_scripts()
    [(bound_repo, event)] = seen
    assert bound_repo == "o/r"
    assert event["facts"]["repo"] == "o/r"
    assert json.loads(event["facts"]["result"])["repo"] == "other/private"


@pytest.mark.asyncio
async def test_remote_change_refuses_queued_screen_before_opening_channel(screen):
    import subprocess
    from infra.database import get_db

    await _route(screen["state"]["sha"])
    subprocess.run(["git", "-C", str(screen["root"] / "source" / "repo"), "remote", "set-url",
                    "origin", "https://github.com/other/current.git"], check=True)
    await run_scripts()
    [(outcome, reason, _, _)] = starts(screen["key"] + "_opened")
    assert outcome == "failed" and "event repository does not match the tracked repository" in reason
    assert screen["state"]["calls"] == []
    assert starts(screen["key"] + "_start") == []
    row = get_db().execute('SELECT "match" FROM event_rules WHERE id=?',
                           (screen["key"] + "_opened",)).fetchone()
    assert json.loads(row[0])[0]["value"] == ["o/r"]
