"""C1 re-test read isolation: what each side's read tools may not observe of its own lesson.

Every call goes through the real ``ToolRegistry.execute`` and the real read tools, inside
``carry_in_tool_isolation(arm.unseen)`` with a real eval thread and admitted Turn. The baseline
sees nothing of the trial lesson; the candidate reads the lesson file as it was delivered; neither
can observe the lesson's case, in its file or in the captured hypothesis it came from.
"""

from __future__ import annotations

import shutil
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from tests.test_carry_in_lifecycle import (  # noqa: F401 - c1 is the shared fixture
    _arm, _make_pending, _pending_fact, _save, _write_lesson, c1,
)
from tests.test_carry_in_retest_delivery import LESSON_BODY

RIGHT_CALL = "Quartz-heron right call 4417."
CASE_MARKS = (RIGHT_CALL, "right_call", "required_actions", "forbidden_actions", "decisive_action", "does_not_apply")
SIDES = ("baseline", "candidate")


@asynccontextmanager
async def _world(c1, monkeypatch):
    """A trial lesson with its case, its captured hypothesis, unrelated files, both sides' arms and a registry."""
    from eval.endpoint import open_eval_thread
    from eval.tool_allowlist import carry_in_tool_isolation
    from infra import state
    from infra.database import get_db
    from infra.thread_driver import admit_input
    from tools import sandbox_config
    from tools.filesystem_edit import ListDirTool
    from tools.filesystem_rw import ReadFileTool
    from tools.glob import GlobTool
    from tools.grep import GrepTool
    from tools.plans import PlanReadTool
    from tools.registry import ToolRegistry

    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RW", [c1.workspace])
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RO", [])
    monkeypatch.setattr(sandbox_config, "_list_authoritative_repo_policies", lambda: [])
    lesson = _write_lesson(c1, text=LESSON_BODY, right_call=RIGHT_CALL).resolve()
    tray = lesson.parents[2]
    capture = tray / "context" / "captures" / "capture-1.md"
    capture.parent.mkdir(parents=True)
    capture.write_text(f"captured hypothesis\nright_call: {RIGHT_CALL}\n")
    other = tray / "skills" / "other-skill" / "SKILL.md"
    other.parent.mkdir(parents=True)
    other.write_text("---\nname: other-skill\n---\nAn unrelated skill.\n")
    unrelated = c1.workspace / "docs" / "unrelated.md"
    unrelated.parent.mkdir()
    unrelated.write_text("heron and distinctive lesson body, an unrelated note\n")
    _save(c1, "rule v1\n")
    await _make_pending(c1)
    fact = _pending_fact()
    arms = {"baseline": _arm(c1, fact, deliver=False), "candidate": _arm(c1, fact, deliver=True)}

    db = get_db()
    user = db.execute("SELECT user_id FROM threads WHERE id=?", (c1.owner,)).fetchone()[0]
    thread_id, _row = await open_eval_thread(
        user, "native/test", thread_id="eval-c1-isolated-b1", parent_thread_id=c1.owner,
    )
    admission = admit_input(
        thread_id, role="user", content="sample", intent="apply_now", message_id="iso-input",
        turn_id="iso-turn", boot_epoch="test-boot", admitted_at="t", db=db,
    )
    assert admission.batch is not None
    registry = ToolRegistry()
    for tool in (
        ReadFileTool(workspace=c1.workspace), GlobTool(workspace=c1.workspace),
        GrepTool(workspace=c1.workspace), ListDirTool(workspace=c1.workspace), PlanReadTool(),
    ):
        registry.register(tool)

    async def run(side, tool, params):
        """One call as ``side``'s sample makes it; ``None`` side is no isolation at all."""
        if side is None:
            return await registry.execute(tool, params)
        with carry_in_tool_isolation(arms[side].unseen):
            return await registry.execute(tool, params)

    thread_token = state._current_thread_id.set(thread_id)
    turn_token = state._current_turn_identity.set(admission.batch.identity)
    try:
        yield SimpleNamespace(
            run=run, arms=arms, lesson=lesson, capture=capture, other=other, unrelated=unrelated,
            tray=tray, workspace=c1.workspace.resolve(),
        )
    finally:
        state._current_turn_identity.reset(turn_token)
        state._current_thread_id.reset(thread_token)


