#!/usr/bin/env python3
"""Generate the environment configuration reference from named reads."""
from __future__ import annotations

import argparse
import ast
from collections import defaultdict
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "docs/reference/configuration.md"

# Dynamic keys intentionally excluded (file:function; repeated calls grouped):
# app_startup_runtime.py:_start_current_corrections_classifier; cli_events.py:_trace
# cli_github.py:read; cli_sources.py:_read_token; connections/github_broker.py:_scrub
# core/corrections_asset_download.py:base_url; e2e_mock.py:seed_mock_catalog
# eval/credentials.py:resolve_eval_token; eval/harbor_tools.py:execute,register_harbor_bridge_tools
# eval/runner.py:_default_judge_model; eval/tool_allowlist.py:eval_tool_allowlist
# events/connectors/github_head_checks.py:hook_disabled_reason
# events/connectors/github_notifications.py:_static_reason
# events/connectors/github_pr_quiet_watch.py:disabled_reason
# events/connectors/github_repo_activity.py:disabled_reason
# events/connectors/mail_poller.py:disabled_reason
# host_access/browser_session_commands.py:_script_config
# infra/db_loop_guard.py:guard_write_off_event_loop
# infra/migration_106_to_107_key_store_credentials.py:_collect
# infra/otlp_listener.py:_ports; infra/restart_authorization.py:agent_restart_allowed
# llm/public_model_catalog.py:load_catalog; onboarding/agent_import.py:_managed_homes
# reviewer/reviewer_runner.py:_resolve_model; subagents/stream_capture.py:capture_dir_for,_allowlist
# tools/mcp_import_agents.py:_environment

