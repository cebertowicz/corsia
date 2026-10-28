"""Focused Claude Code reviewer invocation failure coverage."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from llm_reviewer_fixtures import _reviewer_catalog  # noqa: F401

_BASE_SHA = "b" * 40
_HEAD_SHA = "a" * 40
_CC_SOURCE = {
    "source_git_dir": "/tmp/repo.git",
    "base_sha": _BASE_SHA,
    "head_sha": _HEAD_SHA,
    "env": {"CLAUDE_CONFIG_DIR": "/account-claude-home"},
}


@pytest.fixture(autouse=True)
def _unconfined_launch(monkeypatch):
    """Run the mocked CLI unwrapped: these tests cover envelope handling, and
    write confinement (macOS only) has its own coverage in
    test_reviewer_exact_source.py."""
    monkeypatch.setattr(
        "reviewer.reviewer_backends.write_confined",
        lambda cmd, writable_dirs, config_state=None: cmd,
    )


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    """Build a mocked subprocess result."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


def _envelope(result: str = "[]", subtype: str = "success", is_error: bool = False) -> str:
    """Build a Claude Code stream envelope."""
    return json.dumps({
        "type": "result",
        "subtype": subtype,
        "is_error": is_error,
        "result": result,
        "usage": {"input_tokens": 100, "output_tokens": 10},
        "total_cost_usd": 0.01,
    })


