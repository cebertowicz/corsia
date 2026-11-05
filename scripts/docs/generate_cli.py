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
# Parser roots defined but dispatched by neither wrapper. Fail when this goes stale.
UNROUTED = set()
INTERNAL = {"ensure-current-release", "prepare-redeploy", "restart-prepared", "discard-prepared", "redeploy"}
# These dispatch arms do not have an entry in the shell help.
EXTRA = {
    "doc": "Comment on and review documents",
    "local": "Set up a local model",
    "agent": "Prepare the CORSIA agent's first reply",
    "ensure-current-release": "Ensure the frontend release is current",
    "prepare-redeploy": "Prepare a redeploy",
    "restart-prepared": "Restart a prepared release",
    "discard-prepared": "Discard a prepared release",
}
SCRIPTS = {"delegate": "delegate_cli", "doc": "doc_cli", "context": "context_cli", "providers": "providers_cli"}
# Shared descriptions where source and desktop help differ.
SHARED_DESCRIPTIONS = {
    "start": "Start CORSIA; --open also checks model sources and opens the browser",
    "open": "Open CORSIA in the browser",
    "stop": "Stop CORSIA; attempts to finish active work first unless --force is set",
    "service": "Manage CORSIA's service; source installs use the service for their checkout",
    "setup": "Choose local or server exposure",
}


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


def shell_help(text):
    # Read literal help only: running the shell sources checkout configuration.
    start = text.index('            echo "Usage: corsia <command>"')
    lines = re.findall(r'^\s*echo "  ([a-z][^"$]*)"', text[start:], re.M)
    descriptions = {}
    for line in lines:
        parts = re.split(r"\s{2,}", line.strip(), maxsplit=1)
        if len(parts) == 2:
            name = parts[0].split()[0]
            descriptions.setdefault(name, parts[1])
    return descriptions


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
    if hasattr(module, "build_parser"):
        return module.build_parser()
    # Some script CLIs construct their parser in main. Interrupt parsing before
    # it can return arguments or execute a handler.
    class Captured(Exception):
        pass
    original = argparse.ArgumentParser.parse_args
    def capture(self, *args, **kwargs):
        raise Captured(self)
    argparse.ArgumentParser.parse_args = capture
    try:
        module.main([])
        raise ValueError(f"Parser not captured: {module.__name__}")
    except Captured as captured:
        return captured.args[0]
    finally:
        argparse.ArgumentParser.parse_args = original


def parsers():
    sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "backend/scripts")]
    import cli
    parser = argparse.ArgumentParser(prog="corsia")
    sub = parser.add_subparsers(dest="cmd")
    cli._add_bootstrap_commands(sub)
    cli._add_runtime_commands(sub)
    result = dict(sub.choices)
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
    help_text = shell_help(text)
    module_guard()
    available = parsers()
    backend_roots = set(available) - set(SCRIPTS)
    if UNROUTED & commands:
        raise ValueError(f"Unrouted exemption is now dispatched: {sorted(UNROUTED & commands)}")
    if backend_roots - commands != UNROUTED:
        raise ValueError(f"Unclassified parser commands: {sorted(backend_roots - commands - UNROUTED)}; stale exemptions: {sorted(UNROUTED - backend_roots)}")
    if app_direct - backend_roots:
        raise ValueError(f"Desktop commands lack parsers: {sorted(app_direct - backend_roots)}")
    unknown = commands - set(help_text) - set(EXTRA) - backend_roots
    if unknown:
        raise ValueError(f"Shell commands lack descriptions: {', '.join(sorted(unknown))}")
    lines = ["# CLI reference", "", "Generated reference for `corsia` commands.", "",
             f"Generate with `{regenerate_command('generate_cli')}`; add `--check` to compare the generated page. The backend's locked dependencies are required.", ""]
    for name in sorted(commands - INTERNAL):
        description = SHARED_DESCRIPTIONS.get(name, help_text.get(name, EXTRA.get(name, "")))
        lines.extend([f"## `{name}`", "", cell(description), ""])
        if name not in source_commands:
            lines.extend(["Desktop app only.", ""])
        if name in available:
            describe_parser(lines, available[name], f"corsia {name}")
        elif name == "service":
            lines.extend(["`corsia service [install|uninstall|start|stop|restart|status]`", "",
                          "`install` and `uninstall` are available in source installs; the desktop app refuses them.", ""])
        elif name == "logs":
            lines.extend(["`corsia logs [backend]`", ""])
        elif name == "start":
            lines.extend(["`corsia start [--open] [--force]`", ""])
        elif name == "stop":
            lines.extend(["`corsia stop [--stop-local-model] [--force]`", ""])
        elif name in {"open", "restart"}:
            lines.extend([f"`corsia {name} [--force]`", ""])
        if name == "setup":
            lines.extend(["In source installs, `corsia setup local|server` updates the choice and restarts the service. "
                          "In the desktop app, it updates the choice without restarting.", ""])
    lines.extend(["## Internal commands", "", "These commands are for internal operation.", ""])
    for name in sorted(INTERNAL & commands):
        lines.append(f"- `{name}`: {cell(help_text.get(name, EXTRA.get(name, '')))}")
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
