"""A disposable project for ``start_worker`` rules: a rule owner on a Claude Code model, a
workflow skill that declares a result, a declared request event, and a fake ``claude``
whose every answer is what the test writes to its reply file.

Everything between the event and the worker's result runs for real: the ``event_rules``
tool, intake and rule evaluation, the worker runner, ``delegate_task``'s spawn with the
``review`` confinement, the Claude Code runtime and the settle. Only the owner's own
turn after a wake is recorded instead of run.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import stat
import sys
import textwrap
import uuid
from pathlib import Path

import pytest

from tests.project_ref_helpers import project_ref, select_workspace, write_project_context
from tests.skill_sidecar_helpers import POINTER

OWNER = "rw-owner"
WORKFLOW = "worker-flow"
FINDINGS = {"findings": [{"title": "Off by one", "severity": "major", "line": 7}]}

_DECLARATIONS = """schema: corsia.measures/1
namespace: qa
events:
  qa.request:
    attributes: {request: id, nonce: id, staging_path: id, head_repo: id, head_sha: id}
    dedupe: [nonce]
    use: route
"""

_SIDECAR = """handler: {kind: instruction}
workflow:
  result:
    type: object
    required: [findings]
    properties:
      findings:
        type: array
        items:
          type: object
          required: [title]
          properties:
            title: {type: string}
            severity: {type: string, enum: [blocker, major, minor, nit], unknown: nit}
            line: {type: integer, coerce: true}
            blocking: {type: boolean, default: false}
"""

# Answers each input with the reply file: HANG answers nothing, CRASH exits.
_FAKE = textwrap.dedent(
    '''\
    #!{python}
    import sys, json
    def emit(o):
        sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
    emit({{"type":"system","subtype":"init","session_id":"fake","model":"m","tools":[],"mcp_servers":[],"cwd":"."}})
    for line in sys.stdin:
        if not line.strip():
            continue
        reply = open({reply!r}).read()
        if reply == "HANG":
            continue
        if reply == "CRASH":
            sys.exit(3)
        emit({{"type":"assistant","message":{{"content":[{{"type":"text","text":reply}}]}}}})
        emit({{"type":"result","subtype":"success","is_error":False,"result":reply,"total_cost_usd":0.0,"num_turns":1,"stop_reason":"end_turn"}})
    '''
)


def fenced(payload: object) -> str:
    return f"Here is the review.\n```json\n{json.dumps(payload)}\n```\nDone."


@pytest.fixture
def world(isolated_db, tmp_path, monkeypatch, _cc_runtime_cleanup):
    """The project, owner, skill, declarations and fake binary; yields its handles."""
    import connections.models as cm
    from automation import script_runner, worker_runner
    from infra import config
    from infra.database import get_db, write_transaction
    from infra.measure_registry import REGISTRY, sync

    workspace = Path(config.WORKSPACE).resolve()
    select_workspace(monkeypatch, workspace)
    slug = f"rw-{uuid.uuid4().hex[:8]}"
    root = write_project_context(workspace, slug)
    project = f"project-{slug}"
    with write_transaction(None, scope="rule_worker_seed") as db:
        db.execute("INSERT INTO projects (id, name, path, created_at, updated_at) VALUES (?,?,?,'t','t')",
                   (project, slug, project_ref(slug)))
        db.execute("INSERT INTO threads (id, user_id, title, model, project_id, created_at, updated_at) "
                   "VALUES (?, 'user-1', 'Owner', 'claude-code/sonnet', ?, 't', 't')", (OWNER, project))
    skill = root / "skills" / WORKFLOW
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {WORKFLOW}\ndescription: Review the staged change.\n{POINTER}---\n# Review\nRead it.\n")
    (skill / "corsia.yaml").write_text(_SIDECAR)
    folder = root / ".threads" / OWNER
    (folder / "staging" / "req").mkdir(parents=True)
    (folder / "staging" / "req" / "change.diff").write_text("+ x = 1\n")
    REGISTRY.reset()
    declarations = folder / "measures.yaml"
    declarations.write_text(_DECLARATIONS)
    db = get_db()
    sync(db, owner_thread_id=OWNER, path=str(declarations), text=_DECLARATIONS)
    db.commit()

    reply = tmp_path / "reply"
    reply.write_text(fenced(FINDINGS))
    binary = tmp_path / "fake_claude"
    binary.write_text(_FAKE.format(python=sys.executable, reply=str(reply)))
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    monkeypatch.setattr(cm, "get_connection", lambda provider, db=None: {"config": {"binary": str(binary)}})
    driven: list[str] = []

    async def drive_owner(thread_id):
        driven.append(thread_id)

    monkeypatch.setattr(script_runner, "_drive_owner", drive_owner)
    monkeypatch.setattr(worker_runner, "_state",
                        {"lock": None, "task": None, "again": False, "open": False, "waited": 0})
    yield {"root": root, "folder": folder, "project": project, "reply": reply, "driven": driven,
           "staging": folder / "staging" / "req"}
    shutil.rmtree(root, ignore_errors=True)


async def atool(thread: str, **kwargs) -> dict | str:
    """The ``event_rules`` tool's answer: parsed, or its error text."""
    from tools.event_rules import EventRulesTool

    out = await EventRulesTool().execute(_thread_id=thread, **kwargs)
    return out if out.startswith(("Error", "Refused")) else json.loads(out)


