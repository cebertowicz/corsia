"""A phase added to run a workflow builds the run: one task per step, from the definition."""

from __future__ import annotations

import itertools
import json
import subprocess

import pytest

from infra.database import get_db
from infra.db_fts import _init_messages_fts
from infra.input_admission import accept_thread_input
from plans import service
from plans.workflow_fields import STEPS_CREATED
from tests.event_routing_helpers import mk_session
from tests.plan_execution_helpers import execute_as, open_turn, registry_of
from tests.skill_sidecar_helpers import POINTER
from tools import workflow_task_fields
from tools.plans import PlanUpdateTool

pytestmark = pytest.mark.asyncio

SHIP_BODY = (
    "# Ship\n\n## Drafting\nWrite the draft.\n\n### Detail\nKeep it short.\n\n"
    "```\n## not a heading inside a fence\n```\n\n## Review\nRead it twice.\n"
)
SHIP_STEPS = (
    "    - {id: draft, title: Draft it, required: true, check: Draft exists,"
    " instruction: '## Drafting', difficulty: {default: standard}}\n"
    "    - {id: review, required: true, check: Review passes, after: [draft],"
    " instruction: '## Review', difficulty: {default: routine}}\n"
    "    - {id: polish, after: [review], difficulty: {default: routine}}\n"
)
_IDS = itertools.count()


class Run:
    """One thread with workflow skills in its workspace, driven through the real plan tool."""

    def __init__(self, tmp_path, monkeypatch, skills: dict[str, tuple[str, str]]) -> None:
        workspace = tmp_path / "workspace"
        for name, (body, steps) in skills.items():
            folder = workspace / "skills" / name
            folder.mkdir(parents=True)
            (folder / "SKILL.md").write_text(
                f"---\nname: {name}\ndescription: Workflow {name}\n{POINTER}---\n{body}", encoding="utf-8",
            )
            (folder / "corsia.yaml").write_text(
                f"handler: {{kind: instruction}}\nworkflow:\n  steps:\n{steps}", encoding="utf-8",
            )
        subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
        subprocess.run(["git", "add", "."], cwd=workspace, check=True)
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "skills"],
            cwd=workspace, check=True,
        )
        monkeypatch.setattr(workflow_task_fields.config, "WORKSPACE", workspace)
        self.thread_id = mk_session(f"workflow-run-{next(_IDS)}")
        db = get_db()
        try:
            self.plan_id = service.ensure_plan("thread", self.thread_id, db=db).id
            _init_messages_fts(db)
            accept_thread_input(
                self.thread_id, role="user", content="Ship the release notes.", intent="apply_now",
                message_id=f"workflow-run-authority-{self.thread_id}", db=db,
            )
            self.identity = open_turn(db, self.thread_id)
        finally:
            db.close()
        self.registry = registry_of(PlanUpdateTool())

    async def update(self, operation: str, params: dict, mutation_id: str | None = None):
        """The tool's parsed reply, or its error text."""
        text = await execute_as(
            self.registry, self.identity, self.thread_id, "plan_update",
            {"plan_id": self.plan_id, "mutation_id": mutation_id or f"m-{next(_IDS)}",
             "operation": operation, "params": params},
        )
        return text if text.startswith("Error") else json.loads(text)

    async def start(self, workflow: str, mutation_id: str | None = None, **params):
        return await self.update("add", {
            "kind": "phase", "title": f"Run {workflow}", "acceptance_criteria": "Run recorded",
            "workflow_ref": workflow, **params,
        }, mutation_id)

    def rows(self, sql: str, *args):
        db = get_db()
        try:
            return [dict(row) for row in db.execute(sql, args).fetchall()]
        finally:
            db.close()

    def children(self, parent_id: str):
        return self.rows("SELECT * FROM plan_nodes WHERE parent_id=? ORDER BY order_key", parent_id)

    def revision(self, node_id: str) -> int:
        return self.rows("SELECT revision FROM plan_nodes WHERE id=?", node_id)[0]["revision"]

    def node_count(self) -> int:
        return self.rows("SELECT COUNT(*) AS n FROM plan_nodes WHERE plan_id=?", self.plan_id)[0]["n"]


async def test_a_phase_add_builds_every_step_from_the_definition(isolated_db, tmp_path, monkeypatch):
    run = Run(tmp_path, monkeypatch, {"ship": (SHIP_BODY, SHIP_STEPS)})

    reply = await run.start("ship", "start-ship", size="M")
    phase = reply["node"]
    steps = run.children(phase["id"])

    assert reply["notice"] == STEPS_CREATED
    assert reply["steps"] == [
        {"id": steps[0]["id"], "title": "Draft it", "required": True},
        {"id": steps[1]["id"], "title": "review", "required": True},
        {"id": steps[2]["id"], "title": "polish", "required": False},
    ]
    assert [(row["kind"], row["workflow_step"], row["acceptance_criteria"], row["difficulty"]) for row in steps] == [
        ("task", "draft", "Draft exists", "standard"),
        ("task", "review", "Review passes", "routine"),
        ("task", "polish", "Done: polish", "routine"),
    ]
    assert [row["description"] for row in steps] == [
        "## Drafting\nWrite the draft.\n\n### Detail\nKeep it short.\n\n"
        "```\n## not a heading inside a fence\n```",
        "## Review\nRead it twice.",
        None,
    ]
    assert {(row["workflow_ref"], row["workflow_version"], row["size"], row["intent_node_id"]) for row in steps} == {
        ("ship", phase["workflow_version"], "M", phase["id"]),
    }
    assert phase["workflow_version"] is not None
    assert run.rows(
        "SELECT node_id,depends_on_node_id FROM plan_node_dependencies ORDER BY node_id"
    ) == sorted([
        {"node_id": steps[1]["id"], "depends_on_node_id": steps[0]["id"]},
        {"node_id": steps[2]["id"], "depends_on_node_id": steps[1]["id"]},
    ], key=lambda edge: edge["node_id"])
    created = {
        row["node_id"]: row for row in run.rows(
            "SELECT node_id,actor_id,payload_json FROM plan_node_events WHERE event='node_created'"
        )
    }
    assert {created[row["id"]]["actor_id"] for row in steps} == {None}
    assert [json.loads(created[row["id"]]["payload_json"]).get("workflow_required") for row in steps] == [
        True, True, None,
    ]

    count = run.node_count()
    assert await run.start("ship", "start-ship", size="M") == reply
    assert run.node_count() == count


