#!/usr/bin/env python3
"""Generate the agent-tools reference from the catalog and offline registry."""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys
import tempfile

from generate_data_model import initialize_stores

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from tools.catalog import TOOL_CATALOG, TOOL_PACKS, is_reachable, pack_members  # noqa: E402

OUTPUT = ROOT / "docs/reference/agent-tools.md"


def registry_descriptions():
    """Build the product registry against disposable stores."""
    with tempfile.TemporaryDirectory(prefix="corsia-agent-tools-") as temp:
        stores = initialize_stores(Path(temp))
        try:
            from infra.database import get_db, initialize_schema
            from subagents.harness_tool_bridge import build_tool_registry

            initialize_schema(get_db())
            registry = build_tool_registry(working_dir=str(Path(temp) / "workspace"))
            return {item["function"]["name"]: item["function"]["description"]
                    for item in registry.get_definitions()}
        finally:
            from infra import analytics_db
            from infra.database import close_thread_db

            close_thread_db()
            analytics_db.close()
            for name, connection in stores.items():
                if name != "analytics":
                    connection.close()


def cell(text):
    return text.replace("|", "\\|").replace("\n", " ")


def render(descriptions):
    lines = [
        "# Agent tools", "",
        "This reference lists tools an agent can reach. Core tools are sent on every turn; "
        "other tools are loaded by pack through `tools_load` and called with `tool_call`. "
        "Connected apps' tools depend on the installation and are not listed here.", "",
        "Generate with `.venv/bin/python scripts/docs/generate_agent_tools.py`; "
        "add `--check` to compare the generated page.", "",
        "## Core tools", "", "| Tool | Description |", "|---|---|",
    ]
    for name, entry in sorted(TOOL_CATALOG.items()):
        if not entry.core or not is_reachable(name):
            continue
        if name not in descriptions or not descriptions[name].strip():
            raise ValueError(f"No registry description for reachable core tool: {name}")
        description = re.split(r"(?<=[.!?])\s+", descriptions[name].strip(), maxsplit=1)[0]
        lines.append(f"| `{name}` | {cell(description)} |")
    for pack, names in pack_members().items():
        lines.extend(["", f"## {TOOL_PACKS[pack]}", "", f"Pack: `{pack}`", "",
                      "| Tool | Description |", "|---|---|"])
        for name in names:
            entry = TOOL_CATALOG[name]
            if not entry.summary.strip():
                raise ValueError(f"No catalog summary for pack tool: {name}")
            description = cell(entry.summary)
            if entry.account_bound:
                description += " (Requires a connected account.)"
            lines.append(f"| `{name}` | {description} |")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check the generated page without writing")
    args = parser.parse_args()
    content = render(registry_descriptions())
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text() != content:
            print(f"Out of date: {OUTPUT.relative_to(ROOT)}", file=sys.stderr)
            return 1
    else:
        OUTPUT.write_text(content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
