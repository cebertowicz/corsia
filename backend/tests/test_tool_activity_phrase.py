"""Tool-call row phrases: one label per call on every path."""

import pytest

from tools.catalog import TOOL_CATALOG
from tools.tool_activity import ToolActivity, tool_activity, tool_display_activity


def test_every_catalog_tool_declares_its_verb():
    assert sorted(name for name, entry in TOOL_CATALOG.items() if not entry.activity) == []


def test_every_catalog_tool_phrase_hides_raw_name():
    for name in TOOL_CATALOG:
        assert name not in tool_activity(name, {}).phrase


@pytest.mark.parametrize(("name", "arguments", "phrase"), [
    ("write_file", {"path": "/a/b/notes.md"}, "Writing notes.md"),
    ("Write", {"file_path": "/a/notes.md"}, "Writing notes.md"),
    ("read_file", {"path": "/a/b/notes.md"}, "Reading notes.md"),
    ("web_fetch", {"url": "https://x.dev"}, "Fetching https://x.dev"),
    ("search_knowledge", {"query": "folds"}, "Searching knowledge folds"),
    ("plan_read", {}, "Reading plan"),
    ("request_checkpoint", {}, "Requesting checkpoint"),
    ("Bash", {"command": "git status"}, "Running git status"),
    ("Bash", {"command": "cd /srv/work && cd '/repo && scratch'; grep -rn needle"}, "Running grep -rn needle"),
])
def test_phrase_names_the_real_action(name, arguments, phrase):
    assert tool_activity(name, arguments).phrase == phrase


def test_object_is_the_named_argument_unshortened():
    assert tool_activity("read_file", {"path": "/a/b/notes.md"}) == ToolActivity(
        "Reading notes.md", "/a/b/notes.md",
    )
    command = "cd /srv/work && grep -rn needle"
    assert tool_activity("Bash", {"command": command}) == ToolActivity("Running grep -rn needle", command)
    assert tool_activity("plan_read", {}).object == ""


def test_bridged_call_gets_the_inner_tool_phrase_live_and_on_reload():
    from core.message_display import _display_tool_call

    bridged = {"name": "bash", "arguments": {"command": "git status"}}
    live = tool_display_activity("mcp__corsia__tool_call", bridged)
    stored = _display_tool_call({
        "id": "c1", "name": "mcp__corsia__tool_call", "args": bridged,
    })
    assert live == tool_display_activity(stored["name"], stored["args"])
    assert live == ToolActivity("Running git status", "git status")


def test_partial_streamed_arguments_still_render_the_verb():
    assert tool_display_activity("write_file", '{"path": "/a/no').phrase == "Writing"


def test_uncatalogued_tool_is_humanized():
    assert tool_activity("custom_thing", {}).phrase == "Custom thing"
    assert tool_activity("widget_open", {}).phrase == "Opening widget"


def test_harness_builtin_outside_the_catalog_is_humanized():
    assert tool_activity("Grep", {"pattern": "TODO"}).phrase == "Grep TODO"


@pytest.mark.parametrize(("name", "arguments", "phrase"), [
    (
        "read_file",
        {"path": "/w/.corsia/skills/task-discipline/SKILL.md"},
        "Reading task-discipline skill",
    ),
    (
        "Read",
        {"file_path": "/w/.corsia/skills/github/SKILL.md"},
        "Reading github skill",
    ),
    ("edit_file", {"path": "/w/skills/github/SKILL.md"}, "Editing github skill"),
    ("write_file", {"path": "/w/skills/github/SKILL.md"}, "Writing github skill"),
    (
        "read_file",
        {"path": "/w/skills/sdlc-process/references/code-health.md"},
        "Reading sdlc-process skill (references/code-health.md)",
    ),
    (
        "Read",
        {"file_path": "/w/projects/p/.threads/af204877/skills/server-log-triage/SKILL.md"},
        "Reading server-log-triage skill",
    ),
    ("read_file", {"path": "/w/backend/tools/tool_activity.py"}, "Reading tool_activity.py"),
    ("read_file", {"path": "/w/skills/SKILL.md"}, "Reading SKILL.md"),
])
def test_skill_paths_name_the_skill(name, arguments, phrase):
    assert tool_activity(name, arguments).phrase == phrase


def test_skill_row_keeps_the_unshortened_path_as_its_object():
    assert tool_activity("read_file", {"path": "/w/skills/github/SKILL.md"}) == ToolActivity(
        "Reading github skill", "/w/skills/github/SKILL.md",
    )
