
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class ScenarioStatus(str):

    PASS = "pass"
    FAIL = "fail"
    BLOCKED = "blocked"
    ERROR = "error"


@dataclass(frozen=True)
class ScoreResult:

    name: str
    passed: bool
    explanation: str = ""
    raw_value: Any = None


@dataclass
class ScenarioResult:

    scenario_id: str
    status: str
    scorer_outcomes: dict[str, ScoreResult] = field(default_factory=dict)
    endpoint_response: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    blocked_scorers: list[str] = field(default_factory=list)
    unknown_scorers: list[str] = field(default_factory=list)
    turn_attrs: list[dict[str, Any]] = field(default_factory=list)
    turn_responses: list[dict[str, Any]] = field(default_factory=list)
    mode: str = "A"


__all__ = ["ScenarioResult", "ScenarioStatus", "ScoreResult"]