# Each entry is an explicit reviewed classification. Descriptions are kept here
# so a new environment read must be classified before the page can be generated.
DOCUMENTED = {
    "AGENT_BROWSER_ARGS": "Passes arguments to the browser process.",
    "AGENT_BROWSER_AUTO_CONNECT": "Controls automatic connection to a browser.",
    "AGENT_BROWSER_CDP": "Selects a browser debugging endpoint.",
    "AGENT_BROWSER_CONFIG": "Selects browser configuration.",
    "AGENT_BROWSER_ENGINE": "Selects the browser engine.",
    "AGENT_BROWSER_EXECUTABLE_PATH": "Selects the browser executable.",
    "AGENT_BROWSER_PROVIDER": "Selects the browser provider.",
    "APPDATA": "Provides the Windows application data directory.",
    "BACKEND_LOG": "Sets the backend log path in the launcher.",
    "BACKEND_PORT": "Sets the backend port.",
    "CI": "Identifies a CI environment for workspace checks.",
    "CLAUDE_BIN": "Selects the Claude executable.",
    "CODEX_BIN": "Selects the Codex executable.",
    "CODEX_HOME": "Selects the Codex home directory.",
    "CORSIA_AGENT_BROWSER_VERSION_RANGE": "Sets the accepted agent-browser version range.",
    "CORSIA_ALLOW_HOME_WORKSPACE_IN_CI": "Allows a home workspace in CI or tests.",
    "CORSIA_ANALYTICS_DISABLE": "Disables product analytics when set to 1.",
    "CORSIA_ANALYTICS_PROJECT": "Sets the project identifier for product events.",
    "CORSIA_API_BASE": "Sets the CORSIA API base URL for CLI clients.",
    "CORSIA_API_TOKEN": "Supplies an API token to CLI clients.",
    "CORSIA_API_TOKEN_FILE": "Selects the file containing the CLI API token.",
    "CORSIA_BACKUP_FREE_FLOOR_BYTES": "Sets the free-space floor for migration backups.",
    "CORSIA_CLIENT_METADATA_URL": "Sets the OAuth client metadata document URL.",
    "CORSIA_CORRECTIONS_ASSET_DIR": "Selects the corrections asset directory.",
    "CORSIA_DB_POOL_WORKERS": "Sets the asynchronous database worker count.",
    "CORSIA_DEVICES_DISABLE": "Disables the devices listener when set to 1.",
    "CORSIA_DIR": "Specifies the launcher checkout, which must match the script checkout.",
    "CORSIA_DRAIN_MAX_WAIT": "Sets the maximum wait for draining.",
    "CORSIA_ENV_FILE": "Selects the launcher environment file.",
    "CORSIA_EVAL_ENDPOINT": "Sets the evaluation endpoint.",
    "CORSIA_EVAL_JUDGE_MODEL": "Selects the evaluation judge model.",
    "CORSIA_EVAL_LOG_DIR": "Selects the evaluation log directory.",
    "CORSIA_EVAL_MODE": "Controls evaluation-mode telemetry.",
    "CORSIA_EVAL_SUT_MODEL": "Selects the model under evaluation.",
    "CORSIA_EVAL_TIMEOUT": "Sets the evaluation timeout.",
    "CORSIA_GIT_LOCK_STALE_SECONDS": "Sets the age at which a Git lock may be considered stale.",
    "CORSIA_GITHUB_NOTIFICATIONS_DISABLE": "Disables GitHub notifications polling when set to 1.",
    "CORSIA_HEALTH_WAIT_SECONDS": "Sets the startup health wait in seconds.",
    "CORSIA_LAUNCHD_BOOTSTRAP_ATTEMPTS": "Sets the number of launchd bootstrap attempts.",
    "CORSIA_PRE_MIGRATION_BACKUPS_KEEP": "Sets how many pre-migration backups to keep.",
    "CORSIA_PYTHON": "Selects the Python executable used by the launcher.",
    "CORSIA_RESTART_JITTER_SECONDS": "Sets restart jitter in seconds.",
    "CORSIA_REVIEWER_MAX_TURNS": "Sets the maximum reviewer turns.",
    "CORSIA_SERVICE_LABEL": "Sets the service label.",
    "CORSIA_SERVICE_MANAGER": "Selects the service manager.",
    "CORSIA_SERVICE_PATH": "Overrides the executable search path used for the service.",
    "CORSIA_SILENT_CHILD_SECONDS": "Sets the threshold for a silent child in seconds.",
    "CORSIA_STARTUP_CEILING_SECONDS": "Sets the startup ceiling in seconds.",
    "CORSIA_STARTUP_SECONDS_PER_GIB": "Sets the startup allowance per GiB in seconds.",
    "CORSIA_STATE_DIR": "Selects the runtime state directory.",
    "CORSIA_T1_DEFAULT_BUDGET_SECONDS": "Sets the default continuation watchdog budget in seconds.",
    "CORSIA_TEST_MODE": "Enables test-specific behavior when set to 1.",
    "CORSIA_THREAD_ID": "Identifies the current thread for CLI calls.",
    "CORSIA_VERDICT_OWED_SECONDS": "Sets the delegated verdict threshold in seconds.",
    "CORSIA_WORKFLOW_JUDGE_MODEL": "Selects the workflow quality judge model.",
    "CORSIA_WORKSPACE": "Selects the workspace directory.",
    "CUSTOM_TIKTOKEN_CACHE_DIR": "Selects a custom tokenization cache directory.",
    "GH_CONFIG_DIR": "Selects the GitHub CLI configuration directory.",
    "GITHUB_ACTIONS": "Identifies GitHub Actions for workspace checks.",
    "HOME": "Provides the home directory.",
    "LANG": "Provides locale information for region detection.",
    "LC_ALL": "Provides locale information for region detection.",
    "LITELLM_LOCAL_MODEL_COST_MAP": "Controls the local LiteLLM model cost map.",
    "LOCALAPPDATA": "Provides the Windows local application data directory.",
    "OMLX_BASE_URL": "Sets the oMLX endpoint for the reviewer.",
    "PATH": "Provides executable search paths.",
    "SHELL": "Selects the user's shell.",
    "TIKTOKEN_CACHE_DIR": "Selects the tokenization cache directory.",
    "TZ": "Provides the timezone for provider usage limits.",
    "XDG_CONFIG_HOME": "Selects the XDG configuration directory.",
}
INTERNAL = {
    "CORSIA_BROWSER_INSTANCE_KEY", "CORSIA_CC_TOOL_TOKEN", "CORSIA_CC_TOOL_URL",
    "CORSIA_FORK_ID", "CORSIA_FORK_TOOL_TOKEN",
    "CORSIA_GIT_LOCK_RECOVERY_FAILURE_PREFIX", "CORSIA_INSTALL_FLOW", "CORSIA_INSTALL_LINUX",
    "CORSIA_RESTART_BACKEND_PORT", "CORSIA_RESTART_CALLER_TOKEN", "CORSIA_RESTART_OPERATION_ID",
    "CORSIA_SERVICE_GENERATION", "CORSIA_TOKEN", "SPIKE_CC_MODEL",
}


