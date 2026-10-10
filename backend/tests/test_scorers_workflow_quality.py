
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from _scorer_helpers import make_state  # noqa: E402



class TestParseJudgeResponse:

    def test_parses_clean_json(self):
        from eval.scorers.registry.workflow_quality import _parse_judge_response
        score, reason = _parse_judge_response('{"score": 0.85, "reason": "good"}')
        assert score == 0.85
        assert reason == "good"

    def test_parses_json_with_leading_prose(self):
        from eval.scorers.registry.workflow_quality import _parse_judge_response
        raw = 'Here is my evaluation:\n{"score": 0.5, "reason": "ok"}'
        score, reason = _parse_judge_response(raw)
        assert score == 0.5
        assert reason == "ok"

    def test_rejects_out_of_range_score(self):
        from eval.scorers.registry.workflow_quality import _parse_judge_response
        score, reason = _parse_judge_response('{"score": 1.5, "reason": "x"}')
        assert score is None
        assert "out of range" in reason

    def test_rejects_negative_score(self):
        from eval.scorers.registry.workflow_quality import _parse_judge_response
        score, reason = _parse_judge_response('{"score": -0.1, "reason": "x"}')
        assert score is None

    def test_rejects_non_numeric_score(self):
        from eval.scorers.registry.workflow_quality import _parse_judge_response
        score, reason = _parse_judge_response('{"score": true, "reason": "x"}')
        assert score is None
        score2, _ = _parse_judge_response('{"score": "high", "reason": "x"}')
        assert score2 is None

    def test_rejects_empty_response(self):
        from eval.scorers.registry.workflow_quality import _parse_judge_response
        score, reason = _parse_judge_response("")
        assert score is None
        assert "empty" in reason

    def test_rejects_garbage(self):
        from eval.scorers.registry.workflow_quality import _parse_judge_response
        score, reason = _parse_judge_response("the model decided to free-form prose")
        assert score is None

    def test_handles_missing_reason(self):
        from eval.scorers.registry.workflow_quality import _parse_judge_response
        score, reason = _parse_judge_response('{"score": 0.4}')
        assert score == 0.4
        assert reason == ""



class TestSerializeTranscript:

    def test_serializes_history(self):
        from eval.scorers.registry.workflow_quality import _serialize_transcript
        resp = {
            "history": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi there"},
            ],
        }
        txt = _serialize_transcript(resp, [])
        assert "user: hello" in txt
        assert "assistant: hi there" in txt

    def test_falls_back_to_content_when_history_missing(self):
        from eval.scorers.registry.workflow_quality import _serialize_transcript
        resp = {"content": "the final answer"}
        txt = _serialize_transcript(resp, [])
        assert "assistant_final: the final answer" in txt

    def test_includes_endpoint_error(self):
        from eval.scorers.registry.workflow_quality import _serialize_transcript
        resp = {"content": None, "error": "rate limited"}
        txt = _serialize_transcript(resp, [])
        assert "ENDPOINT_ERROR" in txt
        assert "rate limited" in txt

    def test_appends_per_turn_attrs(self):
        from eval.scorers.registry.workflow_quality import _serialize_transcript
        resp = {"content": "ok"}
        turns = [
            {"turn_index": 0, "cached_tokens": 0, "prefill_new_tokens": 100},
            {"turn_index": 1, "cached_tokens": 90, "prefill_new_tokens": 10},
        ]
        txt = _serialize_transcript(resp, turns)
        assert "PER_TURN_ATTRS" in txt
        assert '"cached_tokens": 90' in txt

    def test_handles_empty_response(self):
        from eval.scorers.registry.workflow_quality import _serialize_transcript
        txt = _serialize_transcript({}, [])
        assert txt == "(empty transcript)"



