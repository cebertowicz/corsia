#!/usr/bin/env python3
"""Freeze frontend design-token debt so it can only improve.

The committed baseline records every currently governed frontend source file.
Normal checks fail only when a category rises, or a new file has a violation.
``--update-baseline`` removes deleted files and lowers recorded counts; it
refuses to write if any existing count would rise or a new file has debt.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

from design_token_scan import CATEGORIES, Violation, counts, scan

_SCRIPT_DIR = Path(__file__).resolve().parent
_ROOT = _SCRIPT_DIR.parent
_BASELINE = _ROOT / "quality" / "design-token-ratchet.json"
_HINTS = {
    "raw_colors": "Use a semantic colour token from app-tokens.css.",
    "raw_px": "Use --space-*, type, radius, or layout tokens.",
    "heavy_borders": "§0.2: use a 1px tokenized border, not a heavy border.",
    "raw_shadows": "§0.2: use --shadow-* or --glass-rim tokens.",
    "row_menu_buttons": "§0.1 Row menu: use the shared row menu, not a ⋯ button.",
}


def load(path: Path) -> dict[str, dict[str, int]]:
    """Load and lightly validate the committed baseline."""
    data = json.loads(path.read_text(encoding="utf-8"))
    files = data.get("files")
    if not isinstance(files, dict):
        raise ValueError("baseline must contain a 'files' object")
    return {
        str(name): {category: int(values.get(category, 0)) for category in CATEGORIES}
        for name, values in files.items()
    }


def payload(measured: dict[str, dict[str, int]]) -> dict[str, object]:
    """Return the deterministic on-disk baseline representation."""
    return {
        "_comment": [
            "Per-file frontend design-token debt frozen by scripts/design_token_ratchet.py.",
            "Run --update-baseline after reducing debt; it only lowers counts or removes files.",
        ],
        "files": dict(sorted(measured.items())),
    }


def totals(measured: dict[str, dict[str, int]]) -> dict[str, int]:
    """Sum each category across all governed files."""
    return {category: sum(row[category] for row in measured.values()) for category in CATEGORIES}


def violations(
    measured: dict[str, dict[str, int]], baseline: dict[str, dict[str, int]]
) -> list[tuple[str, str]]:
    """Return every increased category, including debt in a new file."""
    failures: list[tuple[str, str]] = []
    for path, row in measured.items():
        previous = baseline.get(path)
        for category, current in row.items():
            if (previous is None and current) or (previous is not None and current > previous[category]):
                failures.append((path, category))
    return failures


def print_failures(
    failures: list[tuple[str, str]], findings: dict[str, list[Violation]]
) -> None:
    """Print source locations for each increased category."""
    for path, category in failures:
        print(f"{path}: {category} increased", file=sys.stderr)
        for finding in findings[path]:
            if finding.category == category:
                print(f"  {finding.line}: {finding.text}", file=sys.stderr)
        print(f"  Hint: {_HINTS[category]}", file=sys.stderr)


def update(
    measured: dict[str, dict[str, int]], baseline: dict[str, dict[str, int]]
) -> tuple[dict[str, dict[str, int]], list[tuple[str, str]]]:
    """Return only safe baseline reductions and the changes that would raise it."""
    unsafe = violations(measured, baseline)
    if unsafe:
        return baseline, unsafe
    lowered = {
        path: {
            category: min(current, baseline[path][category])
            for category, current in row.items()
        }
        for path, row in measured.items()
        if path in baseline
    }
    return lowered, []


def print_report(measured: dict[str, dict[str, int]]) -> None:
    """Print totals and the ten files with the most recorded debt."""
    print("Baseline totals: " + ", ".join(f"{key}={value}" for key, value in totals(measured).items()))
    ranking = sorted(measured.items(), key=lambda item: (-sum(item[1].values()), item[0]))
    print("Top files:")
    for path, row in ranking[:10]:
        print(f"  {path}: " + ", ".join(f"{key}={value}" for key, value in row.items() if value))


def main(argv: list[str]) -> int:
    """Check, report, or safely lower the token-compliance baseline."""
    if set(argv) - {"--update-baseline", "--report", "--write-initial-baseline"}:
        raise SystemExit(
            "usage: design_token_ratchet.py [--update-baseline] [--report] "
            "[--write-initial-baseline]"
        )
    if "--write-initial-baseline" in argv:
        if _BASELINE.exists():
            raise SystemExit(f"refusing to overwrite existing {_BASELINE}")
        measured = counts(scan(_ROOT))
        _BASELINE.parent.mkdir(parents=True, exist_ok=True)
        _BASELINE.write_text(json.dumps(payload(measured), indent=2) + "\n", encoding="utf-8")
        print(f"wrote initial baseline for {len(measured)} frontend files")
        if "--report" in argv:
            print_report(measured)
        return 0
    if not _BASELINE.exists():
        raise SystemExit(f"{_BASELINE} is missing; write it once with --write-initial-baseline")
    findings = scan(_ROOT)
    measured = counts(findings)
    baseline = load(_BASELINE)
    if "--update-baseline" in argv:
        updated, unsafe = update(measured, baseline)
        if unsafe:
            print("refusing to raise design-token baseline:", file=sys.stderr)
            print_failures(unsafe, findings)
            return 1
        _BASELINE.write_text(json.dumps(payload(updated), indent=2) + "\n", encoding="utf-8")
        print(f"updated baseline: {len(baseline) - len(updated)} removed files, counts only lowered")
        measured = updated
    else:
        failures = violations(measured, baseline)
        if failures:
            print_failures(failures, findings)
            return 1
        print(f"{len(measured)} frontend files, no design-token category above baseline")
    if "--report" in argv:
        print_report(measured)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
