"""Startup repair of stored paths that still name the pre-move state root.

The packaged app's data once lived under ``com.cebertowicz.corsia/state`` and moved to
``CORSIA/state``. Rows written before the move hold absolute paths under the old root, so
an account whose home is stored there is refused on every launch, and a discovery run
creates the old folder again. This runs on every start, so it repairs the first start
after a move and installs that already moved. A value is rewritten only when the same
path exists under the current root; any other value stays as stored and is logged. Once
nothing names the old root, it is one SELECT over the accounts table, which holds a few
rows however large the database is.

Remove it once every supported install has started a release that contains it.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

from loguru import logger

from infra.database import write_transaction
from infra.platform_paths import legacy_state_root

# Every stored column that can hold a state-root path, as (table, plain path columns, JSON
# columns). Keep each table small: a column in messages or events would need an indexed
# predicate or a once-only marker instead of this scan.
_TABLES = (
    ("vendor_accounts", ("source_home", "runtime_home"), ("auth_cleanup_json", "discovery_json")),
)


def heal_moved_state_paths(
    *, legacy_root: Path | None = None, state_root: Path | None = None, db=None,
) -> int:
    """Repoint stored legacy-root paths to the current root; return how many values moved."""
    if state_root is None:
        from infra.config import STATE_ROOT

        state_root = STATE_ROOT
    current = Path(state_root).resolve(strict=False)
    legacy = Path(legacy_root) if legacy_root is not None else legacy_state_root()
    prefixes = tuple(dict.fromkeys((str(legacy), str(legacy.resolve(strict=False)))))
    if str(current) in prefixes:
        return 0
    rewriter = _Rewriter(prefixes, str(current))
    with write_transaction(db, scope="heal_moved_state_paths") as connection:
        for table, columns, json_columns in _TABLES:
            marked = " OR ".join(f"instr({c}, ?) > 0" for c in (*columns, *json_columns) for _ in prefixes)
            rows = connection.execute(
                f"SELECT id, {', '.join((*columns, *json_columns))} FROM {table} WHERE {marked}",
                tuple(prefix for _ in (*columns, *json_columns) for prefix in prefixes),
            ).fetchall()
            for row in rows:
                counted = rewriter.moved
                try:
                    changes = rewriter.row_changes(table, row, columns, json_columns)
                except OSError as error:
                    # The link still names the old home: keep the row as stored so the next start retries it whole.
                    rewriter.moved = counted
                    logger.error("left {} {} as stored: could not repoint its auth link: {}", table, row["id"], error)
                    continue
                if changes:
                    rewriter.vacated.extend(row[c] for c in columns if c in changes)
                if changes:
                    sets = ", ".join(f"{column}=?" for column in changes)
                    connection.execute(f"UPDATE {table} SET {sets} WHERE id=?", (*changes.values(), row["id"]))
    for old_home in rewriter.vacated:
        _remove_empty_dirs(old_home, prefixes)
    return rewriter.moved


def _remove_empty_dirs(home: str, prefixes: tuple[str, ...]) -> None:
    """rmdir an old home and each emptied parent under the legacy root; never a file, never the root."""
    for prefix in prefixes:
        if home.startswith(prefix + os.sep):
            root = Path(prefix)
            break
    else:
        return
    folder = Path(home)
    while folder != root and root in folder.parents:
        try:
            folder.rmdir()
        except FileNotFoundError:
            pass
        except OSError:  # not empty, not a folder, or not ours to remove: stop here
            return
        else:
            logger.info("removed empty folder {} left under the old state root", folder)
        folder = folder.parent


class _Rewriter:
    def __init__(self, prefixes: tuple[str, ...], current: str) -> None:
        self.prefixes, self.current, self.moved = prefixes, current, 0
        self.vacated: list[str] = []

    def under_legacy(self, value: str) -> str | None:
        for prefix in self.prefixes:
            if value == prefix or value.startswith(prefix + os.sep):
                return self.current + value[len(prefix):]
        return None

    def rebase(self, value: str, *, what: str) -> str:
        """The same path under the current root when it is there; else the value, logged."""
        target = self.under_legacy(value)
        if target is None:
            return value
        if not os.path.lexists(target):
            logger.warning("stored path {} still names the old state root and has no copy to point to: {}", what, value)
            return value
        self.moved += 1
        return target

    def row_changes(self, table: str, row, columns, json_columns) -> dict[str, Any]:
        changes: dict[str, Any] = {}
        for column in columns:
            if row[column] is not None and (new := self.rebase(row[column], what=f"{table}.{column}")) != row[column]:
                changes[column] = new
        for column in json_columns:
            if row[column] is None:
                continue
            document = json.loads(row[column])
            self._relink(document)
            new_document = self._walk(document, f"{table}.{column}")
            if new_document != document:
                changes[column] = json.dumps(new_document, sort_keys=True)
        if changes:
            logger.info("moved stored paths in {} {} to the current state root", table, row["id"])
        return changes

    def _walk(self, node: Any, what: str) -> Any:
        if isinstance(node, str):
            return self.rebase(node, what=what)
        if isinstance(node, list):
            return [self._walk(item, what) for item in node]
        if isinstance(node, dict):
            return {self._walk(key, what): self._walk(value, what) for key, value in node.items()}
        return node

    def _relink(self, document: Any) -> None:
        """Point a managed ``auth.json`` link at the moved account home (never reads its target)."""
        if isinstance(document, list):
            for item in document:
                self._relink(item)
        elif isinstance(document, dict):
            source, destination = document.get("source"), document.get("destination")
            if isinstance(source, str) and isinstance(destination, str):
                self._relink_one(source, destination)
            for value in document.values():
                self._relink(value)

    def _relink_one(self, source: str, destination: str) -> None:
        new_source, new_destination = self.under_legacy(source), self.under_legacy(destination)
        if new_source is None or new_destination is None or not os.path.lexists(new_source):
            return
        if not os.path.islink(new_destination) or os.readlink(new_destination) != source:
            return
        stage = Path(new_destination).with_name(".corsia-auth-" + uuid.uuid4().hex)
        try:
            stage.symlink_to(new_source)
            os.replace(stage, new_destination)
        except OSError:
            stage.unlink(missing_ok=True)
            raise
