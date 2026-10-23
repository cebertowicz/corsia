"""Carry-in action contracts and the admitted local fresh-turn runner.

This module keeps the mechanical contract separate from durable carry-in facts.
It neither imports the disposable eval harness nor chooses or loads a model.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypeVar

from loguru import logger

Action = dict[str, Any]
Verdict = Literal["pass", "fail"]
T = TypeVar("T")

ACTION_FIELDS = frozenset({"tool", "params"})
ACTIONS_FIELDS = frozenset({"required_actions", "forbidden_actions", "decisive_action"})
SAMPLE_COUNT = 3
PASS_THRESHOLD = 2
# One pending case per existing catalog-ready trigger bounds recovery work.
CASES_PER_EDIT_CAP = 1
PENDING_EVENT = "learning_loop.carry_in_pending"
RESULT_EVENT = "learning_loop.carry_in_result"
RETIREMENT_EVENT = "learning_loop.carry_in_retirement"
SOURCE_SKIPPED_EVENT = "learning_loop.carry_in_source_skipped"


class TraceAdapter(Protocol):
    """The owned boundary to the local fresh-turn runner."""

    def ready(self) -> bool:
        """Whether an already-loaded local model may receive a turn now."""

    def run(self, *, scenario: str) -> Sequence[Mapping[str, Any]]:
        """Return canonical ``EvalTurnResult.tool_calls`` for one fresh turn."""


def ready_local_target():
    """Resolve the sole ready local catalog row without loading, switching, or cloud fallback."""
    from llm.model_status import get_model_status_rows
    from llm.resolution import resolve_execution_target

    rows = [
        row for row in get_model_status_rows()
        if row.get("status") == "ready" and row.get("provider") == "omlx" and row.get("is_local")
    ]
    if len(rows) != 1:
        return None
    target = resolve_execution_target(str(rows[0]["litellm_model"]))
    return target if target.harness == "native" and target.provider == "omlx" and target.available else None


class LocalTraceAdapter:
    """Admission-gated bridge to the one shared product fresh-turn runner."""

    def __init__(self, user_id: str, scope=None, runner: Callable[..., Any] | None = None):
        self.user_id = user_id
        self.scope = scope
        self._runner = runner
        self._target = None
        self._fenced_target = None
        self._cleanups: list[Callable[[], Any]] = []

    def set_fenced_target(self, target) -> None:
        """Deliver one immutable target blob through the hidden eval thread."""
        self._fenced_target = target

    def current_model_identity(self) -> str | None:
        """Expose the exact local selector and resolved catalog identity without dispatching."""
        target = self._target or ready_local_target()
        if target is None:
            return None
        return json.dumps({"resolved_identity": target.catalog_id, "selector": target.transport_model}, sort_keys=True)

    def cleanup(self) -> None:
        """Remove every hidden eval attachment only after its outcome is recorded."""
        cleanups, self._cleanups = self._cleanups, []
        for cleanup in cleanups:
            asyncio.run(cleanup())

    def ready(self) -> bool:
        from llm.capabilities import get_active_local_threads, snapshot_active_threads

        self._target = ready_local_target()
        return bool(self._target and not get_active_local_threads(snapshot_active_threads()))

    def run(self, *, scenario: str) -> Sequence[Mapping[str, Any]]:
        from eval.fresh_turn import run_fresh_turn

        target = self._target or ready_local_target()
        if target is None:
            raise RuntimeError("no ready local model")
        started = time.perf_counter()
        scope = self.scope
        if scope is not None and self._fenced_target is not None:
            scope = replace(scope, fenced_target=self._fenced_target)
        fresh = asyncio.run((self._runner or run_fresh_turn)(
            user_id=self.user_id,
            prompt=scenario,
            model=target.transport_model,
            timeout_seconds=120.0,
            scope=scope,
        ))
        cleanup = getattr(fresh, "cleanup", None)
        if cleanup is not None:
            self._cleanups.append(cleanup)
        self.last_receipt = {
            "wall_seconds": round(time.perf_counter() - started, 3),
            "usage": {} if fresh.result is None else getattr(fresh.result, "usage", {}),
            "error": fresh.error,
            "selector": target.transport_model,
            "identity": target.catalog_id,
            "fixture_sha": getattr(getattr(fresh, "context_receipt", None), "fixture_sha", None),
            "delivered_blob_sha": getattr(getattr(fresh, "context_receipt", None), "delivered_blob_sha", None),
            "neighbour_shas": getattr(getattr(fresh, "context_receipt", None), "neighbour_shas", ()),
        }
        return () if fresh.result is None else fresh.result.tool_calls


@dataclass(frozen=True)
class TraceScore:
    """The first decisive or forbidden call's mechanical verdict."""

    verdict: Verdict
    reason: str
    decisive_call_index: int | None


