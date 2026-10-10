"""Invocation-local project-instruction controls for CORSIA-owned harnesses."""

from __future__ import annotations

import json


_CODEX_PROJECT_DOC_CONTROL = "project_doc_max_bytes=0"
_CODEX_NATIVE_SKILL_CONTROLS = (
    # CORSIA's scoped workspace registry is the sole managed skill source.
    "skills.include_instructions=false",
    "skills.bundled.enabled=false",
)
_CODEX_APP_SERVER_CONTROLS = (
    _CODEX_PROJECT_DOC_CONTROL,
    "features.shell_tool=false",
    "features.code_mode=false",
    "features.sleep_tool=false",
    "features.goals=false",
    "tools.experimental_request_user_input.enabled=false",
    "orchestrator.skills.enabled=false",
    # Codex 0.147 gates this under [tools.update_plan], not [features].
    "tools.update_plan.enabled=false",
    'web_search="disabled"',
    *_CODEX_NATIVE_SKILL_CONTROLS,
)
_CODEX_APP_SERVER_CONTROL_KEYS = frozenset(
    value.partition("=")[0] for value in _CODEX_APP_SERVER_CONTROLS
)
_CODEX_APP_SERVER_DISABLED_FEATURES = (
    "apps",
    "remote_plugin",
    "hooks",
    "shell_snapshot",
    "multi_agent",
    "multi_agent_v2",
    "enable_fanout",
    "image_generation",
    # CORSIA owns memory; Codex-native memories would inject a second store.
    "memories",
)
# CORSIA delivers every instruction a managed Claude Code session receives.
# These match at any depth, so they drop the user, ancestor, working-directory,
# local and rules instruction files alike. Root-specific paths are not enough:
# they miss CLAUDE.local.md and the memory index under a custom config root.
_CLAUDE_INSTRUCTION_EXCLUDES = ("**/CLAUDE.md", "**/CLAUDE.local.md", "**/rules/**")


# Codex's native filesystem sandbox is intentionally launched read-only: the
# harness process is not itself confined by CORSIA's Safehouse gate, so any
# native write surface would reach the real filesystem — including protected
# base checkouts — unmediated. All sanctioned writes therefore flow through the
# CORSIA dynamic tools (bash/write_file/edit_file/multi_edit), which ARE
# write-capable and are gated per call. The read-only *native* sandbox is a
# true statement about native tools, but Codex surfaces it to the model as a
# blanket "workspace is read-only" environment note. Some models take that at
# face value and abandon write-bearing tasks without ever trying their tools.
# This developer instruction reconciles the signal with reality so the model
# uses its write-capable tools instead of concluding the task is impossible.
CODEX_WRITE_PATH_MARKER = "[corsia:write-path]"
CODEX_WRITE_PATH_DEVELOPER_INSTRUCTIONS = (
    f"{CODEX_WRITE_PATH_MARKER} Your native filesystem sandbox is read-only by "
    "design. This does NOT mean the workspace is read-only or that changes are "
    "impossible — only that direct native filesystem writes are disabled. Every "
    "file you create or edit and every shell command you run goes through your "
    "CORSIA tools (bash, write_file, edit_file, multi_edit, "
    "worktree_create), which ARE "
    "write-capable within your authorized workspace and its linked git "
    "worktrees. Never report a task blocked because the native sandbox is "
    "read-only: use your CORSIA tools. When your working directory is a "
    "protected base checkout, create a new branch worktree with the "
    "`worktree_create` tool, which refreshes the remote base first, then make "
    "all changes inside the returned `.worktrees/<...>` directory."
)
# CORSIA dynamic tools that give a session an actual write path. The native
# Codex filesystem is read-only by design, so a task-bearing session is only
# genuinely write-capable when at least one of these is on its tool surface.
_CODEX_WRITE_CAPABLE_TOOL_NAMES = frozenset(
    {"bash", "write_file", "edit_file", "multi_edit"}
)
# Codex native sandbox modes that present the workspace to the model as
# read-only. ``workspace-write`` and ``danger-full-access`` do not.
_CODEX_READ_ONLY_SANDBOX_MODES = frozenset({"read-only", "strict"})


