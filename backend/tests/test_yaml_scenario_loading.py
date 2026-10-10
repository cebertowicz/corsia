
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from eval.datasets import from_yaml_scenario
from eval.scorers.resolve import resolve_per_sample_scorers


_SCENARIOS_ENV = "CORSIA_SPRINT1_SCENARIOS_DIR"
_SCENARIOS_DIR_VALUE = os.environ.get(_SCENARIOS_ENV)
_SCENARIOS_DIR = Path(_SCENARIOS_DIR_VALUE) if _SCENARIOS_DIR_VALUE else None
_SCENARIOS_AVAILABLE = _SCENARIOS_DIR is not None and _SCENARIOS_DIR.is_dir()


def _scenario_files() -> list[Path]:
    if not _SCENARIOS_AVAILABLE or _SCENARIOS_DIR is None:
        return []
    return sorted(_SCENARIOS_DIR.glob("*.yaml"))


pytestmark = pytest.mark.skipif(
    not _SCENARIOS_AVAILABLE,
    reason=f"Set {_SCENARIOS_ENV} to opt into historical Sprint-1 scenarios",
)


def test_all_sprint_1_scenarios_load_to_samples():
    files = _scenario_files()
    assert len(files) >= 5, f"Expected ≥5 scenarios, found {len(files)}"

    for path in files:
        with path.open("r", encoding="utf-8") as f:
            scenario = yaml.safe_load(f)
        sample = from_yaml_scenario(scenario)
        assert sample.id == scenario["id"]


def test_scenarios_carry_scorers_into_metadata():
    files = _scenario_files()
    samples = []
    for path in files:
        with path.open("r", encoding="utf-8") as f:
            scenario = yaml.safe_load(f)
        if "scorers" not in scenario:
            continue
        sample = from_yaml_scenario(scenario)
        samples.append(sample)
        assert "scorers" in (sample.metadata or {}), (
            f"Scenario {sample.id}: scorers not folded into metadata"
        )

    assert len(samples) >= 4, (
        "Expected ≥4 of the Sprint-1 Slice-2 scenarios to declare scorers"
    )


def test_resolve_per_sample_scorers_against_real_scenarios():
    samples = []
    for path in _scenario_files():
        with path.open("r", encoding="utf-8") as f:
            scenario = yaml.safe_load(f)
        samples.append(from_yaml_scenario(scenario))

    resolved = resolve_per_sample_scorers(samples)

    for s in samples:
        assert s.id in resolved