@dataclass(frozen=True)
class CarryInRun:
    """Three samples, or a pending result when local admission is unavailable."""

    state: Literal["pending", "settled"]
    scores: tuple[TraceScore, ...] = ()
    selector: str | None = None
    identity: str | None = None
    target_hash: str | None = None
    fixture_sha: str | None = None
    delivered_blob_sha: str | None = None
    neighbour_shas: tuple[str, ...] = ()
    reason: str | None = None

    @property
    def verdict(self) -> Verdict | None:
        if self.state != "settled":
            return None
        return "pass" if sum(score.verdict == "pass" for score in self.scores) >= PASS_THRESHOLD else "fail"


def scenario_for_case(case: Mapping[str, Any]) -> str:
    """Build the scenario-only fresh-turn prompt; scoring data never crosses this boundary."""
    situation = case.get("situation", case.get("what_happened"))
    if not isinstance(situation, str) or not situation.strip():
        raise ValueError("carry-in case needs a non-empty situation or what_happened")
    return json.dumps({"situation": situation.strip()}, sort_keys=True)


def _json_value(value: object) -> bool:
    if value is None or isinstance(value, (str, int, float, bool)):
        return True
    if isinstance(value, list):
        return all(_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _json_value(item) for key, item in value.items())
    return False


def _action(value: object, *, field: str) -> Action:
    if not isinstance(value, dict) or set(value) != ACTION_FIELDS:
        raise ValueError(f"corsia.lesson.case.actions.{field} actions must be {{tool, params}}")
    tool, params = value.get("tool"), value.get("params")
    if not isinstance(tool, str) or not tool.strip() or not isinstance(params, dict) or not _json_value(params):
        raise ValueError(f"corsia.lesson.case.actions.{field} actions need a tool and JSON-object params")
    return {"tool": tool.strip(), "params": params}


def validate_actions(value: object) -> dict[str, Any] | None:
    """Validate the intentionally small, file-local action contract.

    It deliberately excludes evaluator bindings, shell semantics and allowances:
    a lesson needing those remains a case without an executable carry-in test.
    """
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != ACTIONS_FIELDS:
        raise ValueError(
            "corsia.lesson.case.actions must name required_actions, forbidden_actions and decisive_action"
        )
    required_raw, forbidden_raw = value.get("required_actions"), value.get("forbidden_actions")
    if not isinstance(required_raw, list) or not required_raw or not isinstance(forbidden_raw, list):
        raise ValueError("corsia.lesson.case.actions requires non-empty required_actions and list forbidden_actions")
    required = [_action(item, field="required_actions") for item in required_raw]
    forbidden = [_action(item, field="forbidden_actions") for item in forbidden_raw]
    decisive = _action(value.get("decisive_action"), field="decisive_action")
    if decisive not in required:
        raise ValueError("corsia.lesson.case.actions.decisive_action must be one declared required action")
    return {
        "required_actions": required,
        "forbidden_actions": forbidden,
        "decisive_action": decisive,
    }


def _parsed_params(value: object) -> dict[str, Any] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, dict) else None


def canonical_calls(calls: Sequence[Mapping[str, Any]]) -> list[Action]:
    """Normalize the canonical eval projection, including its generic tool wrapper."""
    normalized: list[Action] = []
    for call in calls:
        name = call.get("name") or call.get("tool")
        params = _parsed_params(call.get("arguments", call.get("params", {})))
        if not isinstance(name, str) or params is None:
            continue
        if name == "tool_call":
            inner_name, inner_params = params.get("name"), _parsed_params(params.get("arguments"))
            if not isinstance(inner_name, str) or inner_params is None:
                continue
            name, params = inner_name, inner_params
        normalized.append({"tool": name, "params": params})
    return normalized


