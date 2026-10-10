from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from reviewer.failures import cc_max_turns
from reviewer.reviewer_backends import invoke_cc_subagent, invoke_codex_reviewer
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


@patch("reviewer.reviewer_backends.subprocess.run")
def test_cc_reviewer_uses_conversation_project_control(
    mock_run,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("CLAUDE_BIN", "claude")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://provider.example.test")
    mock_run.return_value = _proc(
        stdout=json.dumps({
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "[]",
        }),
    )

    git_dir = str(tmp_path / "objects.git")
    assert invoke_cc_subagent(
        "review", str(tmp_path), model="opus", source_git_dir=git_dir,
        base_sha=_BASE_SHA, head_sha=_HEAD_SHA,
        env={"ANTHROPIC_BASE_URL": "https://provider.example.test"},
    ) == "[]"

    cmd = mock_run.call_args.args[0]
    kwargs = mock_run.call_args.kwargs
    assert cmd == [
        "claude",
        "-p",
        "--restricted",
        "--safe-mode",
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--permission-mode",
        "dontAsk",
        "--tools",
        "Bash",
        "--allowedTools",
        f"Bash(git --git-dir={git_dir} diff --no-ext-diff --no-textconv --stat "
        f"{_BASE_SHA}...{_HEAD_SHA})",
        f"Bash(git --git-dir={git_dir} diff --no-ext-diff --no-textconv --name-only "
        f"{_BASE_SHA}...{_HEAD_SHA})",
        f"Bash(git --git-dir={git_dir} diff --no-ext-diff --no-textconv --find-renames "
        f"{_BASE_SHA}...{_HEAD_SHA} *)",
        f"Bash(git --git-dir={git_dir} show {_HEAD_SHA}:*)",
        f"Bash(git --git-dir={git_dir} grep -n -F -e * {_HEAD_SHA} -- *)",
        "--add-dir",
        git_dir,
        "--disable-slash-commands",
        "--no-session-persistence",
        "--no-chrome",
        "--model",
        "opus",
        "--max-turns",
        # Not a literal: this test pins the argv *shape*. The budget value is
        # asserted by name in test_reviewer_cc_invocation.py, and hardcoding it
        # here made every budget change break an unrelated argv test.
        cc_max_turns(),
        "--effort",
        "high",
        "--output-format",
        "json",
    ]
    assert "--settings" not in cmd
    assert cmd.count("--setting-sources") == 1
    assert cmd.count("--tools") == 1
    assert cmd.count("--restricted") == 1
    assert cmd.count("--strict-mcp-config") == 1
    assert kwargs["env"]["CLAUDE_CODE_DISABLE_BUNDLED_SKILLS"] == "1"
    assert kwargs["env"]["ANTHROPIC_BASE_URL"] == "https://provider.example.test"
    assert kwargs["env"]["GIT_CONFIG_NOSYSTEM"] == "1"
    assert kwargs["env"]["GIT_CONFIG_GLOBAL"] == "/dev/null"


@patch("reviewer.reviewer_backends.subprocess.run")
def test_codex_reviewer_uses_conversation_project_control(mock_run, tmp_path):
    def run(cmd, **_kwargs):
        output_path = Path(cmd[cmd.index("--output-last-message") + 1])
        output_path.write_text("[]")
        return _proc(stdout='{"type":"turn.completed"}\n')

    mock_run.side_effect = run
    assert invoke_codex_reviewer(
        "review",
        str(tmp_path),
        model="gpt-5.6-sol",
        source_git_dir=str(tmp_path / "objects.git"),
        env={},
    ) == "[]"

    cmd = mock_run.call_args.args[0]
    assert cmd[:8] == [
        "codex",
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--strict-config",
    ]
    assert 'approval_policy="never"' in cmd
    assert 'default_permissions="corsia_reviewer"' in cmd
    assert "permissions.corsia_reviewer.network.enabled=false" in cmd
    filesystem = next(
        value for value in cmd
        if value.startswith("permissions.corsia_reviewer.filesystem=")
    )
    assert f'"{tmp_path.resolve()}"="read"' in filesystem
    assert f'"{(tmp_path / "objects.git").resolve()}"="read"' in filesystem
    assert "--sandbox" not in cmd
    assert cmd.count(_CODEX_PROJECT_CONTROL) == 1
    assert cmd.count(_CODEX_SKILL_INSTRUCTIONS_CONTROL) == 1
    assert cmd.count(_CODEX_BUNDLED_SKILLS_CONTROL) == 1
    assert cmd[cmd.index("--model") + 1] == "gpt-5.6-sol"
    assert cmd[-2:] == ["--", "-"]
    assert cmd.count("--ignore-user-config") == 1
    assert cmd.count("--ignore-rules") == 1
