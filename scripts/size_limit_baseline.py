"""Baseline persistence and the adopt guards for the code-health ratchet.

Everything about the committed baseline lives here: reading it, deciding which
measured debt may be recorded, and rewriting it. The check itself lives in
``file_size_limits.py``, which owns the baseline path and passes it in -- no
module-level coupling, so a caller can point this at a scratch baseline.

The central rule: a tracked limit may only move down on its own. Raising one,
whether because the entry grew or because it is brand new already over the
standard, needs ``--adopt``.
"""

from __future__ import annotations

import json
import pathlib
import sys

from size_limit_scan import (
    FUNCTION_STANDARD,
    STANDARDS,
    over_standard,
    owning_file,
    standard_for,
)

_KINDS = ", ".join(f"{suffix} {limit}" for suffix, limit in sorted(STANDARDS.items()))

DEFAULT_COMMENT = [
    "Maximum line count allowed per oversized source file, and per oversized",
    "function, across every tracked source file in the repository. CI fails",
    "when an entry grows past its limit, so the decomposition backlog cannot",
    "get worse while it is worked down.",
    "An entry NOT listed here must meet its standard. Files: " + _KINDS + ".",
    f"Functions: {FUNCTION_STANDARD} lines, Python only -- a language whose",
    "spans we cannot get from a real parser is not guessed at. New code gets",
    "no exemption.",
    "When you shrink an entry, lower the number here in the same change; the",
    "check prints the exact replacement values.",
    "A file that legitimately only ever grows (an append-only history, e.g.",
    'a migrations file) goes in "append_only" instead, with a one-line',
    "reason. It is still measured for visibility but never limited or",
    "failed, its functions are exempt too, and --write never touches it.",
    "Regenerate with: scripts/file_size_limits.py --write",
    "That only ever lowers or drops existing entries. An entry that grew, or",
    "new code already over standard, blocks the write; re-run with --adopt",
    "once you have looked at it and mean to accept it.",
]

Unsafe = tuple[str, list[str], list[str]]


def function_standard_for(_name: str) -> int:
    """Return the single function standard, whatever the entry is called."""
    return FUNCTION_STANDARD


def load(limits_path: pathlib.Path) -> tuple[dict[str, int], dict[str, int], dict[str, str]]:
    """Return (file limits, function limits, append_only) from the baseline.

    The baseline's own "_comment" is not returned: --write always replaces it
    with DEFAULT_COMMENT, so the committed copy is derived, never read back.
    """
    if not limits_path.exists():
        return {}, {}, {}
    data = json.loads(limits_path.read_text(encoding="utf-8"))
    return (
        dict(data.get("limits", {})),
        dict(data.get("function_limits", {})),
        dict(data.get("append_only", {})),
    )


def recorded_debt(
    sizes: dict[str, int], functions: dict[str, int], append_only: dict[str, str]
) -> tuple[dict[str, int], dict[str, int]]:
    """Return over-standard files and functions, minus append-only exemptions."""
    files = {
        name: lines
        for name, lines in over_standard(sizes, standard_for).items()
        if name not in append_only
    }
    funcs = {
        name: lines
        for name, lines in over_standard(functions, function_standard_for).items()
        if owning_file(name) not in append_only
    }
    return files, funcs


def classify(
    recorded: dict[str, int], known: dict[str, int]
) -> tuple[list[str], list[str], dict[str, int]]:
    """Split measured debt into (adopted, grown, always-safe limits).

    A shrink -- an existing tracked entry still over standard but no higher
    than its recorded limit -- is always safe and always written, regardless
    of what else is going on: the common decomposition workflow shrinks the
    original file being split in the SAME change that adds new over-standard
    modules, and that shrink must never be held hostage by --adopt.
    """
    adopted = sorted(set(recorded) - set(known))
    grown = sorted(
        name for name in recorded if name in known and recorded[name] > known[name]
    )
    safe = {name: lines for name, lines in known.items() if name in recorded}
    safe.update(
        {
            name: lines
            for name, lines in recorded.items()
            if name in known and name not in grown
        }
    )
    return adopted, grown, safe