def _subset(expected: object, actual: object) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(key in actual and _subset(value, actual[key]) for key, value in expected.items())
    return expected == actual


def action_matches(call: Mapping[str, Any], action: Mapping[str, Any]) -> bool:
    """Match an action's params as a subset of one canonical call's params."""
    return call.get("tool") == action.get("tool") and _subset(action.get("params", {}), call.get("params", {}))


def score_trace(actions: Mapping[str, Any], calls: Sequence[Mapping[str, Any]]) -> TraceScore:
    """Score a settled fresh turn by its first forbidden or decisive call."""
    contract = validate_actions(dict(actions))
    assert contract is not None
    required = contract["required_actions"]
    decisive = contract["decisive_action"]
    preconditions = [action for action in required if action != decisive]
    normalized = canonical_calls(calls)
    for index, call in enumerate(normalized):
        if any(action_matches(call, action) for action in contract["forbidden_actions"]):
            return TraceScore("fail", "forbidden decisive action", index)
        if action_matches(call, decisive):
            seen = normalized[:index]
            if all(any(action_matches(previous, action) for previous in seen) for action in preconditions):
                return TraceScore("pass", "required decisive action", index)
            return TraceScore("fail", "required precondition missing before decisive action", index)
    return TraceScore("fail", "no_decisive_action", None)


def run_three(
    actions: Mapping[str, Any], adapter: TraceAdapter, *, scenario: str = "",
    before_sample: Callable[[], tuple[str | None, str | None]] | None = None,
) -> CarryInRun:
    """Run exactly three serialized fresh turns, or retain pending when not ready."""
    if not adapter.ready():
        return CarryInRun("pending", reason="no_ready_local_model")
    scores: list[TraceScore] = []
    receipt: dict[str, Any] = {}
    for _ in range(SAMPLE_COUNT):
        if before_sample is not None:
            target_hash, reason = before_sample()
            if reason is not None:
                return CarryInRun("pending", tuple(scores), reason=reason)
        else:
            target_hash = None
        calls = adapter.run(scenario=scenario)
        receipt = getattr(adapter, "last_receipt", {})
        if isinstance(receipt, dict) and isinstance(receipt.get("error"), str):
            return CarryInRun("pending", tuple(scores), reason=receipt["error"])
        delivered_blob_sha = (
            receipt.get("delivered_blob_sha", receipt.get("fixture_sha"))
            if isinstance(receipt, dict) else None
        )
        if (
            target_hash is not None and delivered_blob_sha is not None
            and delivered_blob_sha != target_hash
        ):
            return CarryInRun("pending", tuple(scores), reason="fixture_blob_mismatch")
        score = score_trace(actions, calls)
        usage = receipt.get("usage", {}) if isinstance(receipt, dict) else {}
        logger.info(
            "carry-in sample wall_seconds={} input_tokens={} output_tokens={} decisive={} verdict={}",
            receipt.get("wall_seconds") if isinstance(receipt, dict) else None,
            usage.get("prompt_tokens", usage.get("input_tokens")),
            usage.get("completion_tokens", usage.get("output_tokens")),
            score.decisive_call_index,
            score.verdict,
        )
        scores.append(score)
    return CarryInRun(
        "settled", tuple(scores),
        selector=receipt.get("selector"), identity=receipt.get("identity"), target_hash=target_hash,
        fixture_sha=receipt.get("fixture_sha"), delivered_blob_sha=receipt.get("delivered_blob_sha", receipt.get("fixture_sha")),
        neighbour_shas=tuple(receipt.get("neighbour_shas", ())),
    )


def is_discriminating(before: CarryInRun, after: CarryInRun) -> bool | None:
    """Promotion requires a 2-of-3 pre-lesson failure and post-lesson pass."""
    if before.state == "pending" or after.state == "pending":
        return None
    return before.verdict == "fail" and after.verdict == "pass"


def rotated_cases(cases: Sequence[T], *, cap: int | None, edit_number: int) -> list[T]:
    """Select a stable rotating window; an unset cap intentionally runs nothing."""
    if cap is None or cap <= 0 or not cases:
        return []
    size = min(cap, len(cases))
    start = (edit_number * size) % len(cases)
    return [cases[(start + offset) % len(cases)] for offset in range(size)]