async def test_a_required_step_closes_only_with_evidence_and_its_check_is_handed_over(
    isolated_db, tmp_path, monkeypatch,
):
    run = Run(tmp_path, monkeypatch, {"ship": (SHIP_BODY, SHIP_STEPS)})
    draft, review, polish = (step["id"] for step in (await run.start("ship"))["steps"])
    skip = "required workflow step draft can't be skipped"

    closed = await run.update(
        "close_node", {"node_id": draft, "expected_revision": run.revision(draft), "reason": "Not needed"},
    )
    archived = await run.update("archive_node", {"node_id": draft, "expected_revision": run.revision(draft)})
    assert skip in closed and skip in archived
    assert await run.update("start", {"node_id": draft, "expected_revision": run.revision(draft)})
    unproven = await run.update("accept", {
        "node_id": draft, "expected_revision": run.revision(draft), "verification_result": "passed",
    })
    assert "finished active work needs evidence" in unproven

    done = await run.update("mark_done", {
        "node_id": draft, "expected_revision": run.revision(draft), "verification_result": "passed",
        "evidence": "Draft is in shared/notes.md", "next_node_id": review,
    })

    assert (done["next"]["selected"]["id"], done["next"]["selected"]["how"]) == (review, "started")
    assert done["next"]["selected"]["acceptance_criteria"] == "Review passes"
    assert done["selected_description"] == {"node_id": review, "text": "## Review\nRead it twice.", "cut": False}

    skipped = await run.update(
        "close_node", {"node_id": polish, "expected_revision": run.revision(polish), "reason": "Nothing to polish"},
    )
    assert skipped["node"]["state"] == "closed"


async def test_a_nested_workflow_step_builds_its_own_run(isolated_db, tmp_path, monkeypatch):
    run = Run(tmp_path, monkeypatch, {
        "outer": ("# Outer\n", (
            "    - {id: design, workflow: inner, required: true, check: Design approved,"
            " difficulty: {default: hard}}\n"
            "    - {id: ship, after: [design], difficulty: {default: routine}}\n"
        )),
        "inner": ("# Inner\n", "    - {id: sketch, required: true, check: Sketch exists}\n"),
    })

    reply = await run.start("outer", difficulty="standard")
    design, ship = run.children(reply["node"]["id"])
    (sketch,) = run.children(design["id"])

    assert [step["title"] for step in reply["steps"]] == ["design", "ship"]
    assert reply["steps"][0]["steps"] == [{"id": sketch["id"], "title": "sketch", "required": True}]
    assert (design["kind"], design["workflow_ref"], design["workflow_step"]) == ("phase", "inner", "design")
    assert (sketch["workflow_ref"], sketch["workflow_version"]) == ("inner", design["workflow_version"])
    assert design["workflow_version"] is not None
    # An unrated step takes the difficulty of the run it belongs to: the nested one's.
    assert sketch["difficulty"] == "hard" and ship["difficulty"] == "routine"


@pytest.mark.parametrize(("skills", "refusal"), [
    (
        {"orphan": ("# Orphan\n", "    - {id: plan, workflow: missing, difficulty: {default: routine}}\n")},
        "step plan names workflow missing, which isn't available here",
    ),
    (
        {
            "loop-a": ("# A\n", "    - {id: next, workflow: loop-b, difficulty: {default: routine}}\n"),
            "loop-b": ("# B\n", "    - {id: back, workflow: loop-a, difficulty: {default: routine}}\n"),
        },
        "step back names workflow loop-a, which already runs above it: loop-a -> loop-b -> loop-a",
    ),
    (
        {"headless": ("# Headless\n## Elsewhere\nText.\n", (
            "    - {id: write, instruction: '## Nowhere', difficulty: {default: routine}}\n"
        ))},
        "step write names instruction '## Nowhere', which headless's SKILL.md doesn't have",
    ),
    (
        {"unrated": ("# Unrated\n", "    - {id: guess}\n")},
        "step guess: workflow steps require difficulty",
    ),
])
async def test_a_run_that_cannot_be_built_creates_nothing(isolated_db, tmp_path, monkeypatch, skills, refusal):
    run = Run(tmp_path, monkeypatch, skills)

    refused = await run.start(next(iter(skills)))

    assert isinstance(refused, str) and refusal in refused
    assert run.node_count() == 0
