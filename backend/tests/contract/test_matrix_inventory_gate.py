"""The lifecycle gate must reject missing or malformed coverage evidence."""

import pytest

from tests.contract import test_i10_lifecycle_matrix as gate
from tests.contract.matrix_runner import CellResult, HARNESSES, SCENARIOS

pytestmark = pytest.mark.contract


def _results():
    return [CellResult(scenario, harness, "covered")
            for scenario in SCENARIOS for harness in HARNESSES]


@pytest.mark.parametrize("damage", ["empty", "missing", "duplicate", "unknown", "status"])
def test_matrix_gate_rejects_incomplete_or_malformed_inventory(tmp_path, monkeypatch, damage):
    results = _results()
    if damage == "empty":
        results.clear()
    elif damage == "missing":
        results.pop()
    elif damage == "duplicate":
        results.append(results[0])
    elif damage == "unknown":
        results.append(CellResult("unregistered-scenario", "native", "covered"))
    else:
        results[0].status = "not-executed"

    async def runner(_scenarios, _root):
        return results

    # Substitute only the expensive journey execution. Exercise the actual
    # merge gate's judgment of returned evidence, including its render path.
    monkeypatch.setattr(gate, "run_matrix", runner)
    with pytest.raises(AssertionError, match="matrix inventory"):
        gate.test_turn_lifecycle_matrix(tmp_path)


def test_matrix_gate_accepts_complete_valid_inventory(tmp_path, monkeypatch):
    results = _results()

    async def runner(_scenarios, _root):
        return results

    monkeypatch.setattr(gate, "run_matrix", runner)
    gate.test_turn_lifecycle_matrix(tmp_path)
