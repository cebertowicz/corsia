#!/usr/bin/env python3
"""Generate the data-model reference from disposable initialized stores.

Run with the backend's locked Python environment. No server or user data is used.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
from collections import defaultdict

from data_model_layout import connected_diagrams, module_group, regroup

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "docs/reference/data-model"
CREATE_TABLE = re.compile(
    r"CREATE\s+(?:VIRTUAL\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_]\w*)",
    re.IGNORECASE,
)
SHADOW = re.compile(r"^(.*_fts)_(?:data|idx|docsize|config|content)$")
SUMMARIES = {
    "Threads and messages": "These tables store threads, turns, messages, presence and related updates.",
    "Plans": "These tables store plans, their nodes, events, dependencies and execution state.",
    "Events and webhooks": "These tables store events, rules, watches, bulletins and webhook deliveries.",
    "Documents and artifacts": "These tables store artifact registrations, identities, comments and document feedback.",
    "Accounts and connections": "These tables store accounts, credentials, connections and connected apps.",
    "Usage and measurement": "These tables store model usage, pricing and measurement data.",
    "Context and memory": "These tables store checkpoints, continuity, context changes and extracted facts.",
    "Settings and policy": "These tables store projects, settings and policies.",
    "Host access": "These tables store host access leases and action reviews.",
    "Providers and runtime": "These tables store provider, model, server and runtime process data.",
    "Store internals": "These tables support SQLite sequences and full-text search indexes.",
    "Document store": "These tables store a document's live updates, mirror and publication state.",
    "Log store": "These tables store CORSIA log records and their schema metadata.",
    "Analytics store": "These tables store product events and request timing data.",
}


def initialize_stores(root: Path):
    """Invoke the four store initializers with disposable paths."""
    workspace = root / "workspace"
    state = root / "state"
    workspace.mkdir()
    state.mkdir()
    os.environ.update(CORSIA_WORKSPACE=str(workspace), CORSIA_STATE_DIR=str(state),
                      CORSIA_TEST_MODE="1", CORSIA_ANALYTICS_DISABLE="1",
                      CORSIA_ALLOW_HOME_WORKSPACE_IN_CI="1")
    sys.path.insert(0, str(ROOT / "backend"))
    from infra.database import initialize_schema
    from doceditor.live_store import LiveStore
    from infra.otel_logs.store import LogStore
    from infra import analytics_db
    from api.measurement_prepared import create_tables

    primary = sqlite3.connect(state / "primary.sqlite")
    initialize_schema(primary)
    create_tables(primary, roots=True)
    LiveStore(state / "document.sqlite")
    logs = LogStore(state / "logs.sqlite")
    if not logs.open():
        raise RuntimeError("Could not initialize the disposable log store")
    logs.close()
    analytics = analytics_db.get_db()
    return {
        "primary": primary,
        "document": sqlite3.connect(state / "document.sqlite"),
        "logs": sqlite3.connect(state / "logs.sqlite"),
        "analytics": analytics,
    }


def source_map():
    """Find current table declarations, rejecting ambiguous source ownership."""
    paths = list((ROOT / "backend/infra").rglob("*_schema.py"))
    paths += [ROOT / "backend/infra" / name for name in
              ("schema.py", "continuity_staging.py", "db_fts.py")]
    result = {}
    for path in sorted(paths):
        module = path.relative_to(ROOT).as_posix()
        definitions = CREATE_TABLE.findall(path.read_text())
        if module != "backend/infra/schema.py":
            module_group(module)
        for table in definitions:
            if table in result and result[table] != module:
                raise ValueError(f"Ambiguous table source: {table}: {result[table]}, {module}")
            result[table] = module
    return result


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def sqlite_tables(connection, temporary=False):
    tables = {}
    catalog = "sqlite_temp_master" if temporary else "sqlite_master"
    schema = "temp." if temporary else ""
    names = connection.execute(f"SELECT name FROM {catalog} WHERE type='table' ORDER BY name")
    for (name,) in names:
        columns = connection.execute(f"PRAGMA {schema}table_xinfo({quote(name)})").fetchall()
        foreign = connection.execute(f"PRAGMA {schema}foreign_key_list({quote(name)})").fetchall()
        indexes = connection.execute(f"PRAGMA {schema}index_list({quote(name)})").fetchall()
        tables[name] = {
            "columns": [(r[1], r[2] or "", bool(r[3]), bool(r[5]), r[4]) for r in columns],
            "foreign": [(r[3], r[2], r[4]) for r in foreign],
            "indexes": sorted((r[1], bool(r[2]), ", ".join(
                str(c[2]) if c[2] is not None else "expression"
                for c in connection.execute(f"PRAGMA {schema}index_info({quote(r[1])})").fetchall()
            )) for r in indexes),
        }
    return tables


def duck_tables(connection):
    names = connection.execute(
        "SELECT table_name FROM duckdb_tables() WHERE NOT internal ORDER BY table_name"
    ).fetchall()
    result = {}
    for (name,) in names:
        columns = connection.execute(f"PRAGMA table_info({quote(name)})").fetchall()
        foreign = connection.execute(
            "SELECT constraint_column_names, referenced_table, referenced_column_names "
            "FROM duckdb_constraints() WHERE table_name=? AND constraint_type='FOREIGN KEY'",
            [name],
        ).fetchall()
        indexes = connection.execute(
            "SELECT index_name, is_unique, expressions FROM duckdb_indexes() WHERE table_name=? ORDER BY index_name",
            [name],
        ).fetchall()
        result[name] = {
            "columns": [(r[1], r[2] or "", bool(r[3]), bool(r[5]), r[4]) for r in columns],
            "foreign": [(col, target, ref) for cols, target, refs in foreign
                        for col, ref in zip(cols, refs)],
            "indexes": indexes,
        }
    return result


def collect(stores):
    source = source_map()
    modules = {"document": "backend/doceditor/live_store.py",
               "logs": "backend/infra/otel_logs/store.py",
               "analytics": "backend/infra/analytics_db.py"}
    groups = defaultdict(dict)
    for store, connection in stores.items():
        tables = duck_tables(connection) if store == "analytics" else sqlite_tables(connection)
        for table, info in tables.items():
            if store == "primary":
                shadow = SHADOW.match(table)
                module = "SQLite internal" if table == "sqlite_sequence" else source.get(
                    shadow.group(1) if shadow else table)
                if module is None:
                    raise ValueError(f"No schema module found for {table}")
            else:
                module = modules[store]
            groups[(store, module)][table] = info
        if store == "primary":
            groups[(store, "backend/api/measurement_prepared.py")].update(
                sqlite_tables(connection, temporary=True))
    return dict(groups)


def cell(value):
    if value is None:
        return "—"
    return str(value).replace("|", "\\|").replace("\n", " ")


def diagram(tables):
    lines = ["```mermaid", "erDiagram"]
    for name, info in tables.items():
        for column, target, _ in info["foreign"]:
            if target in tables:
                required = next((required or primary for col, _, required, primary, _
                                 in info["columns"] if col == column), False)
                parent = "||" if required else "o|"
                unique = any(is_unique and cols == column for _, is_unique, cols in info["indexes"])
                child = "o|" if unique else "o{"
                lines.append(f'    {target} {parent}--{child} {name} : {column}')
    return "\n".join(lines + ["```"])


def table_section(name, info):
    foreign = defaultdict(list)
    for column, target, reference in info["foreign"]:
        foreign[column].append(f"{target}.{reference}")
    lines = [f"### `{name}`", "", f"Source: `{info['module']}`.", "",
             "| Column | Type | Primary key | NOT NULL | Default | References |",
             "|---|---|---|---|---|---|"]
    for column, kind, required, primary, default in info["columns"]:
        lines.append(f"| `{cell(column)}` | {cell(kind)} | {'Yes' if primary else 'No'} | "
                     f"{'Yes' if required else 'No'} | {cell(default)} | {cell(', '.join(foreign[column]) or None)} |")
    indexes = ", ".join(f"`{name}` ({cell(columns)}" + (", unique)" if unique else ")")
                        for name, unique, columns in info["indexes"]) or "None"
    return "\n".join(lines + ["", f"Indexes: {indexes}.", ""])


def page(group, tables):
    lines = [f"# {group}", "", SUMMARIES[group], ""]
    diagrams = connected_diagrams(tables)
    if diagrams:
        lines.extend(["## Relationships", ""])
        for batch in diagrams:
            lines.extend([diagram(batch), ""])
    lines.extend(["## Tables", ""])
    for name in sorted(tables):
        lines.append(table_section(name, tables[name]))
    return "\n".join(lines).rstrip() + "\n"


def filename(group):
    return group.lower().replace(" ", "-") + ".md"


def render(groups):
    files = {}
    lines = ["# Data model", "", "Generated reference for the primary, document, log, and analytics stores.",
             "", "Generate with `PYTHONPATH=backend .venv/bin/python scripts/docs/generate_data_model.py`; "
             "add `--check` to compare the generated pages. The backend's locked dependencies are required.",
             "", "SQLite full-text search creates shadow tables; SQLite creates `sqlite_sequence`. "
             "Relationships shown in a diagram connect tables in that diagram; column tables list all declared foreign keys. "
             "The measurement tables are temporary and are created on a primary connection when needed.",
             "", "Historical migration tables are excluded: `checkpoints_new`, `connected_accounts_new`, "
             "`connected_account_audit_new`, `_usage_backfill_bound`, `_usage_backfill_unbound`, "
             "and `retired_worker_releases` are migration intermediates; `project_vendor_accounts` was retired.", ""]
    subsystems, modules = regroup(groups)
    lines.extend(["| Subsystem | Page | Source modules |", "|---|---|---|"])
    for group in sorted(subsystems):
        path = filename(group)
        files[path] = page(group, subsystems[group])
        sources = ", ".join(f"`{module}`" for module in sorted(modules[group]))
        lines.append(f"| {group} | [{group}]({path}) | {sources} |")
    files["README.md"] = "\n".join(lines) + "\n"
    return files


def write_or_check(files, check):
    existing = {path.name for path in OUTPUT.glob("*.md")}
    stale = existing - set(files)
    changed = [name for name, content in files.items()
               if not (OUTPUT / name).exists() or (OUTPUT / name).read_text() != content]
    if check:
        for name in sorted(changed + list(stale)):
            print(f"Out of date: {name}", file=sys.stderr)
        return bool(changed or stale)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (OUTPUT / name).write_text(content)
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check generated pages without writing")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="corsia-data-model-") as temp:
        stores = initialize_stores(Path(temp))
        try:
            files = render(collect(stores))
        finally:
            from infra import analytics_db
            analytics_db.close()
            for name, connection in stores.items():
                if name != "analytics":
                    connection.close()
    return int(write_or_check(files, args.check))


if __name__ == "__main__":
    raise SystemExit(main())
