"""Resolve changed paths → owning PRD slug(s) (stage-2b).

PRD-owned mapping — the post-spec source of truth (the spec boundary registry
is retired as reviewer authority, #555). One path may be owned by multiple PRDs
(cross-cutting contracts), so resolution returns the union of matches. The map
is hand-maintained today; it is intended to be generated from PRD frontmatter
``boundary_paths`` later. Reuses the path-pattern matcher only (not spec data).
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from reviewer.prd_contract import PRD_DIR
from reviewer.reviewer_subsystems import path_matches

_MAP_PATH = f"{PRD_DIR}/PRD-PATH-MAP.md"
# Anchor parsing to this heading so a prose/example fence earlier in the doc is
# never mistaken for the machine-readable block (the live doc has exactly such
# an example fence above the real map).
_MAP_HEADING = "## PRD PATH MAP (machine-readable)"


def prds_for_changed_paths(
    changed_files: list[str], workspace_root: str | Path
) -> list[str]:
    """Return the distinct PRD slugs owning any of the changed paths."""
    rows = _load_map(workspace_root)
    if not rows:
        return []

    slugs: list[str] = []
    for path in changed_files:
        for pattern, row_slugs in rows:
            if path_matches(path, pattern):
                for slug in row_slugs:
                    if slug not in slugs:
                        slugs.append(slug)
    return slugs


_MapRow = tuple[str, list[str]]


def _load_map(workspace_root: str | Path) -> list[_MapRow]:
    """Parse the machine-readable block under ``_MAP_HEADING``: ``<pattern> <slug>...``.

    Anchored to the heading (not the first fence in the file) so an earlier
    prose/example fence is skipped — only the fence that follows the heading is
    read.
    """
    path = Path(workspace_root) / _MAP_PATH
    if not path.is_file():
        logger.info(f"PRD path map: not found at {path} — no PRD coverage")
        return []

    rows: list[_MapRow] = []
    seen_heading = False
    in_block = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if not seen_heading:
            if line.strip() == _MAP_HEADING:
                seen_heading = True
            continue
        if line.strip() == "```":
            if in_block:
                break
            in_block = True
            continue
        if not in_block:
            continue
        entry = _parse_row(line)
        if entry:
            rows.append(entry)
    return rows


def _parse_row(line: str) -> _MapRow | None:
    """Parse ``<path-pattern> <slug> [<slug>...]``; skip blanks/comments."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    parts = stripped.split()
    if len(parts) < 2:
        return None
    return (parts[0], parts[1:])
