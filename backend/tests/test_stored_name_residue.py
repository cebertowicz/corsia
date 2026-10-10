"""After schema v79 the source keeps the former product name only where it converts it.

A reader or seed that still spells the former name finds none of the converted
rows, so this scans the source for it rather than trusting a review to.
"""

from __future__ import annotations

from pathlib import Path

from stored_names_fixtures import OLD

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOTS = ("backend", "frontend/src", "scripts")
SUFFIXES = {".py", ".ts", ".tsx", ".js", ".svelte", ".sh", ".json", ".sql", ".md", ".toml", ".yml"}
SKIP_PARTS = {"node_modules", "__pycache__", ".venv", "venv311"}
TESTS = "backend/tests/"
# The code that converts the former name away is the only code that may spell it.
CONVERTERS = {
    "backend/core/skill_key_conversion.py",
    "backend/infra/migration_78_to_79_section_key.py",
}


def _sources() -> dict[str, str]:
    found: dict[str, str] = {}
    for root in SOURCE_ROOTS:
        for path in (REPO_ROOT / root).rglob("*"):
            if path.suffix not in SUFFIXES or SKIP_PARTS & set(path.parts) or not path.is_file():
                continue
            found[path.relative_to(REPO_ROOT).as_posix()] = path.read_text(
                encoding="utf-8", errors="replace",
            )
    return found


def test_only_the_converters_spell_the_former_name() -> None:
    spelled = {
        name for name, text in _sources().items()
        if OLD in text.lower() and not name.startswith(TESTS)
    }

    assert spelled == CONVERTERS
