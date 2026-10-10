"""The System tier's persisted section key is ONE name across its writer and every reader.

`origin_section` is stored in installed-context rows, restored by key (`installed_parts`),
mapped in usage attribution, recorded in the guidance manifest and labelled in the sheet
component. A reader that expects a different name silently drops the System tier for every
existing thread, and a writer that emits a different name orphans every stored row, so a split
in EITHER direction is the failure this test catches.

The two name forms come from the rename map, never from a literal here, and the positions
scanned are the ones that name a section: a line that mentions `origin_section`, a tuple/set
vocabulary that carries the sibling sections, or a bare brand literal used as a mapping key.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
# Test data: the former name, which schema v79 converted out of every stored row.
LEGACY_NAME = "maestro"
# Test data: the product name, and the persisted System section key.
NEW_NAME = "corsia"
SCAN_ROOTS = ("backend", "frontend/src")
SKIP_PARTS = {"node_modules", "__pycache__", ".venv", "venv311"}
LINE_RE = re.compile(r"[\"']([A-Za-z][A-Za-z0-9_-]*)[\"']")
SECTION_NAMES = ("origin_section", "originSection", "_BASE_ORIGINS", "installed_parts", "component_id")
DICT_KEY_RE = re.compile(r"^\s*[\"']([A-Za-z][A-Za-z0-9_-]*)[\"']\s*:")


def _name_forms() -> tuple[str, str]:
    return LEGACY_NAME, NEW_NAME


def _files() -> list[Path]:
    """Source files that know the section key at all: the tier's writer, readers and their tests."""
    out: list[Path] = []
    for root in SCAN_ROOTS:
        for path in (REPO_ROOT / root).rglob("*"):
            if path.suffix not in {".py", ".ts", ".tsx", ".svelte"}:
                continue
            if SKIP_PARTS & set(path.parts):
                continue
            if "rename_identity" in path.name:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if "origin_section" in text or "originSection" in text:
                out.append(path)
    return out


def _section_positions(path: Path) -> list[tuple[int, str, str]]:
    """(line number, brand form, line) for each literal that names an origin section."""
    old, new = _name_forms()
    forms = {old, new}
    found: list[tuple[int, str, str]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").split("\n"), start=1):
        if not any(form in line for form in forms):
            continue
        literals = [m.group(1) for m in LINE_RE.finditer(line)]
        mentions_section = any(name in line for name in SECTION_NAMES)
        sibling_vocabulary = {"system", "workspace"} <= set(literals)
        if mentions_section or sibling_vocabulary:
            named = literals
        elif dict_key := DICT_KEY_RE.match(line):
            # A bare section name used as a mapping key (usage attribution's bucket map); only
            # the key counts, so a brand value elsewhere on the line is not a section position.
            named = [dict_key.group(1)]
        else:
            continue
        for literal in named:
            if literal in forms:
                found.append((number, literal, line.strip()))
    return found


def test_the_system_section_key_is_one_name_everywhere() -> None:
    old, new = _name_forms()
    seen: dict[str, list[str]] = {old: [], new: []}
    for path in _files():
        rel = str(path.relative_to(REPO_ROOT))
        for number, form, line in _section_positions(path):
            seen[form].append(f"{rel}:{number}: {line[:110]}")
    assert len({item.split(":")[0] for item in seen[new]}) >= 2, (
        "the writer and at least one reader must both name the section: the check would prove nothing"
    )
    # Schema v79 converted every stored row to the new key, so the former one finds none of them.
    assert not seen[old], (
        f"{len(seen[old])} origin-section position(s) use '{old}' while the System tier's "
        f"persisted key is '{new}' ({len(seen[new])} position(s)); a split in either "
        "direction drops or orphans the tier:\n  " + "\n  ".join(seen[old])
    )