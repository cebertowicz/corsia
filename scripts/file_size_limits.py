#!/usr/bin/env python3
"""Fail when tracked source grows past its committed size, or new source lands over.

The repository carries a backlog of oversized files and functions. Requiring it
to reach the standard before enforcement would leave the check advisory
indefinitely, so this freezes the current sizes instead: existing entries stay
tolerated at their recorded length, growth fails the build, and a NEW file or
function over the standard fails immediately because nothing entitles it to a
legacy exemption.

Modelled on ``mypy_error_limits.py`` deliberately -- same freeze-then-tighten
shape, same ``quality/`` home. It diverges on one point, and the divergence is
intentional: an entry that SHRINKS fails here, where a mypy drop only prints.
The whole purpose of this check is a decomposition that cannot regress, and a
split whose limit stays at the old size leaves exactly enough slack for the
file to grow back. Recording the new size is one command, and it keeps the
baseline exact rather than merely non-increasing.

A file that is expected to only grow (an append-only history such as a
migrations file) does not belong in ``limits`` at all -- no numeric ceiling
would ever be legitimate. It is listed in ``append_only`` instead, with a
one-line reason, and is measured for visibility but never fails and never
needs ``--adopt``. Functions inside an ``append_only`` file are exempt too:
the file has no ceiling, so neither do its parts.

Scope is every tracked source file the repository actually builds from, found
via ``git ls-files`` rather than a hand-maintained root list -- a new
production directory is governed the day it lands, without anyone remembering
to add it here.

Three modules, one concern each: ``size_limit_scan`` measures and owns the
standards, ``size_limit_baseline`` owns the committed baseline and the adopt
guards, and this file checks the tree and is the command CI runs.

Usage:
    file_size_limits.py                  # check the tree
    file_size_limits.py --write          # lower shrunk/removed tracked limits
    file_size_limits.py --write --adopt  # also adopt new/grown oversized entries
"""

from __future__ import annotations

import pathlib
import sys
from typing import Callable

_SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import size_limit_baseline as baseline
from size_limit_scan import measure, owning_file, standard_for

_ROOT = _SCRIPT_DIR.parent
_LIMITS = _ROOT / "quality" / "file-size-limits.json"


def _check_group(
    measured: dict[str, int],
    limits: dict[str, int],
    append_only: dict[str, str],
    standard: Callable[[str], int | None],
    new_entry_label: Callable[[str], str],
) -> tuple[list[str], list[str]]:
    """Return (failures, required baseline updates) for one dimension."""
    failures: list[str] = []
    drops: list[str] = []
    for name, lines in sorted(measured.items()):
        if owning_file(name) in append_only:
            continue
        ceiling = standard(name) or 0
        limit = int(limits.get(name, ceiling))
        if lines > limit and name in limits:
            failures.append(
                f"{name}: {lines} lines, {lines - limit} more than its limit of "
                f"{limit}. Split it, or re-run --write --adopt once you mean to "
                "raise the limit."
            )
        elif lines > limit:
            failures.append(
                f"{name}: {lines} lines, over the {ceiling}-line standard for "
                f"{new_entry_label(name)}. Split it before it becomes backlog."
            )
        elif name in limits and lines < limit:
            drops.append(f'    "{name}": {lines},   (was {limit})')
    return failures, drops


def _stale_entries(
    sizes: dict[str, int],
    functions: dict[str, int],
    limits: dict[str, int],
    function_limits: dict[str, int],
    append_only: dict[str, str],
    blind: set[str],
) -> list[str]:
    """Return baseline entries whose source is genuinely gone.

    A tracked entry that is merely unmeasurable right now is not gone -- it
    already failed above, by name. `blind` therefore covers unreadable AND
    unparseable files: an unparseable file keeps its size but contributes no
    functions, so without it every one of its recorded function limits would
    be reported as deleted and the printed remediation would discard real debt.

    A function whose owning file has disappeared entirely is stale too, and
    must be dropped: leaving it behind would grant a legacy ceiling to brand
    new code if that path is ever recreated.
    """
    gone = sorted(
        name
        for name in set(limits) - set(sizes) - set(append_only) - blind
        if not (_ROOT / name).exists()
    )
    gone += sorted(
        name
        for name in set(function_limits) - set(functions)
        if owning_file(name) not in append_only
        and owning_file(name) not in blind
        and (owning_file(name) in sizes or not (_ROOT / owning_file(name)).exists())
    )
    return gone


def _unmeasurable_failures(
    unreadable: list[str], parse_errors: list[tuple[str, str]]
) -> list[str]:
    """Describe every file whose real size or shape could not be established."""
    failures = [
        f"{name}: could not be measured (unreadable, or not valid UTF-8). "
        "An unmeasurable file cannot be silently skipped -- fix its "
        "encoding/permissions, or exclude it explicitly."
        for name in unreadable
    ]
    failures += [
        f"{detail}. An unparseable file cannot be silently skipped -- fix its "
        "syntax, or exclude it explicitly."
        for _name, detail in parse_errors
    ]
    return failures


def main(argv: list[str]) -> int:
    """Compare every tracked file and function against its committed limit."""
    try:
        sizes, functions, unreadable, parse_errors = measure(_ROOT)
    except RuntimeError as exc:
        print(f"file/function size ratchet failed: {exc}", file=sys.stderr)
        return 1

    if "--write" in argv:
        blind = unreadable + [detail for _name, detail in parse_errors]
        return baseline.write(_LIMITS, argv, sizes, functions, blind)

    if not _LIMITS.exists():
        raise SystemExit(
            f"{_LIMITS} is missing; generate it with "
            "scripts/file_size_limits.py --write --adopt"
        )
    limits, function_limits, append_only = baseline.load(_LIMITS)

    failures = _unmeasurable_failures(unreadable, parse_errors)
    file_failures, drops = _check_group(
        sizes,
        limits,
        append_only,
        standard_for,
        lambda name: f"a new {pathlib.PurePosixPath(name).suffix} file",
    )
    func_failures, func_drops = _check_group(
        functions,
        function_limits,
        append_only,
        baseline.function_standard_for,
        lambda _name: "a new function",
    )
    failures += file_failures + func_failures
    drops += func_drops
    for name in _stale_entries(
        sizes,
        functions,
        limits,
        function_limits,
        append_only,
        set(unreadable) | {name for name, _detail in parse_errors},
    ):
        label = "file" if "::" not in name else "function"
        drops.append(f"    remove {name!r}   ({label} no longer exists)")

    if drops:
        print(
            f"Files shrank or disappeared. Update {_LIMITS.name} so they cannot "
            "grow back -- run scripts/file_size_limits.py --write:"
        )
        print("\n".join(drops))

    if failures or drops:
        if failures:
            print("\n".join(failures), file=sys.stderr)
        return 1

    print(
        f"{len(limits)} tracked files and {len(function_limits)} tracked "
        "functions, none over their limit"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
