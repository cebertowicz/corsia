from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from llm_reviewer_fixtures import _reviewer_catalog  # noqa: F401
from reviewer.failures import CCUnavailableError, raise_cli_failure
from reviewer.reviewer_runner import run_cc_review


def envelope(**overrides):
    value = {
        "is_error": True, "subtype": "success", "duration_api_ms": 0,
        "total_cost_usd": 0, "usage": {"input_tokens": 0},
        "padding": "x" * 250, "result": "specific CLI refusal",
    }
    value.update(overrides)
    return json.dumps(value)


@pytest.mark.parametrize("returncode", [0, 1])
def test_no_call_envelope_is_unavailable_and_contains_result(returncode):
    from reviewer.reviewer_backends import invoke_cc_subagent

    proc = MagicMock(returncode=returncode, stdout=envelope(), stderr="")
    with patch("reviewer.reviewer_backends._run_write_confined", return_value=proc):
        with pytest.raises(CCUnavailableError, match="specific CLI refusal") as exc:
            invoke_cc_subagent("p", "/tmp", model="opus", source_git_dir="/tmp/repo.git",
                               base_sha="b" * 40, head_sha="a" * 40, env={})
    assert str(exc.value).index("specific CLI refusal") < str(exc.value).index("subtype=")


def test_envelope_detail_keeps_raw_output_and_stderr():
    with pytest.raises(CCUnavailableError) as exc:
        raise_cli_failure(1, envelope(), "diagnostic")
    assert '"is_error"' in str(exc.value)
    assert str(exc.value).index("specific CLI refusal") < str(exc.value).index('"is_error"')
    assert "stderr: diagnostic" in str(exc.value)


@pytest.mark.parametrize("usage", [None, {}])
def test_no_call_with_absent_input_tokens_is_unavailable(usage):
    with pytest.raises(CCUnavailableError):
        raise_cli_failure(1, envelope(usage=usage), "")


@pytest.mark.parametrize("change", [
    {"duration_api_ms": 1}, {"total_cost_usd": 0.01}, {"usage": {"input_tokens": 1}},
])
def test_model_ran_is_runtime_error_and_does_not_fall_back(monkeypatch, change):
    monkeypatch.setenv("CORSIA_REVIEWER_MODEL", "claude-opus-4-8")
    monkeypatch.setenv("CORSIA_REVIEWER_FALLBACK_MODEL", "claude-sonnet-4-6")
    def attempt_failure(*args, **kwargs):
        raise_cli_failure(1, envelope(**change), "")

    with patch("reviewer.reviewer_runner._attempt", side_effect=attempt_failure) as attempt, \
         patch("reviewer.reviewer_runner.set_pending"), \
         patch("reviewer.reviewer_runner.set_failure"), \
         patch("reviewer.reviewer_runner.emit_product_event"):
        with pytest.raises(RuntimeError, match="specific CLI refusal"):
            run_cc_review("d", "", 1, "o/r", "abc", "/tmp", "dev")
    assert attempt.call_count == 1


def test_no_call_falls_back(monkeypatch):
    monkeypatch.setenv("CORSIA_REVIEWER_MODEL", "claude-opus-4-8")
    monkeypatch.setenv("CORSIA_REVIEWER_FALLBACK_MODEL", "claude-sonnet-4-6")

    def attempt(*args, **kwargs):
        if args[6] == "claude-opus-4-8":
            raise_cli_failure(1, envelope(), "")
        return "[]"

    with patch("reviewer.reviewer_runner._attempt", side_effect=attempt) as calls, \
         patch("reviewer.reviewer_runner.set_pending"), \
         patch("reviewer.reviewer_runner.emit_product_event"):
        result = run_cc_review("d", "", 1, "o/r", "abc", "/tmp", "dev")
    assert result == ("[]", "claude-sonnet-4-6 (fallback)")
    assert [call.args[6] for call in calls.call_args_list] == ["claude-opus-4-8", "claude-sonnet-4-6"]
