"""Keep contract tests explicit outside the CI merge gate.

The backend CI job sets ``CORSIA_CONTRACT_SUITE=1`` so these assertions are
part of every pull-request gate. Local broad test runs skip the tier unless the
same opt-in is set; focused contract runs must enable it explicitly.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]


def pytest_collection_modifyitems(config, items):
    if os.environ.get("CORSIA_CONTRACT_SUITE") == "1":
        return
    skip = pytest.mark.skip(
        reason="contract suite runs only with CORSIA_CONTRACT_SUITE=1",
    )
    contract_dir = Path(__file__).resolve().parent
    for item in items:
        # Only THIS directory. A substring match on "contract" silently
        # disabled ~290 unrelated tests whose filenames contain the word
        # (test_step_return_contract.py among them) — discovered by a skip
        # histogram, which is now the reason this comment exists.
        try:
            in_dir = Path(str(item.fspath)).resolve().is_relative_to(contract_dir)
        except (OSError, ValueError):
            in_dir = False
        if in_dir:
            item.add_marker(skip)


def iter_backend_sources():
    """Yield (path, text) for production backend sources, excluding sanctioned writers."""
    excluded_parts = {"tests", "__pycache__"}
    excluded_files = {
        # E2E fixtures seed state deliberately.
        BACKEND_ROOT / "e2e_mock.py",
        BACKEND_ROOT / "e2e_app.py",
        BACKEND_ROOT / "e2e_pressure_fixture.py",
    }
    for path in BACKEND_ROOT.rglob("*.py"):
        if excluded_parts & set(part for part in path.parts):
            continue
        if path in excluded_files:
            continue
        yield path, path.read_text(errors="replace")