# Effective fallbacks whose read sites use constants or computed expressions.
DEFAULTS = {
    "AGENT_BROWSER_ENGINE": "Browser config engine; otherwise chrome",
    "AGENT_BROWSER_PROVIDER": "Browser config provider; otherwise unset",
    "BACKEND_LOG": "Source: $CORSIA_STATE_DIR/backend.log; desktop: $CORSIA_STATE_DIR/logs/service.log",
    "BACKEND_PORT": "1956",
    "CODEX_HOME": "~/.codex",
    "CORSIA_AGENT_BROWSER_VERSION_RANGE": ">=0.27.0,<0.28.0 or >=0.31.0,<0.32.0",
    "CORSIA_ANALYTICS_PROJECT": "corsia",
    "CORSIA_API_BASE": "CLI: loopback on BACKEND_PORT (1956 by default); API script: local default port",
    "CORSIA_DIR": "The launcher's checkout directory",
    "CORSIA_BACKUP_FREE_FLOOR_BYTES": "10737418240",
    "CORSIA_ENV_FILE": "The checkout's .env file",
    "CORSIA_EVAL_ENDPOINT": "Local loopback URL on port 1956, path /api/eval/turn",
    "CORSIA_EVAL_LOG_DIR": "~/.corsia/logs/eval",
    "CORSIA_PYTHON": "The checkout's .venv/bin/python",
    "CORSIA_SERVICE_PATH": "~/.local/bin, /opt/homebrew/bin, /usr/local/bin, /usr/bin, /bin, /usr/sbin, /sbin",
    "CORSIA_STATE_DIR": "Bootstrap state root; without bootstrap, the platform default state root. "
                        "A workspace different from the bootstrapped workspace requires an explicit state directory.",
    "CORSIA_WORKSPACE": "Install bootstrap workspace; if no bootstrap exists, startup fails.",
    "GH_CONFIG_DIR": "~/.config/gh",
    "SHELL": "/bin/zsh for the login shell probe",
    "XDG_CONFIG_HOME": "~/.config when HOME is set; otherwise empty (Git state cache)",
}

STANDARD = {"PATH", "HOME", "SHELL", "TZ", "LANG", "LC_ALL", "APPDATA", "LOCALAPPDATA",
            "XDG_CONFIG_HOME", "GH_CONFIG_DIR", "TIKTOKEN_CACHE_DIR", "CUSTOM_TIKTOKEN_CACHE_DIR"}
DEVELOPMENT = {"CORSIA_TEST_MODE", "CORSIA_ALLOW_HOME_WORKSPACE_IN_CI", "CI", "GITHUB_ACTIONS"}
MODEL = {"OMLX_BASE_URL", "CLAUDE_BIN", "CODEX_BIN", "CODEX_HOME"}


def add(found, name, path, default):
    if isinstance(name, str):
        found[name][0].add(path.relative_to(ROOT).as_posix())
        found[name][1].add(default)


def python_reads(path, found):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        name = None
        default = "unset"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if ast.unparse(node.func) in {"os.environ.get", "os.getenv"} and node.args:
                name = node.args[0]
                if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                    value = node.args[1].value
                    default = "loopback URL on port 10000" if value == "http://" + "localhost:10000" else repr(value)
        elif isinstance(node, ast.Subscript) and ast.unparse(node.value) == "os.environ":
            if isinstance(node.ctx, ast.Load):
                name = node.slice
        if isinstance(name, ast.Constant) and isinstance(name.value, str):
            add(found, name.value, path, default)


