"""Startup repair of stored account homes that still name the pre-move state root.

The packaged app's data once lived under ``com.cebertowicz.corsia/state`` and moved to
``CORSIA/state``. An account row written before the move holds an absolute home under the
old root, so every launch of it is refused and a discovery run creates the old folder
again. This runs on every start, so it repairs the first start after a move and installs
that already moved. A home is rewritten only when the same path exists under the current
root; any other value stays as stored and is logged. Once nothing names the old root, it
is one SELECT over the accounts table, which holds a few rows however large the database is.

It touches the two home columns only. A Codex account's sign-in link and its record in
``discovery_json`` belong to ``connections/account_projection.py`` and ``account_evidence.py``;
this module never writes them.

Remove it once every supported install has started a release that contains it.
"""

from __future__ import annotations

import os
from pathlib import Path

from loguru import logger

from infra.database import write_transaction
from infra.platform_paths import legacy_state_root

# Every stored column that can hold a state-root path. Keep the table small: a column in
# messages or events would need an indexed predicate or a once-only marker instead of this scan.
_TABLE = "vendor_accounts"
_COLUMNS = ("source_home", "runtime_home")


def heal_moved_state_paths(
    *, legacy_root: Path | None = None, state_root: Path | None = None, db=None,
) -> int:
    """Repoint stored legacy-root homes to the current root; return how many values moved."""
    if state_root is None:
        from infra.config import STATE_ROOT

        state_root = STATE_ROOT
    current = Path(state_root).resolve(strict=False)
    legacy = Path(legacy_root) if legacy_root is not None else legacy_state_root()
    prefixes = tuple(dict.fromkeys((str(legacy), str(legacy.resolve(strict=False)))))
    if str(current) in prefixes:
        return 0
    moved = 0
    vacated: list[str] = []
    with write_transaction(db, scope="heal_moved_state_paths") as connection:
        marked = " OR ".join(f"instr({c}, ?) > 0" for c in _COLUMNS for _ in prefixes)
        rows = connection.execute(
            f"SELECT id, {', '.join(_COLUMNS)} FROM {_TABLE} WHERE {marked}",
            tuple(prefix for _ in _COLUMNS for prefix in prefixes),
        ).fetchall()
        for row in rows:
            changes: dict[str, str] = {}
            for column in _COLUMNS:
                value = row[column]
                target = _under_current(value, prefixes, str(current)) if value is not None else None
                if target is None:
                    continue
                if not os.path.lexists(target):
                    logger.warning(
                        "stored {}.{} still names the old state root and has no copy to point to: {}",
                        _TABLE, column, value,
                    )
                    continue
                changes[column] = target
            if not changes:
                continue
            sets = ", ".join(f"{column}=?" for column in changes)
            connection.execute(f"UPDATE {_TABLE} SET {sets} WHERE id=?", (*changes.values(), row["id"]))
            moved += len(changes)
            vacated.extend(row[column] for column in changes)
            logger.info("moved stored homes of {} {} to the current state root", _TABLE, row["id"])
    for old_home in vacated:
        _remove_empty_dirs(old_home, prefixes)
    return moved


def _under_current(value: str, prefixes: tuple[str, ...], current: str) -> str | None:
    for prefix in prefixes:
        if value == prefix or value.startswith(prefix + os.sep):
            return current + value[len(prefix):]
    return None


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