class TestInvokeCCSubagent:

    @patch("reviewer.reviewer_backends.subprocess.run")
    def test_success_returns_result_text(self, mock_run):
        from reviewer.reviewer_backends import invoke_cc_subagent
        mock_run.return_value = _proc(stdout=_envelope(result="[]"))
        assert invoke_cc_subagent(
            "p", "/tmp", model="claude-opus-4-8",
            **_CC_SOURCE,
        ) == "[]"

    @patch("reviewer.reviewer_backends.subprocess.run")
    def test_nonzero_exit_raises(self, mock_run):
        from reviewer.reviewer_backends import invoke_cc_subagent
        mock_run.return_value = _proc(returncode=1, stdout="partial", stderr="boom")
        with pytest.raises(RuntimeError):
            invoke_cc_subagent(
                "p", "/tmp", model="claude-opus-4-8",
                **_CC_SOURCE,
            )

    @patch("reviewer.reviewer_backends.subprocess.run")
    def test_credit_exhaustion_raises_unavailable(self, mock_run):
        from reviewer.reviewer_backends import CCUnavailableError, invoke_cc_subagent
        mock_run.return_value = _proc(
            stdout=_envelope(
                result="Credit balance is too low",
                subtype="error_during_execution",
                is_error=True,
            ),
        )
        with pytest.raises(CCUnavailableError):
            invoke_cc_subagent(
                "p", "/tmp", model="claude-opus-4-8",
                **_CC_SOURCE,
            )

    @patch("reviewer.reviewer_backends.subprocess.run")
    def test_overloaded_stderr_raises_unavailable(self, mock_run):
        from reviewer.reviewer_backends import CCUnavailableError, invoke_cc_subagent
        mock_run.return_value = _proc(
            returncode=1, stderr="API temporarily unavailable (529)",
        )
        with pytest.raises(CCUnavailableError):
            invoke_cc_subagent(
                "p", "/tmp", model="claude-opus-4-8",
                **_CC_SOURCE,
            )

    @patch("reviewer.reviewer_backends.subprocess.run")
    def test_rate_limit_envelope_raises_rate_limit(self, mock_run):
        from reviewer.reviewer_backends import CCRateLimitError, invoke_cc_subagent
        mock_run.return_value = _proc(
            stdout=_envelope(result="rate_limit_error: 429", subtype="error", is_error=True),
        )
        with pytest.raises(CCRateLimitError):
            invoke_cc_subagent(
                "p", "/tmp", model="claude-opus-4-8",
                **_CC_SOURCE,
            )

    @patch("reviewer.reviewer_backends.subprocess.run")
    def test_usage_rate_limit_envelope_raises_unavailable(self, mock_run):
        from reviewer.reviewer_backends import CCUnavailableError, invoke_cc_subagent
        mock_run.return_value = _proc(
            stdout=_envelope(
                result="Claude AI usage rate limit reached for this plan",
                subtype="error",
                is_error=True,
            ),
        )
        with pytest.raises(CCUnavailableError):
            invoke_cc_subagent(
                "p", "/tmp", model="claude-opus-4-8",
                **_CC_SOURCE,
            )

    @patch("reviewer.reviewer_backends.subprocess.run")
    def test_session_limit_envelope_raises_unavailable_before_rate_limit_retry(self, mock_run):
        from reviewer.reviewer_backends import CCUnavailableError, invoke_cc_subagent
        mock_run.return_value = _proc(
            stdout=_envelope(
                result="You've hit your session limit. Resets at 4:20 PM (rate_limit_error: 429)",
                subtype="error",
                is_error=True,
            ),
        )
        with pytest.raises(CCUnavailableError):
            invoke_cc_subagent(
                "p", "/tmp", model="claude-opus-4-8",
                **_CC_SOURCE,
            )

    @patch("reviewer.reviewer_backends.subprocess.run")
    def test_error_subtype_raises(self, mock_run):
        from reviewer.reviewer_backends import invoke_cc_subagent
        mock_run.return_value = _proc(
            stdout=_envelope(result="hit max turns", subtype="error_max_turns"),
        )
        with pytest.raises(RuntimeError):
            invoke_cc_subagent(
                "p", "/tmp", model="claude-opus-4-8",
                **_CC_SOURCE,
            )

    @patch("reviewer.reviewer_backends.subprocess.run")
    def test_non_json_stdout_raises(self, mock_run):
        from reviewer.reviewer_backends import invoke_cc_subagent
        mock_run.return_value = _proc(stdout="plain text, not an envelope")
        with pytest.raises(RuntimeError):
            invoke_cc_subagent(
                "p", "/tmp", model="claude-opus-4-8",
                **_CC_SOURCE,
            )

    @patch("reviewer.reviewer_backends.subprocess.run")
    def test_empty_result_raises(self, mock_run):
        from reviewer.reviewer_backends import invoke_cc_subagent
        mock_run.return_value = _proc(stdout=_envelope(result=""))
        with pytest.raises(RuntimeError):
            invoke_cc_subagent(
                "p", "/tmp", model="claude-opus-4-8",
                **_CC_SOURCE,
            )

    @patch("reviewer.reviewer_backends.subprocess.run")
    def test_429_in_findings_text_does_not_classify(self, mock_run):
        from reviewer.reviewer_backends import invoke_cc_subagent
        findings = json.dumps([{
            "severity": "nit",
            "file": "x.py",
            "line": 1,
            "category": "style/readability",
            "description": "handle HTTP 429 rate_limit responses here",
            "cited_section": None,
        }])
        mock_run.return_value = _proc(stdout=_envelope(result=findings))
        assert invoke_cc_subagent(
            "p", "/tmp", model="claude-opus-4-8",
            **_CC_SOURCE,
        ) == findings

    @patch("reviewer.reviewer_backends.subprocess.run")
    def test_prompt_travels_via_stdin_not_argv(self, mock_run):
        from reviewer.reviewer_backends import invoke_cc_subagent
        mock_run.return_value = _proc(stdout=_envelope(result="[]"))
        huge_prompt = "x" * (2 * 1024 * 1024)  # 2 MiB > any ARG_MAX
        assert invoke_cc_subagent(
            huge_prompt, "/tmp", model="claude-opus-4-8",
            **_CC_SOURCE,
        ) == "[]"
        cmd = mock_run.call_args[0][0]
        assert huge_prompt not in cmd
        assert all(len(arg) < 10_000 for arg in cmd), "no argv element may carry the prompt"
        assert mock_run.call_args.kwargs["input"] == huge_prompt

    @patch("reviewer.reviewer_backends.subprocess.run")
    def test_delisted_model_raises_unavailable(self, mock_run):
        from reviewer.reviewer_backends import CCUnavailableError, invoke_cc_subagent
        mock_run.return_value = _proc(
            returncode=1, stderr="API error: model not found: claude-fable-5",
        )
        with pytest.raises(CCUnavailableError):
            invoke_cc_subagent(
                "p", "/tmp", model="claude-fable-5",
                **_CC_SOURCE,
            )

    @patch("reviewer.reviewer_backends.subprocess.run")
    def test_real_delisting_envelope_raises_unavailable(self, mock_run):
        from reviewer.reviewer_backends import CCUnavailableError, invoke_cc_subagent
        mock_run.return_value = _proc(
            returncode=1,
            stdout=_envelope(
                result="There's an issue with the selected model (claude-fable-5). It may be retired.",
                subtype="success",
                is_error=True,
            ),
        )
        with pytest.raises(CCUnavailableError):
            invoke_cc_subagent(
                "p", "/tmp", model="claude-fable-5",
                **_CC_SOURCE,
            )

    @patch("reviewer.reviewer_backends.subprocess.run")
    def test_max_turns_budget_is_above_the_timeout_bound(self, mock_run):
        """The cap must not be what kills a review.

        PR #1780 (4,298 additions / 69 files) exhausted a budget of 30 in
        ~355 s at ~11.5 s/turn and died with stop_reason=tool_use. The Codex
        backend passes no cap at all, so the Claude budget has to sit above
        whatever the shared 1800 s subprocess timeout can actually reach.
        """
        from reviewer.reviewer_backends import invoke_cc_subagent
        mock_run.return_value = _proc(stdout=_envelope(result="[]"))
        invoke_cc_subagent("p", "/tmp", model="claude-opus-4-8", **_CC_SOURCE)
        cmd = mock_run.call_args[0][0]
        assert cmd[cmd.index("--max-turns") + 1] == "200"

    @patch.dict("os.environ", {"CORSIA_REVIEWER_MAX_TURNS": "42"})
    @patch("reviewer.reviewer_backends.subprocess.run")
    def test_max_turns_env_override(self, mock_run):
        from reviewer.reviewer_backends import invoke_cc_subagent
        mock_run.return_value = _proc(stdout=_envelope(result="[]"))
        invoke_cc_subagent("p", "/tmp", model="claude-opus-4-8", **_CC_SOURCE)
        cmd = mock_run.call_args[0][0]
        assert cmd[cmd.index("--max-turns") + 1] == "42"

    @pytest.mark.parametrize("raw", ["0", "-3", "abc", ""])
    @patch.dict("os.environ", {"CORSIA_REVIEWER_MAX_TURNS": "0"})
    @patch("reviewer.reviewer_backends.subprocess.run")
    def test_invalid_max_turns_env_falls_back_to_default(self, mock_run, raw):
        from reviewer.reviewer_backends import _cc_max_turns
        with patch.dict("os.environ", {"CORSIA_REVIEWER_MAX_TURNS": raw}):
            assert _cc_max_turns() == "200"

    @patch("reviewer.reviewer_backends.subprocess.run")
    def test_max_turns_exhaustion_names_error_class_and_signals(self, mock_run):
        """The 2026-09-15 outage shape: rc=1, stop_reason=tool_use, subtype
        buried past the 200-char detail truncation. The raised error must name
        the class and carry the signals regardless of blob position."""
        from reviewer.reviewer_backends import CCMaxTurnsError, invoke_cc_subagent
        envelope = json.dumps({
            "type": "result",
            "subtype": "error_max_turns",
            "is_error": True,
            "duration_api_ms": 59682,
            "stop_reason": "tool_use",
            "session_id": "c3d5a033-0992-44a4-ad81-0949ada45096",
            "num_turns": 30,
            "total_cost_usd": 0.6500635,
            "usage": {"input_tokens": 20, "cache_creation_input_tokens": 36894},
            "result": "",
        })
        mock_run.return_value = _proc(returncode=1, stdout=envelope)
        with pytest.raises(CCMaxTurnsError) as excinfo:
            invoke_cc_subagent("p", "/tmp", model="opus[1m]", **_CC_SOURCE)
        message = str(excinfo.value)
        assert "subtype=error_max_turns" in message
        assert "stop_reason=tool_use" in message
        assert "num_turns=30" in message

    def test_envelope_signals_skips_missing_keys(self):
        from reviewer.failures import envelope_signals
        assert envelope_signals({"subtype": "error_max_turns"}) == "subtype=error_max_turns"
        assert envelope_signals({}) == ""
