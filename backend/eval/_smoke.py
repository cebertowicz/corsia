from __future__ import annotations

import asyncio
import json
import sys
from typing import Any


def _make_state(spans: list[dict[str, Any]], **resp_extras: Any):
    class _Store:
        def __init__(self, d): self._d = d
        def get(self, k, default=None): return self._d.get(k, default)
        def set(self, k, v): self._d[k] = v

    class _Output:
        completion = ""

    class _State:
        def __init__(self):
            resp = {
                "content": resp_extras.get("content", "ok"),
                "tool_calls": resp_extras.get("tool_calls", []),
                "trace_id": resp_extras.get("trace_id", "abc"),
                "span_summary": {"spans": spans, "trace_id": resp_extras.get("trace_id", "abc")},
                "ms_total": resp_extras.get("ms_total", 0.0),
                "usage": resp_extras.get("usage", {}),
            }
            self.store = _Store({"corsia_eval_response": resp})
            self.metadata = resp_extras.get("metadata", {})
            self.output = _Output()
            self.input = resp_extras.get("input", "")
            self.input_text = ""
            self.user_prompt = None
    return _State()



def test_otel_roundtrip() -> None:
    from eval.otel import (
        setup_tracing, get_tracer, current_trace_id_hex,
        SPAN_TURN, SPAN_LLM_CALL, SPAN_TOOL_LOOP, SPAN_TOOL_CALL,
    )

    recorder = setup_tracing()
    tracer = get_tracer("smoke")

    trace_hex: str = ""
    with tracer.start_as_current_span(SPAN_TURN) as turn:
        turn.set_attribute("turn.session_id", "smoke")
        trace_hex = current_trace_id_hex() or ""
        with tracer.start_as_current_span(SPAN_LLM_CALL) as ll:
            ll.set_attribute("llm.iteration", 1)
        with tracer.start_as_current_span(SPAN_TOOL_LOOP) as tl:
            tl.set_attribute("tool_loop.tool_count", 2)
            with tracer.start_as_current_span(SPAN_TOOL_CALL) as tc:
                tc.set_attribute("tool.name", "null_probe")
                tc.set_attribute("tool.is_null_probe", True)

    assert trace_hex, "trace_id_hex must be set inside a span"
    spans = recorder.pop(int(trace_hex, 16))
    names = [s["name"] for s in spans]
    assert SPAN_TURN in names, names
    assert SPAN_TOOL_LOOP in names, names
    assert SPAN_TOOL_CALL in names, names
    print(f"  OK: captured {len(spans)} spans across one trace")



async def test_seam_latency_synthetic() -> None:
    from eval.scorers.seam_latency import seam_latency_scorer
    from inspect_ai.scorer import Target

    NS = 1_000_000  # 1ms in ns
    base = 1_000_000_000  # arbitrary epoch
    synthetic_spans = [
        {
            "name": "corsia.tool_loop",
            "start_ns": base,
            "end_ns": base + 200 * NS,
            "attrs": {"tool_loop.tool_count": 2},
        },
        {
            "name": "corsia.tool_call",
            "start_ns": base + 10 * NS,
            "end_ns": base + 30 * NS,  # 20ms
            "attrs": {"tool.name": "null_probe", "tool.is_null_probe": True},
        },
        {
            "name": "corsia.tool_call",
            "start_ns": base + 50 * NS,
            "end_ns": base + 80 * NS,  # 30ms
            "attrs": {"tool.name": "null_probe", "tool.is_null_probe": True},
        },
    ]

    state = _make_state(synthetic_spans, ms_total=200.0, trace_id="synth")
    target = Target("")

    scorer_strict = seam_latency_scorer(budget_ms=100.0)
    score_fail = await scorer_strict(state, target)
    assert score_fail.metadata["worst_seam_ms"] == 150.0, score_fail.metadata
    assert str(score_fail.value) in ("INCORRECT", "I"), f"expected fail value, got {score_fail.value}"
    print(f"  OK: budget=100ms / actual seam=150ms → INCORRECT ({score_fail.explanation})")

    scorer_loose = seam_latency_scorer(budget_ms=200.0)
    score_pass = await scorer_loose(state, target)
    assert score_pass.metadata["worst_seam_ms"] == 150.0
    assert str(score_pass.value) in ("CORRECT", "C")
    print(f"  OK: budget=200ms / actual seam=150ms → CORRECT ({score_pass.explanation})")

    state_empty = _make_state([
        {"name": "corsia.turn", "start_ns": base, "end_ns": base + 100 * NS, "attrs": {}},
    ])
    score_vacuous = await scorer_strict(state_empty, target)
    assert score_vacuous.metadata["loop_count"] == 0
    print(f"  OK: no tool_loop spans → vacuous pass ({score_vacuous.explanation})")



