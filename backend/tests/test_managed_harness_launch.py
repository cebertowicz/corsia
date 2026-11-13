from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from subagents.cc_stream import CCStreamSession
from subagents.codex_app_server import CodexAppServerSession
from subagents.codex_env import governed_codex_model_catalog_path


_CODEX_PROJECT_CONTROL = "project_doc_max_bytes=0"
_CODEX_SHELL_CONTROL = "features.shell_tool=false"
_CODEX_SLEEP_CONTROL = "features.sleep_tool=false"
_CODEX_GOALS_CONTROL = "features.goals=false"
_CODEX_USER_INPUT_CONTROL = "tools.experimental_request_user_input.enabled=false"
_CODEX_SKILLS_TOOL_CONTROL = "orchestrator.skills.enabled=false"
_CODEX_UPDATE_PLAN_CONTROL = "tools.update_plan.enabled=false"
_CODEX_WEB_SEARCH_CONTROL = 'web_search="disabled"'
_CODEX_SKILL_INSTRUCTIONS_CONTROL = "skills.include_instructions=false"
_CODEX_BUNDLED_SKILLS_CONTROL = "skills.bundled.enabled=false"
_CODEX_CHECKPOINT_OVERRIDES = (
    "model_auto_compact_token_limit_scope=body_after_prefix",
    "model_auto_compact_token_limit=200000",
)
_BASE_SHA = "b" * 40
_HEAD_SHA = "a" * 40


