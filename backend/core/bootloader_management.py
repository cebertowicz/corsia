"""Bootloader file management helpers for workspace, project, and thread CTX."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from infra.time import utc_now_iso

BOOTLOADER_EXTS = (".md", ".txt")

BootloaderScope = Literal["workspace", "project", "thread"]


@dataclass(frozen=True)
class BootloaderTarget:
    """Resolved target for one bootloader-management operation."""

    scope: BootloaderScope
    target_id: str
    root: Path
    allowed_roots: tuple[Path, ...]
    store_absolute: bool


def _key(row: Any, key: str) -> Any:
    if hasattr(row, "keys") and key in row.keys():
        return row[key]
    if isinstance(row, dict):
        return row.get(key)
    return None


def _is_relative_to(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _json_list(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, list):
        return None
    return [str(item) for item in parsed]


def normalize_bootloader_entries(
    entries: list[Any],
    target: BootloaderTarget,
    *,
    require_exists: bool = True,
    existing_entries: set[str] | None = None,
) -> list[str]:
    """Validate and normalize bootloader entries for the target layer."""
    if not isinstance(entries, list):
        raise ValueError("bootloader_files must be a list")

    root = target.root.expanduser().resolve()
    allowed_roots = tuple(p.expanduser().resolve() for p in target.allowed_roots)
    normalized: list[str] = []
    seen: set[str] = set()
    existing = existing_entries or set()

    for entry in entries:
        if not isinstance(entry, str) or not entry.strip():
            raise ValueError("Entries must be non-empty paths")
        raw = entry.strip()
        p = Path(raw)
        if p.is_absolute():
            resolved = p.expanduser().resolve(strict=False)
        else:
            if ".." in p.parts:
                raise ValueError(f"'{raw}' must stay inside the {target.scope} context root")
            resolved = (root / p).resolve(strict=False)

        if not any(_is_relative_to(resolved, allowed) for allowed in allowed_roots):
            raise ValueError(f"'{raw}' is outside the {target.scope} context roots")
        if resolved.suffix not in BOOTLOADER_EXTS:
            raise ValueError(
                f"'{raw}' — only {' / '.join(BOOTLOADER_EXTS)} files can be bootloaded"
            )
        stored = str(resolved) if target.store_absolute else str(resolved.relative_to(root))
        if require_exists and not resolved.is_file() and raw not in existing and stored not in existing:
            raise ValueError(f"'{raw}' not found")
        if stored in seen:
            raise ValueError(f"'{stored}' appears twice")
        seen.add(stored)
        normalized.append(stored)

    return normalized


def create_bootloader_file(
    target: BootloaderTarget,
    relative_path: str,
    *,
    content: str = "",
) -> str:
    """Create a new bootloader-eligible file and return its stored path."""
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise ValueError("path is required")
    rel = Path(relative_path.strip())
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError("path must be relative to the context root")
    path = (target.root / rel).resolve(strict=False)
    if not any(_is_relative_to(path, root.resolve()) for root in target.allowed_roots):
        raise ValueError("path is outside the context root")
    if path.suffix not in BOOTLOADER_EXTS:
        raise ValueError(
            f"Only {' / '.join(BOOTLOADER_EXTS)} files can be bootloaded"
        )
    if path.exists():
        raise ValueError(f"{relative_path} already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return str(path) if target.store_absolute else str(path.relative_to(target.root))


def log_bootloader_event(
    db,
    *,
    actor_user_id: str | None,
    actor_thread_id: str | None = None,
    scope: BootloaderScope,
    target_id: str,
    action: str,
    files: list[Any],
) -> None:
    """Append an audit event for an approved bootloader change."""
    db.execute(
        """
        INSERT INTO context_bootloader_events
            (id, actor_user_id, actor_thread_id, scope, target_id, action,
             files_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid.uuid4()),
            actor_user_id,
            actor_thread_id,
            scope,
            target_id,
            action,
            json.dumps(files),
            utc_now_iso(),
        ),
    )


def thread_bootloader_files(thread_id: str, *, db=None) -> list[str]:
    """Return thread-level bootloader files for a thread."""
    conn = db
    should_close = False
    if conn is None:
        from infra.database import get_db

        conn = get_db()
        should_close = True
    try:
        row = conn.execute(
            "SELECT bootloader_files FROM threads WHERE id = ?",
            (thread_id,),
        ).fetchone()
        return _json_list(_key(row, "bootloader_files") if row else None) or []
    except sqlite3.OperationalError:
        return []
    finally:
        if should_close and conn is not None:
            conn.close()