def _everywhere(world):
    """One call of each read tool that could reach the lesson or its case, from several directions."""
    root = str(world.workspace)
    return {
        "read the lesson": ("read_file", {"path": str(world.lesson)}),
        "read the lesson by a relative path": (
            "read_file", {"path": str(world.lesson.relative_to(world.workspace))},
        ),
        "read the capture": ("read_file", {"path": str(world.capture)}),
        "glob the lesson's name": ("glob", {"pattern": "**/SKILL.md", "path": root, "all_threads": True}),
        "glob inside the lesson's directory": ("glob", {"pattern": "*", "path": str(world.lesson.parent), "all_threads": True}),
        "glob the captures": ("glob", {"pattern": "**/*.md", "path": str(world.tray), "all_threads": True}),
        "grep the case": ("grep", {
            "pattern": "heron", "path": root, "all_threads": True, "output_mode": "content",
        }),
        "grep the case, files only": ("grep", {"pattern": "heron", "path": root, "all_threads": True}),
        "grep the body": ("grep", {
            "pattern": "distinctive lesson body", "path": root, "all_threads": True, "output_mode": "content",
        }),
        "grep inside the lesson's file": ("grep", {"pattern": ".", "path": str(world.lesson), "all_threads": True}),
        "list the skills": ("list_dir", {"path": str(world.tray / "skills")}),
        "list the lesson's directory": ("list_dir", {"path": str(world.lesson.parent)}),
        "list the tray's context": ("list_dir", {"path": str(world.tray / "context")}),
        "plan_read": ("plan_read", {}),
    }


@pytest.mark.asyncio
async def test_without_isolation_the_same_calls_do_reach_the_lesson_and_its_case(c1, monkeypatch):
    async with _world(c1, monkeypatch) as world:
        results = {label: await world.run(None, tool, params) for label, (tool, params) in _everywhere(world).items()}
    assert LESSON_BODY in results["read the lesson"] and RIGHT_CALL in results["read the lesson"]
    assert RIGHT_CALL in results["read the capture"]
    assert str(world.lesson.relative_to(world.workspace)) in results["glob the lesson's name"]
    assert "demo-lesson" in results["list the skills"] and "captures" in results["list the tray's context"]
    assert "SKILL.md" in results["grep the case, files only"] or "heron" in results["grep the case"]


@pytest.mark.asyncio
@pytest.mark.parametrize("side", SIDES)
async def test_neither_side_observes_the_lesson_case_through_any_read_tool(c1, monkeypatch, side):
    async with _world(c1, monkeypatch) as world:
        results = {label: await world.run(side, tool, params) for label, (tool, params) in _everywhere(world).items()}
    for label, result in results.items():
        for mark in CASE_MARKS:
            assert mark not in result, (side, label, mark)
        assert "A node closed unread." not in result, (side, label)


@pytest.mark.asyncio
async def test_the_baseline_observes_nothing_of_the_lesson(c1, monkeypatch):
    async with _world(c1, monkeypatch) as world:
        results = {
            label: await world.run("baseline", tool, params) for label, (tool, params) in _everywhere(world).items()
        }
        asked = {label: str(params.get("path", "")) for label, (_tool, params) in _everywhere(world).items()}
    for label, result in results.items():
        answer = result.replace(asked[label], "")  # an error may repeat the path the sample itself asked for
        assert LESSON_BODY not in result and "demo-lesson" not in answer and "captured hypothesis" not in result, label
    assert results["read the lesson"].startswith("Error: File not found")
    assert results["read the lesson by a relative path"].startswith("Error: File not found")
    assert "other-skill" in results["list the skills"]  # its siblings stay


@pytest.mark.asyncio
async def test_the_candidate_reads_the_lesson_body_at_the_delivered_path_without_its_case(c1, monkeypatch):
    async with _world(c1, monkeypatch) as world:
        delivered = str(world.lesson)
        assert LESSON_BODY in world.arms["candidate"].prompt  # a section lesson arrives inline; its file opens too
        read = await world.run("candidate", "read_file", {"path": delivered})
        paged = await world.run("candidate", "read_file", {"path": delivered, "offset": 2, "limit": 3})
        listed = await world.run("candidate", "list_dir", {"path": str(world.lesson.parent)})
    assert LESSON_BODY in read and "name: demo-lesson" in read and "id: " in read
    assert "case:" not in read and "state: trial" in read
    assert "right_call" not in paged and RIGHT_CALL not in paged
    assert "SKILL.md" not in listed  # it opens at its path only; no search finds it