def _claude_settings() -> str:
    return json.dumps(
        {
            "autoMemoryEnabled": False,
            "claudeMdExcludes": ["**/CLAUDE.md", "**/CLAUDE.local.md", "**/rules/**"],
            "disableAllHooks": True,
            "disableBundledSkills": True,
            "disableSkillShellExecution": True,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _proc(*, stdout: str = "", stderr: str = "", returncode: int = 0):
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = stderr
    proc.returncode = returncode
    return proc


@pytest.mark.parametrize(
    ("launch_kind", "resume_id", "identity_args"),
    [
        ("main-fresh", None, ["--session-id", "cc-session"]),
        ("main-resume", "provider-thread", ["--resume", "provider-thread"]),
        ("delegated-fresh", None, ["--session-id", "cc-session"]),
        ("delegated-resume", "provider-thread", ["--resume", "provider-thread"]),
    ],
)
def test_managed_cc_conversation_command_matrix(
    tmp_path,
    launch_kind,
    resume_id,
    identity_args,
):
    del launch_kind  # labels the two production call paths sharing this builder
    before = set(tmp_path.iterdir())
    cmd = CCStreamSession(
        binary="claude",
        session_id="cc-session",
        resume_session_id=resume_id,
        seed_file="/tmp/corsia-seed.md",
        mcp_config="/tmp/corsia-mcp.json",
        model="opus",
        effort="high",
        working_dir=str(tmp_path),
        extra_args=["--fallback-model", "sonnet"],
    ).build_cmd()

    assert cmd == [
        "claude",
        "-p",
        "--input-format",
        "stream-json",
        "--replay-user-messages",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--include-hook-events",
        "--permission-mode",
        "bypassPermissions",
        *identity_args,
        "--append-system-prompt-file",
        "/tmp/corsia-seed.md",
        "--setting-sources",
        "user,local",
        "--settings",
        _claude_settings(),
        "--tools",
        "mcp__corsia__*",
        "--disable-slash-commands",
        "--mcp-config",
        "/tmp/corsia-mcp.json",
        "--strict-mcp-config",
        "--model",
        "opus",
        "--effort",
        "high",
        "--fallback-model",
        "sonnet",
    ]
    assert cmd.count("--settings") == 1
    assert cmd.count("--setting-sources") == 1
    assert set(tmp_path.iterdir()) == before


def test_managed_codex_app_server_command_matrix():
    cmd = CodexAppServerSession(
        binary="codex",
        task=None,
        effort="high",
    ).build_cmd()

    assert cmd == [
        "codex",
        "app-server",
        "--stdio",
        "--strict-config",
        "--disable",
        "apps",
        "--disable",
        "remote_plugin",
        "--disable",
        "hooks",
        "--disable",
        "shell_snapshot",
        "--disable",
        "multi_agent",
        "--disable",
        "multi_agent_v2",
        "--disable",
        "enable_fanout",
        "--disable",
        "image_generation",
        "--disable",
        "memories",
        "-c",
        _CODEX_PROJECT_CONTROL,
        "-c",
        _CODEX_SHELL_CONTROL,
        "-c",
        "features.code_mode=false",
        "-c",
        _CODEX_SLEEP_CONTROL,
        "-c",
        _CODEX_GOALS_CONTROL,
        "-c",
        _CODEX_USER_INPUT_CONTROL,
        "-c",
        _CODEX_SKILLS_TOOL_CONTROL,
        "-c",
        _CODEX_UPDATE_PLAN_CONTROL,
        "-c",
        _CODEX_WEB_SEARCH_CONTROL,
        "-c",
        _CODEX_SKILL_INSTRUCTIONS_CONTROL,
        "-c",
        _CODEX_BUNDLED_SKILLS_CONTROL,
        "-c",
        f"model_catalog_json={json.dumps(str(governed_codex_model_catalog_path()))}",
        "-c",
        "model_reasoning_effort=high",
    ]
    assert cmd.count(_CODEX_PROJECT_CONTROL) == 1
    assert cmd.count(_CODEX_SHELL_CONTROL) == 1
    assert cmd.count(_CODEX_SLEEP_CONTROL) == 1
    assert cmd.count(_CODEX_GOALS_CONTROL) == 1
    assert cmd.count(_CODEX_USER_INPUT_CONTROL) == 1
    assert cmd.count(_CODEX_SKILLS_TOOL_CONTROL) == 1
    assert cmd.count(_CODEX_UPDATE_PLAN_CONTROL) == 1
    assert cmd.count(_CODEX_WEB_SEARCH_CONTROL) == 1
    assert cmd.count(_CODEX_SKILL_INSTRUCTIONS_CONTROL) == 1
    assert cmd.count(_CODEX_BUNDLED_SKILLS_CONTROL) == 1
    for feature in (
        "apps", "remote_plugin", "hooks", "shell_snapshot", "multi_agent",
        "multi_agent_v2", "enable_fanout", "image_generation", "memories",
    ):
        assert cmd.count(feature) == 1

    catalog_overrides = [
        value for value in cmd if value.startswith("model_catalog_json=")
    ]
    assert len(catalog_overrides) == 1


@pytest.mark.parametrize(
    ("codex_thread_id", "config_overrides"),
    [
        pytest.param(None, (), id="live"),
        pytest.param(
            None,
            _CODEX_CHECKPOINT_OVERRIDES,
            id="checkpoint-cold",
        ),
        pytest.param(
            "provider-thread",
            _CODEX_CHECKPOINT_OVERRIDES,
            id="checkpoint-warm",
        ),
    ],
)
def test_codex_native_tool_controls_reach_live_cold_and_warm_argv(
    codex_thread_id,
    config_overrides,
):
    cmd = CodexAppServerSession(
        binary="codex",
        task=None,
        codex_thread_id=codex_thread_id,
        config_overrides=config_overrides,
    ).build_cmd()

    for control in (
        _CODEX_SLEEP_CONTROL,
        _CODEX_GOALS_CONTROL,
        _CODEX_USER_INPUT_CONTROL,
        _CODEX_SKILLS_TOOL_CONTROL,
    ):
        assert cmd.count(control) == 1
    # Checkpoint writers must use the same governed catalog as live Codex turns:
    # GPT-5.6 forced Code Mode otherwise emits nested tool callback IDs that
    # never appear in rollout receipt records, so managed tool receipts time out.
    catalog_overrides = [
        value for value in cmd if value.startswith("model_catalog_json=")
    ]
    assert len(catalog_overrides) == 1


def test_codex_model_discovery_process_suppresses_native_skills_without_catalog_pin():
    cmd = CodexAppServerSession(
        binary="codex",
        task=None,
        control_only=True,
    ).build_cmd()

    assert cmd.count("--strict-config") == 1
    assert cmd.count(_CODEX_UPDATE_PLAN_CONTROL) == 1
    assert cmd.count(_CODEX_SKILL_INSTRUCTIONS_CONTROL) == 1
    assert cmd.count(_CODEX_BUNDLED_SKILLS_CONTROL) == 1
    assert not any(value.startswith("model_catalog_json=") for value in cmd)

