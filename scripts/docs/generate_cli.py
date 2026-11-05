#!/usr/bin/env python3
"""Generate the CLI reference from the shell dispatch and argparse definitions."""
from __future__ import annotations

import argparse
import ast
import importlib
import os
from pathlib import Path
import re
import sys
import tempfile

from docs_commands import regenerate_command

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "docs/reference/cli.md"
SHELL = ROOT / "scripts/corsia"
APP_SHELL = ROOT / "desktop/scripts/corsia"
INTERNAL = {"ensure-current-release", "prepare-redeploy", "restart-prepared", "discard-prepared", "redeploy"}
SCRIPTS = {"delegate": "delegate_cli", "doc": "doc_cli", "context": "context_cli", "providers": "providers_cli"}


def shell_commands(text):
    marker = 'case "${1:-status}" in\n    ensure-current-release)'
    if text.count(marker) != 1:
        raise ValueError("Cannot identify shell dispatch")
    dispatch = text[text.index(marker):]
    arms = re.findall(r"^    ([a-z][a-z0-9|-]*)\)", dispatch, re.M)
    commands = {word for arm in arms for word in arm.split("|")}
    if not commands:
        raise ValueError("No shell commands found")
    return commands


def app_commands(text):
    marker = 'case "${1:-status}" in'
    if text.count(marker) != 1:
        raise ValueError("Cannot identify desktop wrapper dispatch")
    block = text.split(marker, 1)[1].split("esac", 1)[0]
    arms = re.findall(r'^  ([a-z][a-z0-9|-]*)\)', block, re.M)
    if not arms:
        raise ValueError("No desktop commands found")
    return {word for arm in arms for word in arm.split("|")}


def shell_help(text, usage):
    # Read literal help only: running the shell sources checkout configuration.
    block = text.split(f'echo "{usage}"', 1)[1].split(';;', 1)[0].split('exit "$help_status"', 1)[0]
    entries = {}
    for line in re.findall(r'^\s*echo "  ([a-z][^"]*)"', block, re.M):
        # A value the shell fills in at run time is shown as a placeholder.
        line = re.sub(r"\$\((\w+)\)", r"`<\1>`", line)
        parts = re.split(r"\s{2,}", line.strip(), maxsplit=1)
        if len(parts) == 2:
            entries.setdefault(parts[0].split()[0], []).append(tuple(parts))
    return entries


def subparsers(parser):
    return next((action for action in parser._actions if isinstance(action, argparse._SubParsersAction)), None)


def module_guard():
    cli_source = ast.parse((ROOT / "backend/cli.py").read_text())
    runtime = next(node for node in cli_source.body if isinstance(node, ast.FunctionDef) and node.name == "_add_runtime_commands")
    calls = {node.func.id for node in ast.walk(runtime)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    imported = {node.module for node in ast.walk(runtime) if isinstance(node, ast.ImportFrom)
                for alias in node.names if (alias.asname or alias.name) in calls}
    parser_modules = set()
    for path in sorted((ROOT / "backend").glob("cli_*.py")):
        tree = ast.parse(path.read_text())
        if any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
               and node.func.attr in {"add_parser", "add_subparsers", "ArgumentParser"}
               for node in ast.walk(tree)):
            parser_modules.add(path.stem)
    missing = parser_modules - imported
    if missing:
        raise ValueError(f"Parser modules not reached: {', '.join(sorted(missing))}")


def capture_parser(module):
    if not hasattr(module, "build_parser"):
        raise ValueError(f"Missing build_parser: {module.__name__}")
    return module.build_parser()


def parsers():
    sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "backend/scripts")]
    import cli
    parser = argparse.ArgumentParser(prog="corsia")
    sub = parser.add_subparsers(dest="cmd")
    cli._add_bootstrap_commands(sub)
    cli._add_runtime_commands(sub)
    result = dict(sub.choices)
    for action in sub._choices_actions:
        if action.help and not result[action.dest].description:
            result[action.dest].description = action.help
    for name, module in SCRIPTS.items():
        result[name] = capture_parser(importlib.import_module(module))
    return result


def cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def describe_parser(lines, parser, command):
    lines.extend([f"### `{command}`", ""])
    if parser.description:
        lines.extend([cell(parser.description), ""])
    actions = [a for a in parser._actions if not isinstance(a, (argparse._SubParsersAction, argparse._HelpAction))]
    if actions:
        lines.extend(["| Argument | Description |", "|---|---|"])
        for action in actions:
            label = ", ".join(action.option_strings) if action.option_strings else action.dest
            if action.nargs not in (None, 0):
                label += f" ({action.nargs})"
            details = []
            if action.help and action.help != argparse.SUPPRESS:
                text = action.help.replace("%(default)s", str(action.default)).replace("%(prog)s", command)
                details.append(text)
            if action.choices is not None:
                details.append("Choices: " + ", ".join(str(x) for x in action.choices))
            if action.required:
                details.append("Required")
            default = action.default
            help_text = details[0].lower() if action.help and action.help != argparse.SUPPRESS else ""
            default_in_help = "default" in help_text and str(default).lower() in help_text
            if (default is not None and default != argparse.SUPPRESS and default != ""
                    and not (isinstance(action, argparse._StoreTrueAction) and default is False)
                    and isinstance(default, (str, int, float, bool)) and not default_in_help):
                details.append(f"Default: {default}")
            description = '; '.join(part.rstrip('.') if i < len(details) - 1 else part
                                    for i, part in enumerate(details))
            lines.append(f"| `{cell(label)}` | {cell(description) or '—'} |")
        lines.append("")
    children = subparsers(parser)
    if children:
        descriptions = {item.dest: item.help for item in children._choices_actions}
        for name in sorted(children.choices):
            child = children.choices[name]
            description = descriptions.get(name)
            if description and not child.description:
                child.description = description
            describe_parser(lines, child, f"{command} {name}")


def render():
    text = SHELL.read_text()
    source_commands = shell_commands(text)
    app_direct = app_commands(APP_SHELL.read_text())
    commands = source_commands | app_direct
    source_usage = "Usage: corsia <command> [--force]"
    source_help = shell_help(text, source_usage)
    app_help = shell_help(text, "Usage: corsia <command>")
    module_guard()
    available = parsers()
    backend_roots = set(available) - set(SCRIPTS)
    if backend_roots - commands:
        raise ValueError(f"Unclassified parser commands: {sorted(backend_roots - commands)}")
    if app_direct - backend_roots:
        raise ValueError(f"Desktop commands lack parsers: {sorted(app_direct - backend_roots)}")
    unknown = {name for name in commands - INTERNAL
               if name not in source_help and name not in app_help
               and (name not in available or not available[name].description)}
    if unknown:
        raise ValueError(f"Shell commands lack descriptions: {', '.join(sorted(unknown))}")
    lines = ["# CLI reference", "", "Generated reference for `corsia` commands.", "",
             f"Generate with `{regenerate_command('generate_cli')}`; add `--check` to compare the generated page. The backend's locked dependencies are required.", ""]
    lines.extend([f"Source-install help: `{source_usage}`.", ""])
    for name in sorted(commands - INTERNAL):
        lines.extend([f"## `{name}`", ""])
        entries = source_help.get(name, [])
        if name not in source_commands:
            lines.extend(["Desktop app only.", ""])
            entries = app_help.get(name, [])
        for usage, description in entries:
            lines.extend([f"`corsia {usage}` — {cell(description)}", ""])
        if name in source_commands and name in app_help:
            descriptions = {description for _, description in entries}
            for _, description in app_help[name]:
                if description not in descriptions:
                    lines.extend([f"In the desktop app: {cell(description)}", ""])
        if name in available:
            describe_parser(lines, available[name], f"corsia {name}")
    lines.extend(["## Internal commands", "", "These commands are for internal operation.", ""])
    for name in sorted(INTERNAL & commands):
        lines.append(f"- `{name}`")
    lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check generated page without writing")
    args = parser.parse_args()
    # Isolate imports that resolve workspace/state paths at module load.
    with tempfile.TemporaryDirectory(prefix="corsia-cli-docs-") as temp:
        workspace = Path(temp) / "workspace"
        state = Path(temp) / "state"
        workspace.mkdir()
        state.mkdir()
        os.environ.update(CORSIA_WORKSPACE=str(workspace), CORSIA_STATE_DIR=str(state),
                          CORSIA_TEST_MODE="1", CORSIA_ANALYTICS_DISABLE="1")
        content = render()
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text() != content:
            print(f"Out of date: {OUTPUT.relative_to(ROOT)}", file=sys.stderr)
            return 1
    else:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