def shell_reads(path, found):
    pattern = r"\$\{((?:CORSIA|BACKEND)_[A-Z0-9_]+)([^}]*)\}"
    for match in re.finditer(pattern, path.read_text(encoding="utf-8")):
        suffix = match.group(2)
        default = suffix[2:] if suffix.startswith(":-") else None
        if default is None or any(char in default for char in "$`{}"):
            default = "unset"
        else:
            default = repr(default)
        add(found, match.group(1), path, default)


def discover():
    found = defaultdict(lambda: (set(), set()))
    for path in sorted((ROOT / "backend").rglob("*.py")):
        relative = path.relative_to(ROOT / "backend")
        if "tests" in relative.parts or relative == Path("conftest.py"):
            continue
        if len(relative.parts) == 1 and path.name.startswith("e2e_"):
            continue
        python_reads(path, found)
    for path in [ROOT / "scripts/corsia", *sorted((ROOT / "scripts/corsia.d").glob("*.sh"))]:
        shell_reads(path, found)
    example = ROOT / ".env.example"
    for name in re.findall(r"^\s*(?:#\s*)?(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", example.read_text(), re.M):
        add(found, name, example, "unset")
    return found


def cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def area(name):
    if name in STANDARD:
        return "Standard variables CORSIA reads"
    if name in DEVELOPMENT:
        return "Development and testing"
    if name.startswith(("CORSIA_EVAL_", "CORSIA_WORKFLOW_JUDGE_MODEL")):
        return "Evaluation"
    if name in MODEL or name.startswith(("AGENT_BROWSER_", "CORSIA_AGENT_BROWSER_", "LITELLM_", "CORSIA_REVIEWER_")):
        return "Models, browser and review"
    return "CORSIA settings"


def render():
    found = discover()
    classified = set(DOCUMENTED) | INTERNAL
    if set(DOCUMENTED) & INTERNAL or set(found) != classified:
        raise ValueError(f"Unclassified: {sorted(set(found) - classified)}; "
                         f"stale: {sorted(classified - set(found))}; "
                         f"overlap: {sorted(set(DOCUMENTED) & INTERNAL)}")
    stale_defaults = set(DEFAULTS) - set(found)
    if stale_defaults:
        raise ValueError(f"Stale default overrides: {sorted(stale_defaults)}")
    lines = ["# Configuration reference", "", "This page lists environment variables read by name in code. "
             "Generate it with `python scripts/docs/generate_config.py`; add `--check` to compare the page.", "",
             "Defaults show effective values where known; otherwise they show literal fallbacks at read sites. "
             "`unset` means no literal fallback is given. Multiple values come from different read sites.", ""]
    for section in ("CORSIA settings", "Models, browser and review", "Evaluation",
                    "Development and testing", "Standard variables CORSIA reads"):
        names = sorted(name for name in DOCUMENTED if area(name) == section)
        if not names:
            continue
        lines.extend([f"## {section}", "", "| Variable | Default | What it does | Read in |", "|---|---|---|---|"])
        for name in names:
            paths, defaults = found[name]
            locations = ", ".join(f"`{cell(p)}`" for p in sorted(paths))
            values = cell(DEFAULTS[name]) if name in DEFAULTS else ", ".join(
                f"`{cell('empty' if d == repr('') else d)}`" for d in sorted(defaults))
            lines.append(f"| `{cell(name)}` | {values} | {cell(DOCUMENTED[name])} | {locations} |")
        lines.append("")
    lines.extend(["", "## Internal and test variables", "", "These variables are used internally or for tests.", ""])
    lines.extend(f"- `{name}`" for name in sorted(INTERNAL))
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    content = render()
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text(encoding="utf-8") != content:
            print(f"Out of date: {OUTPUT.relative_to(ROOT)}", file=sys.stderr)
            return 1
    else:
        OUTPUT.write_text(content, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