async def test_null_tool_paired() -> None:
    from eval.scorers.null_tool import null_tool_paired_scorer, compute_pair_deltas
    from inspect_ai.scorer import Target

    target = Target("")
    scorer_fn = null_tool_paired_scorer()

    state_real = _make_state(
        spans=[],
        ms_total=120.0,
        metadata={"pair_id": "P1", "mode": "seam_paired_real"},
    )
    state_null = _make_state(
        spans=[],
        ms_total=80.0,
        metadata={"pair_id": "P1", "mode": "seam_paired_null"},
    )

    s_real = await scorer_fn(state_real, target)
    s_null = await scorer_fn(state_null, target)

    assert float(s_real.value) == 120.0, s_real.value
    assert float(s_null.value) == 80.0, s_null.value
    print(f"  OK: paired-real ms={s_real.value}, paired-null ms={s_null.value}")

    deltas = compute_pair_deltas([
        {"metadata": s_real.metadata},
        {"metadata": s_null.metadata},
    ])
    assert deltas == {"P1": 40.0}, deltas
    print(f"  OK: pair_id=P1 delta={deltas['P1']}ms")



async def test_structured_validity() -> None:
    from eval.scorers.structured_validity import structured_validity_scorer
    from inspect_ai.scorer import Target

    target = Target("")

    s_ok = _make_state(spans=[], content='{"answer": "blue", "confidence": 1.0}')
    score = await structured_validity_scorer(mode="json")(s_ok, target)
    assert str(score.value) in ("CORRECT", "C"), score
    print("  OK: valid JSON → CORRECT")

    s_fenced = _make_state(spans=[], content='```json\n{"x": 1}\n```')
    score = await structured_validity_scorer(mode="json")(s_fenced, target)
    assert str(score.value) in ("CORRECT", "C")
    print("  OK: fenced JSON → CORRECT")

    s_bad = _make_state(spans=[], content="not json at all")
    score = await structured_validity_scorer(mode="json")(s_bad, target)
    assert str(score.value) in ("INCORRECT", "I")
    print("  OK: garbage → INCORRECT")



async def test_null_probe_tool() -> None:
    from eval.null_tool import NullProbeTool

    t = NullProbeTool()
    result = await t.execute(probe_id="abc")
    parsed = json.loads(result)
    assert parsed == {"ok": True, "probe_id_echo": "abc"}, parsed
    print(f"  OK: null_probe(probe_id='abc') -> {parsed}")



async def _run_async() -> int:
    failures = 0
    print("Test 1: OTel round-trip")
    try:
        test_otel_roundtrip()
    except Exception as e:
        failures += 1
        print(f"  FAIL: {e!r}")

    print("Test 2: seam-latency scorer (synthetic)")
    try:
        await test_seam_latency_synthetic()
    except Exception as e:
        failures += 1
        print(f"  FAIL: {e!r}")

    print("Test 3: null-tool paired delta")
    try:
        await test_null_tool_paired()
    except Exception as e:
        failures += 1
        print(f"  FAIL: {e!r}")

    print("Test 4: structured validity")
    try:
        await test_structured_validity()
    except Exception as e:
        failures += 1
        print(f"  FAIL: {e!r}")

    print("Test 5: null_probe tool")
    try:
        await test_null_probe_tool()
    except Exception as e:
        failures += 1
        print(f"  FAIL: {e!r}")

    print()
    print(f"Smoke: {5 - failures}/5 passed.")
    return 0 if failures == 0 else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(asyncio.run(_run_async()))