def codex_tools_are_write_capable(tool_names: set[str]) -> bool:
    """Whether a tool surface exposes at least one sanctioned write tool."""
    return bool(tool_names & _CODEX_WRITE_CAPABLE_TOOL_NAMES)


def codex_sandbox_reads_read_only(sandbox_mode: str) -> bool:
    """Whether the native sandbox tells the model the workspace is read-only."""
    return sandbox_mode in _CODEX_READ_ONLY_SANDBOX_MODES


def codex_developer_instructions_with_write_path(
    base: str | None,
    *,
    write_capable: bool,
    read_only: bool,
) -> str | None:
    """Append the write-path clarification when the read-only signal misleads.

    Only a write-capable session under a read-only native sandbox needs it;
    any caller-set instructions are preserved and the clarification is added
    at most once.
    """
    if not (write_capable and read_only):
        return base
    if base and CODEX_WRITE_PATH_MARKER in base:
        return base
    if not base:
        return CODEX_WRITE_PATH_DEVELOPER_INSTRUCTIONS
    return f"{base}\n\n{CODEX_WRITE_PATH_DEVELOPER_INSTRUCTIONS}"


def _codex_config_args(values: tuple[str, ...]) -> list[str]:
    """Render Codex config values as repeated invocation-local overrides."""
    return [part for value in values for part in ("-c", value)]


def codex_governance_config_args() -> list[str]:
    """Render the governance controls shared by every Codex invocation."""
    return _codex_config_args(_CODEX_APP_SERVER_CONTROLS)


def codex_project_discovery_args() -> list[str]:
    """Disable Codex project docs and provider-native skill discovery."""
    return _codex_config_args(
        (_CODEX_PROJECT_DOC_CONTROL, *_CODEX_NATIVE_SKILL_CONTROLS)
    )


def codex_app_server_config_override_args(values: tuple[str, ...]) -> list[str]:
    """Render caller overrides without permitting governance controls to weaken."""
    conflicts = sorted(
        value.partition("=")[0].strip()
        for value in values
        if value.partition("=")[0].strip() in _CODEX_APP_SERVER_CONTROL_KEYS
    )
    if conflicts:
        joined = ", ".join(conflicts)
        raise ValueError(f"Codex config overrides cannot replace governed keys: {joined}")
    return _codex_config_args(values)


def codex_app_server_governance_args() -> list[str]:
    """Disable provider-native actions, skills, search, and project docs.

    These invocation overrides do not isolate arbitrary user/project config;
    that broader claim requires the separate model-facing promotion probe.
    """
    disabled = [
        part
        for feature in _CODEX_APP_SERVER_DISABLED_FEATURES
        for part in ("--disable", feature)
    ]
    overrides = codex_governance_config_args()
    # --strict-config is an upgrade tripwire: if a future Codex release
    # removes or renames one of the governed controls, startup must fail
    # visibly instead of silently restoring a provider-native tool.
    return ["--strict-config", *disabled, *overrides]


def claude_project_discovery_args() -> list[str]:
    """Keep user/local settings but no instruction file or callable capability.

    The user source still carries login and model state. Its instruction
    files, like every other CLAUDE.md, are excluded here, and auto memory is
    off. Paths that pass no settings at all (the reviewer and the isolated
    structured run) rely on ``cc_env.clean_claude_env`` for the same switch.
    """
    settings = json.dumps(
        {
            "autoMemoryEnabled": False,
            "claudeMdExcludes": list(_CLAUDE_INSTRUCTION_EXCLUDES),
            "disableAllHooks": True,
            "disableBundledSkills": True,
            "disableSkillShellExecution": True,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return ["--setting-sources", "user,local", "--settings", settings]
