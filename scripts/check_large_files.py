#!/usr/bin/env python3
"""Fail when a file larger than 500 KB is committed, to keep blobs out of history.

Modes (exactly one):
  FILE ...        check the given paths (pre-commit passes the staged ones)
  --base REF      files added or modified between REF and HEAD (CI)
  --all           every tracked file in the tree

A path matching ALLOWLIST is exempt. Each entry carries a one-line reason;
add an entry only for a file that must be large (a shipped binary asset),
never for a data dump or generated output.
"""

from __future__ import annotations

import argparse
import fnmatch
import subprocess
import sys
from pathlib import Path

MAX_BYTES = 512000

ALLOWLIST: dict[str, str] = {
    "frontend/static/icons/*.png": "shipped app icon artwork",
    "desktop/src-tauri/icons/*": "desktop bundle icons required by the build",
    "desktop/src-tauri/icons/*/*": "desktop bundle icons required by the build",
    "docs/*.png": "documentation screenshots",
    "docs/live-review.gif": "README live co-editing demo",
}


def _git(*args: str) -> list[str]:
    out = subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True
    ).stdout
    return [line for line in out.splitlines() if line]


def is_allowlisted(path: str, allowlist: dict[str, str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in allowlist)


def find_oversized(
    paths: list[str], allowlist: dict[str, str], limit: int = MAX_BYTES
) -> list[tuple[str, int]]:
    found = []
    for path in paths:
        p = Path(path)
        if not p.is_file() or is_allowlisted(path, allowlist):
            continue
        size = p.stat().st_size
        if size > limit:
            found.append((path, size))
    return found


def main(argv: list[str] | None = None, allowlist: dict[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="*")
    parser.add_argument("--base", help="check files changed between this ref and HEAD")
    parser.add_argument("--all", action="store_true", help="check every tracked file")
    args = parser.parse_args(argv)
    if sum([bool(args.files), bool(args.base), args.all]) != 1:
        parser.error("give exactly one of FILE..., --base REF, or --all")

    if args.all:
        paths = _git("ls-files")
    elif args.base:
        paths = _git("diff", "--name-only", "--diff-filter=AM", f"{args.base}...HEAD")
    else:
        paths = args.files

    oversized = find_oversized(paths, ALLOWLIST if allowlist is None else allowlist)
    for path, size in oversized:
        print(f"{path}: {size} bytes exceeds the {MAX_BYTES}-byte limit", file=sys.stderr)
    if oversized:
        print(
            "Keep large blobs and data dumps out of git; if a binary asset must "
            "ship, add it to ALLOWLIST in scripts/check_large_files.py with a reason.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