async def make_rule(**kwargs) -> str:
    created = await atool(OWNER, operation="create", connector="qa", event_type="request",
                          action="start_worker", workflow_id=WORKFLOW, request_identity_fact="request",
                          match=[{"field": "request", "op": "present"}], **kwargs)
    assert isinstance(created, dict), created
    return created["id"]


async def request(request: str = "pr-1@a1", **facts) -> str:
    facts = {"request": request, "nonce": uuid.uuid4().hex[:12], "staging_path": "req", **facts}
    emitted = await atool(OWNER, operation="emit", event_type="qa.request",
                          facts={k: v for k, v in facts.items() if v is not None})
    assert isinstance(emitted, dict), emitted
    return emitted["event_id"]


def starts(rule_id: str) -> list[tuple]:
    """The rule's start rows, oldest first: (outcome, reason, identity, worker)."""
    from infra.database import get_db

    return [tuple(row) for row in get_db().execute(
        "SELECT outcome, reason, request_identity, worker_thread_id FROM event_route_decisions "
        "WHERE rule_id=? ORDER BY id", (rule_id,)).fetchall()]


def worker_events() -> list[tuple[str, str]]:
    """The admitted ``worker.*`` events, oldest first: (type, event id)."""
    from infra.database import get_db

    return [(row["event_type"], row["event_id"]) for row in get_db().execute(
        "SELECT event_type, event_id FROM event_intake_records WHERE outcome='accepted' "
        "AND event_type GLOB 'worker.*' ORDER BY id").fetchall()]


def owner_wakes() -> list[str]:
    """The wakes a rule start queued for the rule owner: nothing came back, or no start."""
    from infra.database import get_db

    return [row["content"] for row in get_db().execute(
        "SELECT content FROM messages WHERE thread_id=? AND (content LIKE '%returned no result:%' "
        "OR content LIKE '%A rule couldn’t start %')", (OWNER,)).fetchall()]


def owner_script(world, monkeypatch, name: str) -> str:
    """One of the owner's script schedules, whose sandboxed run only records its stdin
    in ``world["script_inputs"]``; its id."""
    from automation import script_runner, script_schedules
    from automation.script_sandbox import ScriptRun
    from infra.database import get_db

    world.setdefault("script_inputs", [])

    async def fake(thread_id, project_id, path, *, run_id, timeout_seconds, stdin=None, trace=()):
        world["script_inputs"].append(json.loads(stdin) if stdin is not None else None)
        return ScriptRun(0, b"", b"", False)

    monkeypatch.setattr(script_runner, "run_script", fake)
    path = world["folder"] / name
    path.write_text(f"# {name}\n")
    return script_schedules.create_script(
        OWNER, world["project"], script_path=path, script_sha256=script_schedules.file_sha256(path),
        cadence_seconds=None, series=[], timeout_seconds=30, now="2026-01-01T00:00:00+00:00",
        db=get_db(),
    ).id


async def run_scripts() -> None:
    """Run every queued script run to its end."""
    from datetime import datetime, timezone

    from automation import script_runner

    while await script_runner.drive(datetime.now(timezone.utc).isoformat()):
        await asyncio.gather(*script_runner._in_flight.values())


async def settle_all(tries: int = 300) -> None:
    """Drive passes until no start is open, waiting for the workers' turns between them."""
    from automation import worker_runner
    from events import worker_starts
    from infra.db_async import run_in_db

    for _ in range(tries):
        await worker_runner.drive()
        if not await run_in_db(worker_starts.any_open):
            return
        await asyncio.sleep(0.02)
    raise AssertionError("a worker start never settled")
