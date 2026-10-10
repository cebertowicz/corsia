#!/usr/bin/env python3
"""Dry-run the current direct ``working``/``threads`` to ``.threads`` migration.

This dormant operational helper is intentionally dry-run-only.  Task 7 owns any
future authorized cutover; this script only reports the exact DB-known current
layout work that would be needed for the selected workspace.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

from core.thread_dir_migration import (  # noqa: E402
    MigrationResult,
    migrate_scope,
    repoint_session_bootloader_paths,
    session_working_relpaths,
)
from core.workspace_layout import resolve_project_root, resolve_workspace_root  # noqa: E402
from infra.config import WORKSPACE  # noqa: E402
from infra.database import get_db  # noqa: E402


def _scope_groups(
    db,
    workspace: Path,
) -> list[tuple[Path, set[str], dict[str, set[str]]]]:
    """Group DB-known session IDs and durable paths by their exact scope root."""
    selected_workspace = resolve_workspace_root(workspace)
    groups: dict[Path, tuple[set[str], dict[str, set[str]]]] = {}
    for row in db.execute(
        "SELECT id, project_id, bootloader_files FROM threads"
    ).fetchall():
        sid = str(row["id"] if hasattr(row, "keys") else row[0])
        project_id = row["project_id"] if hasattr(row, "keys") else row[1]
        scope = (
            resolve_project_root(selected_workspace, str(project_id), db=db)
            if project_id
            else selected_workspace
        )
        if scope is None:
            continue
        thread_ids, durable_by_thread = groups.setdefault(scope, (set(), {}))
        thread_ids.add(sid)
        raw = row["bootloader_files"] if hasattr(row, "keys") else row[2]
        try:
            entries = json.loads(raw) if raw else []
        except (TypeError, json.JSONDecodeError):
            entries = []
        if isinstance(entries, list):
            durable = session_working_relpaths([str(entry) for entry in entries], sid)
            if durable:
                durable_by_thread[sid] = durable
    return [
        (scope, thread_ids, durable_by_thread)
        for scope, (thread_ids, durable_by_thread) in groups.items()
    ]


def main() -> int:
    """Print the direct migration plan without mutating files or database rows."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="disabled; Task 7 owns any future authorized cutover",
    )
    args = parser.parse_args()
    if args.apply:
        parser.error("--apply is disabled; this migration helper is dry-run-only")

    db = get_db()
    try:
        total = MigrationResult()
        for root, thread_ids, durable_by_thread in _scope_groups(db, WORKSPACE):
            result = migrate_scope(
                root,
                thread_ids=thread_ids,
                durable_by_thread=durable_by_thread,
                dry_run=True,
            )
            if result.threads_migrated:
                print(
                    f"[{root}] threads={len(result.threads_migrated)} "
                    f"skills={result.moved_skills} context={result.moved_context} "
                    f"working={result.moved_working}"
                )
            total.merge(result)
        repointed = repoint_session_bootloader_paths(db, dry_run=True)
    finally:
        db.close()

    print(
        f"\nWOULD MOVE: {len(total.threads_migrated)} thread dirs "
        f"({total.moved_skills} skills, {total.moved_context} context, "
        f"{total.moved_working} working); bootloader-path rows repointed: {repointed}"
    )
    print("Dry-run only. --apply is disabled until the authorized cutover task.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
