"""The outside-PR screen's model payload and deterministic floor merge."""

import json
from pathlib import Path

from core.workflow_result import ResultInvalid


def capture_floor(staging: str) -> dict:
    """Snapshot the fixed floor before the worker can access staging."""
    record = json.loads((Path(staging) / "floor.json").read_text())
    if not isinstance(record, dict) or not all(
        isinstance(record.get(key), list) and all(isinstance(x, str) for x in record[key])
        for key in ("paths", "floor_hit")
    ):
        raise ValueError("invalid floor record")
    return record


def validate_captured_result(result: dict, record: dict) -> dict:
    """Check the model's paths and signs, then apply the earlier fixed floor once."""
    signs = result["injection_signs"]
    if len(signs) != len(set(signs)):
        raise ResultInvalid("injection signs must be unique")
    payload = result["payload"]
    if any(finding["path"] not in record["paths"] for finding in payload["findings"]):
        raise ResultInvalid("a finding names a path outside the staged diff")
    floor = set(record["floor_hit"])
    if (floor or signs) and payload["verdict"] == "safe":
        payload["verdict"] = "flagged"
    payload["categories"] = sorted(set(payload["categories"]) | floor)
    payload["floor_hit"] = sorted(floor)
    return result