@pytest.mark.asyncio
@pytest.mark.parametrize("side", SIDES)
async def test_unrelated_files_stay_readable_and_searchable_on_both_sides(c1, monkeypatch, side):
    async with _world(c1, monkeypatch) as world:
        root = str(world.workspace)
        read = await world.run(side, "read_file", {"path": str(world.unrelated)})
        other = await world.run(side, "read_file", {"path": str(world.other)})
        globbed = await world.run(side, "glob", {"pattern": "**/*.md", "path": root, "all_threads": True})
        grepped = await world.run(side, "grep", {
            "pattern": "heron", "path": root, "all_threads": True, "output_mode": "content",
        })
        listed = await world.run(side, "list_dir", {"path": str(world.unrelated.parent)})
        skills = await world.run(side, "list_dir", {"path": str(world.tray / "skills")})
    assert "an unrelated note" in read and "An unrelated skill." in other
    assert "docs/unrelated.md" in globbed and "other-skill/SKILL.md" in globbed
    assert "docs/unrelated.md" in grepped and "demo-lesson" not in grepped
    assert "unrelated.md" in listed and "other-skill" in skills


@pytest.mark.asyncio
@pytest.mark.parametrize("side", SIDES)
async def test_a_link_to_the_lesson_gives_a_sample_no_more_than_its_path_does(c1, monkeypatch, side):
    async with _world(c1, monkeypatch) as world:
        link = world.workspace / "docs" / "link.md"
        link.symlink_to(world.lesson)
        result = await world.run(side, "read_file", {"path": str(link)})
        listed = await world.run(side, "list_dir", {"path": str(link.parent)})
    for mark in CASE_MARKS:
        assert mark not in result
    assert ("Error: File not found" in result) == (side == "baseline")
    assert (LESSON_BODY in result) == (side == "candidate")
    assert "link.md" not in listed  # a listing never names what resolves into a hidden path


@pytest.mark.asyncio
async def test_the_grep_fallback_without_ripgrep_hides_the_same_paths(c1, monkeypatch):
    from tools import grep

    monkeypatch.setattr(grep, "_RG_AVAILABLE", False)
    async with _world(c1, monkeypatch) as world:
        results = {
            side: await world.run(side, "grep", {
                "pattern": "heron|distinctive lesson body", "path": str(world.workspace),
                "all_threads": True, "output_mode": "content",
            }) for side in SIDES
        }
    for side, result in results.items():
        assert "docs/unrelated.md" in result, side
        assert "demo-lesson" not in result and RIGHT_CALL not in result and "captured hypothesis" not in result, side


def test_without_case_keeps_the_lesson_and_its_body_and_drops_only_the_case(c1):
    from core import lesson_files

    path = _write_lesson(c1, text=LESSON_BODY, right_call=RIGHT_CALL)
    before = lesson_files.read_trial(path.read_text())
    shown = lesson_files.without_case(path.read_text())
    after = lesson_files.read_trial(shown)
    assert before is not None and after is not None and "case" in before.lesson.case_metadata
    assert "case" not in after.lesson.case_metadata
    assert {k: v for k, v in before.lesson.case_metadata.items() if k != "case"} == after.lesson.case_metadata
    assert (after.lesson.id, after.lesson.state, after.body, after.name) == (
        before.lesson.id, before.lesson.state, before.body, before.name,
    )
    assert after.behavioral_sha256 == before.behavioral_sha256
    assert RIGHT_CALL not in shown and "required_actions" not in shown


@pytest.mark.asyncio
async def test_each_side_hides_the_lesson_directory_and_the_captures_and_only_the_candidate_shows_its_file(c1, monkeypatch):
    async with _world(c1, monkeypatch) as world:
        baseline, candidate = (world.arms[side].unseen for side in SIDES)
    assert baseline.hidden == candidate.hidden == (world.lesson.parent, world.capture.parent)
    assert baseline.shown is None and baseline.view is None
    assert candidate.shown == world.lesson and candidate.view is not None and RIGHT_CALL.encode() not in candidate.view
    shutil.rmtree(world.lesson.parent)  # the arms were fixed when built: nothing later re-reads the disk
    assert world.arms["candidate"].unseen.view == candidate.view