def resolve_dimension(
    argv: list[str], recorded: dict[str, int], known: dict[str, int]
) -> tuple[dict[str, int], Unsafe | None]:
    """Apply --adopt to one dimension, or report why that dimension is unsafe.

    Each dimension is judged on its own. Coupling them makes an ordinary
    change unresolvable: adding an over-standard function to an already
    tracked oversized file grows that file BY adding the function, so a
    combined guard sees one new entry and one grown entry that are the same
    event, refuses every run, and prints a remediation nobody can follow --
    there is no run in which only one of them is present.
    """
    adopted, grown, limits = classify(recorded, known)
    if adopted and grown:
        return limits, ("both", adopted, grown)
    if (adopted or grown) and "--adopt" not in argv:
        return limits, ("no-adopt", adopted, grown)
    for name in (*adopted, *grown):
        limits[name] = recorded[name]
    return limits, None


def _print_entries(
    label: str, adopted: list[str], grown: list[str], known: dict, recorded: dict
) -> None:
    """Print the two unsafe categories --write is refusing to write this run."""
    if adopted:
        print(
            f"  {len(adopted)} {label}(s) over the standard and not yet tracked:",
            file=sys.stderr,
        )
        for name in adopted:
            print(f"    {name}: {recorded[name]}", file=sys.stderr)
    if grown:
        print(
            f"  {len(grown)} tracked {label}(s) grew past their recorded limit:",
            file=sys.stderr,
        )
        for name in grown:
            print(f"    {name}: {known[name]} -> {recorded[name]}", file=sys.stderr)


def report_unsafe(
    label: str, unsafe: Unsafe, known: dict[str, int], recorded: dict[str, int]
) -> None:
    """Explain why one dimension was refused, and name every entry involved."""
    reason, adopted, grown = unsafe
    if reason == "both":
        print(
            f"refusing to write: this run has both brand-new over-standard "
            f"{label}(s) and tracked {label}(s) that grew. Adopting the new "
            "one(s) must not also raise the grown one(s) by accident -- "
            "resolve the grown one(s) on their own first (split them, or "
            "adopt them alone), then adopt the new one(s) separately:",
            file=sys.stderr,
        )
    else:
        print(
            f"refusing to raise limits without --adopt. Split the {label}(s) "
            "instead, or re-run with --write --adopt once you mean to accept "
            "the new ceiling:",
            file=sys.stderr,
        )
    _print_entries(label, adopted, grown, known, recorded)


def write(
    limits_path: pathlib.Path,
    argv: list[str],
    sizes: dict[str, int],
    functions: dict[str, int],
    blind: list[str],
) -> int:
    """Rewrite the baseline. A shrink is always recordable; a raise never is,
    without --adopt -- and adopting new entries never also raises an unrelated
    grown one in the same dimension.
    """
    if blind:
        print(
            "refusing to write: the following file(s) could not be measured, "
            "so their real size is unknown and the baseline cannot be trusted:",
            file=sys.stderr,
        )
        for detail in blind:
            print(f"    {detail}", file=sys.stderr)
        return 1

    known_files, known_functions, append_only = load(limits_path)
    recorded_files, recorded_functions = recorded_debt(sizes, functions, append_only)
    limits, file_unsafe = resolve_dimension(argv, recorded_files, known_files)
    function_limits, func_unsafe = resolve_dimension(
        argv, recorded_functions, known_functions
    )

    if file_unsafe:
        report_unsafe("file", file_unsafe, known_files, recorded_files)
    if func_unsafe:
        report_unsafe("function", func_unsafe, known_functions, recorded_functions)

    payload: dict = {
        "_comment": DEFAULT_COMMENT,
        "limits": dict(sorted(limits.items())),
        "function_limits": dict(sorted(function_limits.items())),
    }
    if append_only:
        payload["append_only"] = dict(sorted(append_only.items()))
    limits_path.parent.mkdir(parents=True, exist_ok=True)
    limits_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(
        f"wrote {len(limits)} file and {len(function_limits)} function limits "
        f"to {limits_path.name}"
    )
    return 1 if (file_unsafe or func_unsafe) else 0
