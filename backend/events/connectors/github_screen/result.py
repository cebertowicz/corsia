"""The outside-PR screen's model payload and deterministic floor merge."""

import json
from pathlib import Path

import yaml

from core.workflow_result import ResultInvalid, check_result

WORKFLOW_DIR = Path(__file__).parent / "github-pr-screen"
SCREEN_SCHEMA = yaml.safe_load((WORKFLOW_DIR / "corsia.yaml").read_text())["workflow"]["result"]


def apply_floor(payload: dict, paths: set[str], floor_hits: set[str]) -> dict:
    """Check paths against the staged diff, then apply the earlier fixed floor."""
    if any(finding["path"] not in paths for finding in payload["findings"]):
        raise ResultInvalid("a finding names a path outside the staged diff")
    floor = sorted(floor_hits)
    if floor and payload["verdict"] == "safe":
        payload["verdict"] = "flagged"
    payload["categories"] = sorted(set(payload["categories"]) | floor_hits)
    payload["floor_hit"] = floor
    return payload


def capture_floor(staging: str | None) -> dict:
    """Snapshot the fixed floor before the worker can access staging."""
    record = json.loads((Path(staging) / "floor.json").read_text())
    if not isinstance(record, dict) or not all(
        isinstance(record.get(key), list) and all(isinstance(x, str) for x in record[key])
        for key in ("paths", "floor_hit")
    ):
        raise ValueError("invalid floor record")
    return record


def apply_captured_floor(result: dict, record: dict) -> dict:
    signs = result["injection_signs"]
    if len(signs) != len(set(signs)):
        raise ResultInvalid("injection signs must be unique")
    payload = apply_floor(result["payload"], set(record["paths"]), set(record["floor_hit"]))
    if signs and payload["verdict"] == "safe":
        payload["verdict"] = "flagged"
    return result


def screen_result(text: str, paths: set[str], floor_hits: set[str]) -> dict:
    """Validate the model result, then apply the earlier deterministic floor."""
    return apply_captured_floor(check_result(text, SCREEN_SCHEMA),
                                {"paths": list(paths), "floor_hit": list(floor_hits)})
