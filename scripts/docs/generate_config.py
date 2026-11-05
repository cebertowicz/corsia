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
    "CORSIA_CORRECTIONS_ASSET_BASE_URL": "Sets the download URL for correction assets.",
    "CORSIA_DB_LOOP_WRITE_STRICT": "Makes database writes on the event loop raise an error.",
    "CORSIA_EVAL_TOKEN": "Supplies the API token used for evaluation.",
    "CORSIA_EVAL_TOOL_ALLOWLIST": "Selects which tools are available during evaluation.",
    "CORSIA_GH_AGENT_CONFIG_DIR": "Selects the agents' GitHub account directory; an empty value turns the account off.",
    "CORSIA_GITHUB_HEAD_CHECKS_ENABLE": "Enables GitHub pull request head check events when set to 1.",
    "CORSIA_GITHUB_REPO_ACTIVITY_DISABLE": "Disables GitHub repository activity polling when set to 1.",
    "CORSIA_MAIL_POLLER_DISABLE": "Disables mail polling when set to 1.",
    "CORSIA_MODEL_CATALOG_URL": "Sets the URL for a mirror of the public model catalog.",
    "CORSIA_OTLP_PORT": "Sets the local port that receives measures from external tools.",
    "CORSIA_PROVIDER_STREAM_CAPTURE_DIR": "Sets the directory for captured provider streams.",
    "CORSIA_PROVIDER_STREAM_CAPTURE_THREADS": "Selects chats whose provider streams are captured.",
    "CORSIA_PR_QUIET_WATCH_DISABLE": "Disables the GitHub pull request quiet watch when set to 1.",
    "CORSIA_REVIEWER_MODEL": "Selects the primary review model.",
    "CORSIA_REVIEWER_FALLBACK_MODEL": "Selects the fallback review model.",
    "HERMES_HOME": "Selects the Hermes Agent directory for import.",
    "OPENCLAW_CONFIG_PATH": "Selects the OpenClaw configuration file for import.",
    "OPENCLAW_HOME": "Selects the OpenClaw home directory for import.",
    "OPENCLAW_STATE_DIR": "Selects the OpenClaw state directory for import.",
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
    "CORSIA_DRAIN_MAX_WAIT": "Sets how long shutdown waits for active work to reach a safe point, in seconds.",
    "CORSIA_ENV_FILE": "Selects the launcher environment file.",
    "CORSIA_EVAL_ENDPOINT": "Sets the evaluation endpoint.",
    "CORSIA_EVAL_JUDGE_MODEL": "Selects the evaluation judge model.",
    "CORSIA_EVAL_LOG_DIR": "Selects the evaluation log directory.",
    "CORSIA_EVAL_MODE": "Enables evaluation measurements when set to 1.",
    "CORSIA_EVAL_SUT_MODEL": "Selects the model under evaluation.",
    "CORSIA_EVAL_TIMEOUT": "Sets the evaluation timeout.",
    "CORSIA_GIT_LOCK_STALE_SECONDS": "Sets how many seconds old a Git lock must be before it may be removed.",
    "CORSIA_GITHUB_NOTIFICATIONS_DISABLE": "Disables GitHub notifications polling when set to 1.",
    "CORSIA_HEALTH_WAIT_SECONDS": "Sets the startup health wait in seconds.",
    "CORSIA_LAUNCHD_BOOTSTRAP_ATTEMPTS": "Sets the number of launchd bootstrap attempts.",
    "CORSIA_PRE_MIGRATION_BACKUPS_KEEP": "Sets how many pre-migration backups to keep.",
    "CORSIA_PYTHON": "Selects the Python executable used by the launcher.",
    "CORSIA_RESTART_JITTER_SECONDS": "Sets the random delay applied to restart timing, in seconds.",
    "CORSIA_REVIEWER_MAX_TURNS": "Sets the maximum reviewer turns.",
    "CORSIA_SERVICE_LABEL": "Sets the service label.",
    "CORSIA_SERVICE_MANAGER": "Selects the service manager.",
    "CORSIA_SERVICE_PATH": "Overrides the executable search path used for the service.",
    "CORSIA_SILENT_CHILD_SECONDS": "Sets how many seconds a delegated worker can go quiet before its parent is notified.",
    "CORSIA_STARTUP_CEILING_SECONDS": "Sets the maximum startup wait in seconds.",
    "CORSIA_STARTUP_SECONDS_PER_GIB": "Sets the startup allowance per GiB in seconds.",
    "CORSIA_STATE_DIR": "Selects the runtime state directory.",
    "CORSIA_T1_DEFAULT_BUDGET_SECONDS": "Sets how many seconds an active task can run before its agent is prompted to check it.",
    "CORSIA_TEST_MODE": "Enables test-specific behavior when set to 1.",
    "CORSIA_THREAD_ID": "Identifies the current chat for CLI calls.",
    "CORSIA_VERDICT_OWED_SECONDS": "Sets how many seconds a returned worker's result can await review before its parent is reminded.",
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
    "CORSIA_AGENT_RESTART_ALLOWED",
    "CORSIA_GITHUB_NOTIFICATIONS_TEST_ENABLE", "CORSIA_GITHUB_REPO_ACTIVITY_TEST_ENABLE",
    "CORSIA_HARBOR_BRIDGE_SOCKET", "CORSIA_MAIL_POLLER_TEST_ENABLE",
    "CORSIA_SCRIPT_GITHUB_SOCKET", "CORSIA_SOURCES_SERVICE_TOKEN",
    "CORSIA_BROWSER_INSTANCE_KEY", "CORSIA_CC_TOOL_TOKEN", "CORSIA_CC_TOOL_URL",
    "CORSIA_FORK_ID", "CORSIA_FORK_TOOL_TOKEN",
    "CORSIA_GIT_LOCK_RECOVERY_FAILURE_PREFIX", "CORSIA_INSTALL_FLOW", "CORSIA_INSTALL_LINUX",
    "CORSIA_RESTART_BACKEND_PORT", "CORSIA_RESTART_CALLER_TOKEN", "CORSIA_RESTART_OPERATION_ID",
    "CORSIA_SERVICE_GENERATION", "CORSIA_TOKEN", "SPIKE_CC_MODEL",
}


