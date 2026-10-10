"""Filesystem browse API for server-side path pickers."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException

from infra.auth import authenticate
from infra.config import WORKSPACE

router = APIRouter(prefix="/api/fs", tags=["fs"])

MAX_ENTRIES = 500


@router.get("/browse")
async def browse(token: str, path: str | None = None, files: int = 0, ext: str = ""):
    """List one directory level for the path pickers."""
    if not authenticate(token):
        raise HTTPException(401)

    home = Path.home().resolve()
    bases = {home, WORKSPACE.resolve()}
    target = Path(path).expanduser() if path else home
    try:
        resolved = target.resolve(strict=True)
    except OSError as e:
        raise HTTPException(400, f"Bad path: {e}") from e
    if not resolved.is_dir():
        raise HTTPException(400, f"Not a directory: {resolved}")
    if not any(resolved == b or b in resolved.parents for b in bases):
        raise HTTPException(400, "Path must be under your home directory or the workspace")

    exts = tuple(s.strip() for s in ext.split(",") if s.strip())
    dirs: list[dict] = []
    file_rows: list[dict] = []
    try:
        entries = sorted(resolved.iterdir(), key=lambda p: p.name.lower())
    except OSError as e:
        raise HTTPException(400, f"Can't read directory: {e}") from e

    for entry in entries:
        if entry.name.startswith("."):
            continue
        if len(dirs) + len(file_rows) >= MAX_ENTRIES:
            break
        try:
            if entry.is_dir():
                dirs.append({
                    "name": entry.name,
                    "is_git": (entry / ".git").exists(),
                    "is_link": entry.is_symlink(),
                })
            elif files and entry.is_file():
                if not exts or entry.name.endswith(exts):
                    file_rows.append({"name": entry.name})
        except OSError:
            continue  # unreadable entry — skip, don't kill the listing

    return {
        "path": str(resolved),
        "parent": str(resolved.parent) if resolved not in bases else None,
        "dirs": dirs,
        **({"files": file_rows} if files else {}),
    }
