"""The context sheet groups every ``origin_section`` the backend emits.

``frontend/src/lib/components/contextSheetModel.ts`` maps each source row's
``origin_section`` to a visible category and falls back to "Dynamic" for one
it does not know, so a new producer value would land in the wrong group
silently. This test collects every literal ``origin_section`` the production
backend writes and holds it to the committed fixture the frontend's
exhaustiveness test reads: when a producer adds a value, this fails until the
fixture is regenerated with ``CORSIA_WRITE_CONTRACT_FIXTURES=1``, and the
frontend test then requires a decision for it.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
FIXTURE = (
    BACKEND.parent
    / "frontend/src/lib/__tests__/fixtures/context-origin-sections.contract.json"
)


def _literal(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _emitted_origin_sections() -> set[str]:
    found: set[str] = set()
    for path in BACKEND.rglob("*.py"):
        relative = path.relative_to(BACKEND).parts
        if "tests" in relative or relative[0] in {"venv", ".venv"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == "origin_section":
                section = _literal(node.value)
                if section:
                    found.add(section)
            elif isinstance(node, ast.Dict):
                for key, item in zip(node.keys, node.values):
                    if key is not None and _literal(key) == "origin_section":
                        section = _literal(item)
                        if section:
                            found.add(section)
    return found


def test_fixture_lists_every_emitted_origin_section():
    emitted = sorted(_emitted_origin_sections())
    if os.environ.get("CORSIA_WRITE_CONTRACT_FIXTURES") == "1":
        FIXTURE.write_text(json.dumps(emitted, indent=2) + "\n", encoding="utf-8")
    committed = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert committed == emitted


def test_scan_finds_known_producers():
    # Guards the scan itself: a parse change that found nothing would pass
    # the equality check against an emptied fixture.
    assert {"corsia", "project", "role", "history", "dynamic"} <= _emitted_origin_sections()