@pytest.mark.asyncio
class TestWorkflowQualityScorer:

    async def test_pass_path(self):
        from eval.scorers.registry.workflow_quality import workflow_quality

        async def fake_judge(model_id, prompt):
            assert "RUBRIC" in prompt and "TRANSCRIPT" in prompt
            return '{"score": 0.85, "reason": "all criteria met"}'

        scorer_cb = workflow_quality(
            rubric="Be helpful.", threshold=0.7, _judge_fn=fake_judge,
        )
        state = make_state(content="hello world")
        target = object()
        score = await scorer_cb(state, target)
        assert score.value == "C"
        assert score.metadata["score"] == 0.85
        assert score.metadata["threshold"] == 0.7

    async def test_fail_path_below_threshold(self):
        from eval.scorers.registry.workflow_quality import workflow_quality

        async def fake_judge(model_id, prompt):
            return '{"score": 0.5, "reason": "missed criterion 2"}'

        scorer_cb = workflow_quality(
            rubric="Be helpful.", threshold=0.7, _judge_fn=fake_judge,
        )
        state = make_state(content="meh")
        target = object()
        score = await scorer_cb(state, target)
        assert score.value == "I"
        assert score.metadata["score"] == 0.5
        assert "missed criterion 2" in score.explanation

    async def test_parse_error_returns_incorrect_loudly(self):
        from eval.scorers.registry.workflow_quality import workflow_quality

        async def fake_judge(model_id, prompt):
            return "I cannot grade this."

        scorer_cb = workflow_quality(
            rubric="Be helpful.", threshold=0.7, _judge_fn=fake_judge,
        )
        state = make_state(content="hi")
        score = await scorer_cb(state, object())
        assert score.value == "I"
        assert score.metadata["score"] is None
        assert score.answer == "parse_error"

    async def test_judge_exception_returns_incorrect(self):
        from eval.scorers.registry.workflow_quality import workflow_quality

        async def fake_judge(model_id, prompt):
            raise RuntimeError("model_unreachable")

        scorer_cb = workflow_quality(
            rubric="Be helpful.", threshold=0.7, _judge_fn=fake_judge,
        )
        state = make_state(content="hi")
        score = await scorer_cb(state, object())
        assert score.value == "I"
        assert score.answer == "judge_error"
        assert "model_unreachable" in score.explanation

    async def test_rubric_required(self):
        from eval.scorers.registry.workflow_quality import workflow_quality
        with pytest.raises(ValueError, match="rubric is required"):
            workflow_quality(rubric="")
        with pytest.raises(ValueError, match="rubric is required"):
            workflow_quality(rubric="   ")

    async def test_threshold_validated(self):
        from eval.scorers.registry.workflow_quality import workflow_quality
        with pytest.raises(ValueError, match="threshold"):
            workflow_quality(rubric="x", threshold=1.5)
        with pytest.raises(ValueError, match="threshold"):
            workflow_quality(rubric="x", threshold=-0.1)

    async def test_turn_attrs_included_in_transcript(self):
        from eval.scorers.registry.workflow_quality import workflow_quality

        seen_prompt: dict[str, str] = {}

        async def fake_judge(model_id, prompt):
            seen_prompt["p"] = prompt
            return '{"score": 0.9, "reason": "ok"}'

        scorer_cb = workflow_quality(
            rubric="Score it.", threshold=0.7, _judge_fn=fake_judge,
        )
        state = make_state(
            content="hi",
            turn_attrs=[
                {"turn_index": 0, "cached_tokens": 0, "prefill_new_tokens": 100},
                {"turn_index": 1, "cached_tokens": 95, "prefill_new_tokens": 5},
            ],
        )
        await scorer_cb(state, object())
        assert "PER_TURN_ATTRS" in seen_prompt["p"]
        assert "cached_tokens" in seen_prompt["p"]

    async def test_default_judge_model_via_env(self, monkeypatch):
        from eval.scorers.registry.workflow_quality import workflow_quality

        seen_model: dict[str, str] = {}

        async def fake_judge(model_id, prompt):
            seen_model["m"] = model_id
            return '{"score": 0.8, "reason": "ok"}'

        scorer_cb = workflow_quality(
            rubric="r", model="oMLX/test-judge", _judge_fn=fake_judge,
        )
        await scorer_cb(make_state(content="hi"), object())
        assert seen_model["m"] == "oMLX/test-judge"

    async def test_metadata_score_preserved_for_gate(self):
        from eval.scorers.registry.workflow_quality import workflow_quality

        async def fake_judge(model_id, prompt):
            return '{"score": 0.42, "reason": "ok"}'

        scorer_cb = workflow_quality(
            rubric="r", threshold=0.3, _judge_fn=fake_judge,
        )
        score = await scorer_cb(make_state(content="hi"), object())
        assert score.value == "C"
        assert score.metadata["score"] == 0.42



def test_workflow_quality_registered_in_resolve():
    from eval.scorers.resolve import SCORER_REGISTRY
    assert "workflow_quality" in SCORER_REGISTRY
    factory = SCORER_REGISTRY["workflow_quality"]
    assert callable(factory)
