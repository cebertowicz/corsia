"""Synthetic-state helpers for scorer unit tests.

Sprint-3 Slice-2 scorers consume ``state.store["corsia_eval_response"]``
and (for some) ``state.metadata`` / ``state.input``. We don't drive the
real ``/api/eval/turn`` here — that's Slice 1's solver path. These helpers
let each scorer test construct a precise synthetic eval response in pure
Python, exercise both pass and fail paths, and avoid network / process
overhead.

The eval-response shape mirrors ``backend/eval/endpoint.py:EvalTurnResponse``:

    {
        "content": str | None,
        "tool_calls": [ {id, name, arguments}, ... ],
        "iteration_count": int,
        "usage": dict,
        "trace_id": str,
        "span_summary": {"spans": [...], "trace_id": str},
        "model": str,
        "ms_total": float,
        "error": str | None,
        # Sprint-3 augmentation — not exposed yet: the canonical Turn's
        # persisted step rows are its source. Scorers fall back to deriving
        # from the
        # canonical fields above when ``events`` is absent.
        "events": [ {kind, payload, iteration}, ... ],
        "history": [ {role, content, ...}, ... ],
    }
"""
from __future__ import annotations

from typing import Any


class _Store:
    def __init__(self, d: dict) -> None:
        self._d = d

    def get(self, k: str, default: Any = None) -> Any:
        return self._d.get(k, default)

    def set(self, k: str, v: Any) -> None:
        self._d[k] = v


class _Output:
    completion: str = ""


class _StubState:
    """Duck-typed stand-in for ``inspect_ai.solver.TaskState``.

    Scorers only reach for ``state.store``, ``state.metadata``,
    ``state.input``, and ``state.output``, so we ship a minimal struct
    rather than constructing a full TaskState (its Pydantic validation
    is heavy and irrelevant to per-scorer logic).
    """

    def __init__(
        self,
        *,
        response: dict | None = None,
        metadata: dict | None = None,
        user_input: str = "",
    ) -> None:
        self.store = _Store({"corsia_eval_response": response or {}})
        self.metadata = metadata or {}
        self.output = _Output()
        self.input = user_input
        self.input_text = user_input
        self.user_prompt = None


def make_state(
    *,
    events: list[dict] | None = None,
    content: str | None = None,
    tool_calls: list[dict] | None = None,
    iteration_count: int = 0,
    error: str | None = None,
    history: list[dict] | None = None,
    spans: list[dict] | None = None,
    ms_total: float = 0.0,
    trace_id: str = "abc",
    metadata: dict | None = None,
    user_input: str = "",
    turn_attrs: list[dict] | None = None,
) -> _StubState:
    """Build a synthetic ``TaskState`` for a scorer unit test.

    Mode-B scorers (Sprint-4 Slice-6) consume ``state.store['turn_attrs']``
    — pass the list via the ``turn_attrs`` kwarg.
    """
    response: dict[str, Any] = {
        "content": content,
        "tool_calls": tool_calls or [],
        "iteration_count": iteration_count,
        "usage": {},
        "trace_id": trace_id,
        "span_summary": {"spans": spans or [], "trace_id": trace_id},
        "model": "test-model",
        "ms_total": ms_total,
        "error": error,
    }
    if events is not None:
        response["events"] = events
    if history is not None:
        response["history"] = history
    state = _StubState(
        response=response,
        metadata=metadata or {},
        user_input=user_input,
    )
    if turn_attrs is not None:
        state.store.set("turn_attrs", turn_attrs)
    return state


def event(kind: str, *, iteration: int = 0, **payload: Any) -> dict:
    """Compact constructor for synthetic events."""
    return {"kind": kind, "iteration": iteration, "payload": payload}
