"""Repoint the few configuration values that store an absolute state path.

The desktop moves the whole state folder in one rename, so these values still
name the old place on the first start after the move. Each is re-derived from
the current ``STATE_ROOT``; history (messages, logs) is never rewritten. Every
step is idempotent and does nothing when the paths already agree.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
from typing import Any

from infra.database import get_db, write_transaction

log = logging.getLogger("infra.moved_state_heal")

_LIVE_HANDLE_STATES = ("running", "death_unverified")
_CONFIG_SUFFIXES = (".json", ".yaml", ".yml")
_CONFIG_MAX_BYTES = 1024 * 1024


def heal_moved_state_paths() -> None:
    """Rebase the managed oMLX row, its config files, and live process logs."""
    _heal_managed_omlx()
    _heal_process_log_paths()


def _rebase(value: Any, old: str, new: str) -> Any:
    if not isinstance(value, str):
        return value
    if value == old:
        return new
    if value.startswith(old + os.sep):
        return new + value[len(old):]
    return value


def _rebase_tree(value: Any, old: str, new: str) -> Any:
    if isinstance(value, dict):
        return {key: _rebase_tree(item, old, new) for key, item in value.items()}
    if isinstance(value, list):
        return [_rebase_tree(item, old, new) for item in value]
    return _rebase(value, old, new)


def _write_atomically(path: Path, text: str) -> None:
    staged = path.with_name(f".{path.name}.heal")
    staged.write_text(text, encoding="utf-8")
    os.replace(staged, path)


def _heal_config_file(path: Path, old: str, new: str) -> bool | None:
    """Rebase one file: True when rewritten, False when nothing to do, None when it failed."""
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > _CONFIG_MAX_BYTES:
            return False
        text = path.read_text(encoding="utf-8")
        if old not in text:
            return False
        if path.suffix == ".json":
            healed = json.dumps(_rebase_tree(json.loads(text), old, new), sort_keys=True)
        else:
            pattern = re.escape(old) + r"(?=[/\"'\s]|$)"
            healed = re.sub(pattern, lambda _match: new, text, flags=re.MULTILINE)
        if healed == text:
            return False
        _write_atomically(path, healed)
    except (OSError, ValueError) as error:
        log.warning("moved state: could not repoint managed oMLX file %s: %s", path.name, type(error).__name__)
        return None
    return True


def _heal_managed_omlx() -> None:
    from llm.managed_omlx_binary import is_corsia_managed_server, managed_omlx_paths
    from llm.network_discovery import get_servers, parse_server_metadata, set_server_metadata

    paths = managed_omlx_paths()
    new_base = str(paths.base_path)
    for row in get_servers(provider="omlx"):
        metadata = parse_server_metadata(row["id"], row.get("metadata_json")) or {}
        if not (is_corsia_managed_server(row) or metadata.get("corsia_managed") is True):
            continue
        old_base = metadata.get("base_path")
        if not isinstance(old_base, str) or not old_base or old_base == new_base:
            continue
        # Files first: once the row names the new base, the old prefix is gone,
        # so a file that failed keeps the row on the old base for the next start.
        failed = False
        if paths.base_path.is_dir():
            for entry in sorted(paths.base_path.iterdir()):
                if entry.suffix not in _CONFIG_SUFFIXES:
                    continue
                result = _heal_config_file(entry, old_base, new_base)
                if result is None:
                    failed = True
                elif result:
                    log.info("moved state: repointed managed oMLX file %s", entry.name)
        if failed:
            log.warning("moved state: managed oMLX server %s keeps its old folder until every file is repointed", row["id"])
            continue
        healed = {
            key: _rebase(metadata.get(key), old_base, new_base)
            for key in ("model_dir", "paged_ssd_cache_dir")
            if isinstance(metadata.get(key), str)
        }
        set_server_metadata(row["id"], base_path=new_base, **healed)
        log.info("moved state: repointed managed oMLX server %s to the current state folder", row["id"])


def _heal_process_log_paths() -> None:
    from infra.config import RUNTIME_DIR

    directory = Path(RUNTIME_DIR) / "managed-processes"
    rows = get_db().execute(
        "SELECT handle, log_path FROM managed_process_handles WHERE state IN (?, ?)",
        _LIVE_HANDLE_STATES,
    ).fetchall()
    moved = [
        (str(directory / Path(row["log_path"]).name), row["handle"])
        for row in rows
        if row["log_path"] and Path(row["log_path"]).parent != directory
    ]
    if not moved:
        return
    with write_transaction(None, scope="heal_process_log_paths") as connection:
        connection.executemany(
            "UPDATE managed_process_handles SET log_path = ? WHERE handle = ?", moved,
        )
    log.info("moved state: repointed %d running process log path(s)", len(moved))
