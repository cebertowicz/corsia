"""#2214: one writer owns a plan node's state and its revision.

``plans.service.touch_node`` is the only statement that advances
``plan_nodes.revision`` and ``plans.service._transition`` is the only caller
that sets ``plan_nodes.state``. Any other ``UPDATE plan_nodes`` that names
either column is a second writer deciding for itself what counts as a change.
"""

from __future__ import annotations

import re
from pathlib import Path
from tests.plan_writer_authorizer import raw_plan_seed

BACKEND = Path(__file__).resolve().parents[1]
_UPDATE = re.compile(r"UPDATE\s+plan_nodes\s+SET(?P<sets>.*?)\bWHERE\b", re.S | re.I)
_OWNED = re.compile(r"\b(revision|state)\s*=", re.I)


def _runtime_sources():
    for path in sorted(BACKEND.rglob("*.py")):
        relative = path.relative_to(BACKEND)
        if relative.parts[0] in {"tests", ".venv", "venv"}:
            continue
        yield relative, path.read_text()


def test_only_the_service_writes_node_state_or_revision():
    offenders = [
        f"{relative}: {' '.join(match.group(0).split())[:120]}"
        for relative, text in _runtime_sources()
        for match in _UPDATE.finditer(text)
        if _OWNED.search(match.group("sets")) and str(relative) != "plans/service.py"
    ]
    assert offenders == [], "route these through plans.service.touch_node:\n" + "\n".join(offenders)


def test_the_service_has_exactly_one_revision_statement():
    text = (BACKEND / "plans" / "service.py").read_text()
    writes = [m for m in _UPDATE.finditer(text) if _OWNED.search(m.group("sets"))]
    assert len(writes) == 1
    assert "revision=revision+1" in writes[0].group("sets")


def test_the_scan_catches_a_second_writer():
    sample = 'db.execute("UPDATE plan_nodes SET revision=revision+1, updated_at=? WHERE id=?")'
    match = _UPDATE.search(sample)
    assert match is not None and _OWNED.search(match.group("sets"))
    assert not _OWNED.search("order_key=order_key+1, requirements_revision=? ")