STANDARD = {"PATH", "HOME", "SHELL", "TZ", "LANG", "LC_ALL", "APPDATA", "LOCALAPPDATA",
            "XDG_CONFIG_HOME", "GH_CONFIG_DIR", "TIKTOKEN_CACHE_DIR", "CUSTOM_TIKTOKEN_CACHE_DIR"}
DEVELOPMENT = {"CORSIA_TEST_MODE", "CORSIA_ALLOW_HOME_WORKSPACE_IN_CI", "CI", "GITHUB_ACTIONS"}
MODEL = {"OMLX_BASE_URL", "CLAUDE_BIN", "CODEX_BIN", "CODEX_HOME"}


def add(found, name, path, default):
    if isinstance(name, str):
        found[name][0].add(path.relative_to(ROOT).as_posix())
        found[name][1].add(default)


def module_tables(paths):
    tables = {}
    for path in paths:
        module = ".".join(path.relative_to(ROOT / "backend").with_suffix("").parts)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        constants, imports = {}, {}
        for node in tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if isinstance(node.value, ast.Constant):
                    for target in targets:
                        if isinstance(target, ast.Name):
                            constants[target.id] = node.value.value
            elif isinstance(node, ast.ImportFrom):
                base = module.split(".")[:-node.level] if node.level else []
                source = ".".join(base + ([node.module] if node.module else []))
                for alias in node.names:
                    imports[alias.asname or alias.name] = (source, alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imports[alias.asname or alias.name.split(".")[0]] = (alias.name, None)
        tables[module] = (tree, constants, imports)
    return tables


def python_reads(path, found, tables):
    module = ".".join(path.relative_to(ROOT / "backend").with_suffix("").parts)
    tree, constants, imports = tables[module]

    def resolve(node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in constants:
                return constants[node.id]
            source, name = imports.get(node.id, (None, None))
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            source, imported = imports.get(node.value.id, (None, None))
            if imported is not None:
                source = f"{source}.{imported}"
            name = node.attr
        else:
            return None
        return tables.get(source, (None, {}, {}))[1].get(name)

    functions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    enclosing = {child: function for function in functions for child in ast.walk(function)}
    for node in ast.walk(tree):
        key = None
        default = "unset"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if ast.unparse(node.func) in {"os.environ.get", "os.getenv"} and node.args:
                key = node.args[0]
                if len(node.args) > 1:
                    value = resolve(node.args[1])
                    default = repr(value) if value is not None or isinstance(node.args[1], ast.Constant) else "computed"
        elif isinstance(node, ast.Subscript) and ast.unparse(node.value) == "os.environ":
            if isinstance(node.ctx, ast.Load):
                key = node.slice
        if key is None:
            continue
        function = enclosing.get(node)
        params = function.args.posonlyargs + function.args.args if function else []
        params += function.args.kwonlyargs if function else []
        names = []
        if isinstance(key, ast.Name) and key.id in [param.arg for param in params]:
            positional = function.args.posonlyargs + function.args.args
            index = next((i for i, param in enumerate(positional) if param.arg == key.id), None)
            for call in calls:
                if isinstance(call.func, ast.Name) and call.func.id == function.name:
                    arg = call.args[index] if index is not None and index < len(call.args) else next(
                        (kw.value for kw in call.keywords if kw.arg == key.id), None)
                    if arg is not None:
                        names.append(resolve(arg))
        else:
            names.append(resolve(key))
        for name in names:
            if isinstance(name, str):
                add(found, name, path, default)


def shell_reads(path, found):
    pattern = r"\$\{((?:CORSIA|BACKEND)_[A-Z0-9_]+)([^}]*)\}"
    for match in re.finditer(pattern, path.read_text(encoding="utf-8")):
        suffix = match.group(2)
        default = suffix[2:] if suffix.startswith(":-") else None
        if default is None:
            default = "unset"
        elif any(char in default for char in "$`{}"):
            default = "computed"
        else:
            default = repr(default)
        add(found, match.group(1), path, default)


def discover():
    found = defaultdict(lambda: (set(), set()))
    paths = sorted((ROOT / "backend").rglob("*.py"))
    tables = module_tables(paths)
    for path in paths:
        relative = path.relative_to(ROOT / "backend")
        if "tests" in relative.parts or relative == Path("conftest.py"):
            continue
        if len(relative.parts) == 1 and path.name.startswith("e2e_"):
            continue
        python_reads(path, found, tables)
    for path in [ROOT / "scripts/corsia", *sorted((ROOT / "scripts/corsia.d").glob("*.sh"))]:
        shell_reads(path, found)
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
    lines = ["# Configuration reference", "", "This page lists names fixed at environment reads in code, including constants passed to a local function one call away. Dynamic names are omitted. "
             "Generate it with `python scripts/docs/generate_config.py`; add `--check` to compare the page.", "",
             "Defaults show fallbacks at read sites. `unset` means no fallback is given; "
             "`computed` means the fallback is an expression. Multiple values come from different read sites.", ""]
    for section in ("CORSIA settings", "Models, browser and review", "Evaluation",
                    "Development and testing", "Standard variables CORSIA reads"):
        names = sorted(name for name in DOCUMENTED if area(name) == section)
        if not names:
            continue
        lines.extend([f"## {section}", "", "| Variable | Default | What it does | Read in |", "|---|---|---|---|"])
        for name in names:
            paths, defaults = found[name]
            locations = ", ".join(f"`{cell(p)}`" for p in sorted(paths))
            values = ", ".join(
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
