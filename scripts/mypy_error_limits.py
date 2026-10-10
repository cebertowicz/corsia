#!/usr/bin/env python3
"""Fail when a target reports more mypy type errors than its committed limit.

The repository carries a backlog of pre-existing type errors. Requiring it to
reach zero before enforcement would leave the check advisory indefinitely, so
this freezes the current counts instead: existing errors stay tolerated, new
ones fail the build. Limits are lowered as the backlog is worked down.

Usage:
    mypy_error_limits.py backend=mypy-backend.txt tools=mypy-tools.txt
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

_LIMITS = (
    pathlib.Path(__file__).resolve().parent.parent
    / "quality"
    / "mypy-error-limits.json"
)
_SUMMARY = re.compile(r"^Found (\d+) errors? in ", re.MULTILINE)


def count_errors(report: str) -> int:
    """Return the error count mypy reported, preferring its own summary line."""
    match = _SUMMARY.search(report)
    if match is not None:
        return int(match.group(1))
    if "Success: no issues found" in report:
        return 0
    # No summary and no success line means mypy did not run to completion;
    # counting matched lines would silently under-report.
    raise SystemExit(
        "mypy produced no summary line; the run did not complete:\n"
        f"{report[-2000:]}"
    )


def main(argv: list[str]) -> int:
    """Compare each target's reported error count against its committed limit."""
    if not argv:
        raise SystemExit(f"usage: {sys.argv[0]} <target>=<report path> ...")

    limits = json.loads(_LIMITS.read_text())
    failures: list[str] = []
    drops: list[str] = []

    for arg in argv:
        target, _, path = arg.partition("=")
        if not path:
            raise SystemExit(f"expected <target>=<report path>, got {arg!r}")
        if target not in limits:
            raise SystemExit(f"{target!r} has no limit in {_LIMITS}")

        limit = int(limits[target])
        actual = count_errors(pathlib.Path(path).read_text(encoding="utf-8"))

        if actual > limit:
            failures.append(
                f"{target}: {actual} type errors, {actual - limit} more than the "
                f"limit of {limit}. Fix the new errors, or raise the limit in "
                f"{_LIMITS.name} with a stated reason."
            )
        elif actual < limit:
            drops.append(f'  "{target}": {actual},   (was {limit})')
        else:
            print(f"{target}: {actual} type errors, at the limit of {limit}")

    if drops:
        print(
            f"Type errors decreased. Lower the limit in {_LIMITS.name} to keep "
            "them from coming back:"
        )
        print("\n".join(drops))

    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
