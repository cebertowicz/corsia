
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
_CODEX_SOURCE = {
    "source_git_dir": "/tmp/repo.git",
    "env": {"CODEX_HOME": "/account-codex-home"},
}


@pytest.fixture(autouse=True)
def _reviewer_model_env(monkeypatch):
    monkeypatch.setenv("CORSIA_REVIEWER_MODEL", "claude-opus-4-8")
    monkeypatch.setenv("CORSIA_REVIEWER_FALLBACK_MODEL", "claude-sonnet-4-6")


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


def _envelope(result: str = "[]", subtype: str = "success", is_error: bool = False) -> str:
    return json.dumps({
        "type": "result",
        "subtype": subtype,
        "is_error": is_error,
        "result": result,
        "usage": {"input_tokens": 100, "output_tokens": 10},
        "total_cost_usd": 0.01,
    })


class TestReviewerGithubTransport:
    @patch("reviewer.gh_api.subprocess.run")
    def test_status_preserves_stdin_payload_and_result(self, mock_run, caplog):
        from reviewer.reviewer_categories import STATUS_CHECK_CONTEXT
        from reviewer.reviewer_status import set_status

        mock_run.return_value = _proc(stdout='{"id": 1}')
        assert set_status(
            "owner/repo", "abc123def", "success", "x" * 200, "https://example.test/review",
        ) is True
        cmd = mock_run.call_args.args[0]
        kwargs = mock_run.call_args.kwargs
        assert cmd == ["gh", "api", "repos/owner/repo/statuses/abc123def", "--method", "POST", "--input", "-"]
        payload = json.loads(kwargs["input"])
        assert payload["state"] == "success"
        assert payload["context"] == STATUS_CHECK_CONTEXT
        assert payload["target_url"] == "https://example.test/review"
        assert payload["description"].startswith("x")
        assert payload["description"].endswith("]")
        assert len(payload["description"]) == 140
        assert kwargs["timeout"] == 30
        mock_run.return_value = _proc(returncode=2, stderr="denied")
        with caplog.at_level("ERROR"):
            assert set_status("owner/repo", "abc", "error", "failed") is False
        assert "failed status write" in caplog.text
        assert "repo=owner/repo" in caplog.text
        assert "sha=abc" in caplog.text
        assert "state=error" in caplog.text
        assert "write_id=" in caplog.text
        assert "rc=2: denied" in caplog.text

    @patch("reviewer.gh_api.subprocess.run")
    def test_all_setters_truncate_description_to_140(self, mock_run):
        """Every public setter hands the transport GitHub's stored 140-char
        form — the guarded setters need it for dedupe equality, and no setter
        may rely on a longer description surviving."""
        from reviewer import reviewer_status

        posted: list[dict] = []

        def _serve(cmd, **kwargs):
            if "--method" in cmd and "POST" in cmd:
                posted.append(json.loads(kwargs["input"]))
                return _proc(stdout='{"id": 1}')
            return _proc(stdout="")  # combined-status read: no standing status

        mock_run.side_effect = _serve
        long_desc = "x" * 200
        assert reviewer_status.set_pending("owner/repo", "abc123def", long_desc)
        assert reviewer_status.set_success("owner/repo", "abc123def", long_desc)
        assert reviewer_status.set_failure("owner/repo", "abc123def", long_desc)
        assert reviewer_status.set_error("owner/repo", "abc123def", long_desc)

        assert [p["state"] for p in posted] == [
            "pending", "success", "failure", "error",
        ]
        assert all(len(p["description"]) == 140 for p in posted)
        assert all("[m:" in p["description"] and " w:" in p["description"] for p in posted)


class TestInvokeCodexReviewer:

    @patch("reviewer.reviewer_backends.subprocess.run")
    def test_success_returns_output_last_message(self, mock_run, monkeypatch):
        from reviewer.reviewer_backends import invoke_codex_reviewer

        def _run(cmd, **kwargs):
            output_path = cmd[cmd.index("--output-last-message") + 1]
            Path(output_path).write_text("[]")
            proc = _proc(stdout='{"type":"turn.completed"}\n')
            proc.cmd = cmd
            proc.kwargs = kwargs
            return proc

        mock_run.side_effect = _run
        prompt = "review this diff"
        assert invoke_codex_reviewer(
            prompt, "/tmp", model="codex-auto-review", **_CODEX_SOURCE,
        ) == "[]"

        cmd = mock_run.call_args[0][0]
        kwargs = mock_run.call_args.kwargs
        assert kwargs["input"] == prompt
        assert prompt not in cmd
        assert cmd[-2:] == ["--", "-"]
        assert cmd[cmd.index("--model") + 1] == "codex-auto-review"
        # The admitted account's environment is handed to the CLI verbatim.
        assert kwargs["env"] == _CODEX_SOURCE["env"]
        # Parity invariant: the Codex reviewer is bounded only by the shared
        # subprocess timeout, never by a turn count. The Claude path must not
        # carry a cap so low that it becomes the thing that kills a review.
        assert "--max-turns" not in cmd

    @patch("reviewer.reviewer_backends.subprocess.run")
    def test_reviewer_never_gets_a_write_path(self, mock_run):
        """The reviewer must not be able to mutate anything it can reach.

        ``codex exec`` runs model-generated shell commands. Reviewing needs
        narrow reads, never writes, so the reviewer selects a strict permission
        profile with only its neutral runtime directory and Git object store.
        """
        from reviewer.reviewer_backends import invoke_codex_reviewer

        def _run(cmd, **kwargs):
            Path(cmd[cmd.index("--output-last-message") + 1]).write_text("[]")
            return _proc(stdout='{"type":"turn.completed"}\n')

        mock_run.side_effect = _run
        invoke_codex_reviewer(
            "p", "/tmp", model="codex-auto-review", **_CODEX_SOURCE,
        )

        cmd = mock_run.call_args[0][0]
        assert cmd.count("--ignore-user-config") == 1
        assert cmd.count("--ignore-rules") == 1
        assert cmd.count("--strict-config") == 1
        assert 'default_permissions="corsia_reviewer"' in cmd
        assert 'approval_policy="never"' in cmd
        filesystem = next(
            value for value in cmd
            if value.startswith("permissions.corsia_reviewer.filesystem=")
        )
        assert f'"{Path("/tmp").resolve()}"="read"' in filesystem
        assert f'"{Path("/tmp/repo.git").resolve()}"="read"' in filesystem
        assert "permissions.corsia_reviewer.network.enabled=false" in cmd
        assert "--sandbox" not in cmd
        assert "--dangerously-bypass-approvals-and-sandbox" not in cmd
        assert "workspace-write" not in cmd
        assert "danger-full-access" not in cmd

    @patch("reviewer.reviewer_backends.subprocess.run")
    def test_weekly_limit_fails_unavailable(self, mock_run):
        from reviewer.reviewer_backends import CCUnavailableError, invoke_codex_reviewer

        mock_run.return_value = _proc(
            returncode=1,
            stderr="Codex weekly limit reached for this plan",
        )
        with pytest.raises(CCUnavailableError):
            invoke_codex_reviewer(
                "p", "/tmp", model="codex-auto-review", **_CODEX_SOURCE,
            )

    @patch("reviewer.reviewer_backends.subprocess.run")
    def test_out_of_credits_fails_unavailable(self, mock_run):
        from reviewer.reviewer_backends import CCUnavailableError, invoke_codex_reviewer

        mock_run.return_value = _proc(
            returncode=1,
            stderr="Codex workspace is out of credits",
        )
        with pytest.raises(CCUnavailableError):
            invoke_codex_reviewer(
                "p", "/tmp", model="codex-auto-review", **_CODEX_SOURCE,
            )

    @patch("reviewer.reviewer_backends.subprocess.run")
    def test_usage_rate_limit_event_fails_unavailable(self, mock_run):
        from reviewer.reviewer_backends import CCUnavailableError, invoke_codex_reviewer

        def _run(cmd, **kwargs):
            output_path = cmd[cmd.index("--output-last-message") + 1]
            Path(output_path).write_text("[]")
            return _proc(stdout=json.dumps({
                "type": "turn.failed",
                "error": {"message": "Codex usage rate limit reached for this plan"},
            }) + "\n")

        mock_run.side_effect = _run
        with pytest.raises(CCUnavailableError, match="turn.failed"):
            invoke_codex_reviewer(
                "p", "/tmp", model="codex-auto-review", **_CODEX_SOURCE,
            )

    @patch("reviewer.reviewer_backends.subprocess.run")
    def test_turn_failed_event_fails_even_with_last_message(self, mock_run):
        from reviewer.reviewer_backends import CCUnavailableError, invoke_codex_reviewer

        def _run(cmd, **kwargs):
            output_path = cmd[cmd.index("--output-last-message") + 1]
            Path(output_path).write_text("[]")
            return _proc(stdout=json.dumps({
                "type": "turn.failed",
                "error": {
                    "code": "quota_exceeded",
                    "message": "Codex usage limit reached",
                },
            }) + "\n")

        mock_run.side_effect = _run
        with pytest.raises(CCUnavailableError, match="turn.failed"):
            invoke_codex_reviewer(
                "p", "/tmp", model="codex-auto-review", **_CODEX_SOURCE,
            )

    @patch("reviewer.reviewer_backends.subprocess.run")
    def test_rate_limit_event_is_retryable(self, mock_run):
        from reviewer.reviewer_backends import CCRateLimitError, invoke_codex_reviewer

        def _run(cmd, **kwargs):
            output_path = cmd[cmd.index("--output-last-message") + 1]
            Path(output_path).write_text("[]")
            return _proc(stdout=json.dumps({
                "type": "turn.failed",
                "error": {"message": "Rate limit reached"},
                "account": {
                    "rateLimits": {"rateLimitReachedType": "window"},
                },
            }) + "\n")

        mock_run.side_effect = _run
        with pytest.raises(CCRateLimitError, match="turn.failed"):
            invoke_codex_reviewer(
                "p", "/tmp", model="codex-auto-review", **_CODEX_SOURCE,
            )



class TestModelSelection:

    def test_primary_from_env(self):
        from reviewer.reviewer_runner import _cc_model_for_base
        model, label = _cc_model_for_base("dev")
        assert model == "claude-opus-4-8"
        assert label == "claude-opus-4-8"

    def test_primary_main_gets_1m_variant(self):
        from reviewer.reviewer_runner import _cc_model_for_base
        model, label = _cc_model_for_base("main")
        assert model == "claude-opus-4-8[1m]"
        assert "1M" in label

    def test_primary_unconfigured_raises(self, monkeypatch):
        monkeypatch.delenv("CORSIA_REVIEWER_MODEL", raising=False)
        monkeypatch.setattr(
            "reviewer.reviewer_runner.get_agent_defaults", lambda: {},
        )
        from reviewer.reviewer_runner import _cc_model_for_base
        with pytest.raises(RuntimeError, match="No reviewer model configured"):
            _cc_model_for_base("dev")

    def test_fallback_from_env(self):
        from reviewer.reviewer_runner import _cc_fallback_for_base
        model, label = _cc_fallback_for_base("dev")
        assert model == "claude-sonnet-4-6"
        assert "fallback" in label.lower()

    def test_fallback_main_gets_1m_variant(self):
        from reviewer.reviewer_runner import _cc_fallback_for_base
        model, label = _cc_fallback_for_base("main")
        assert model == "claude-sonnet-4-6[1m]"
        assert "1M" in label

    def test_no_fallback_configured_returns_none(self, monkeypatch):
        monkeypatch.delenv("CORSIA_REVIEWER_FALLBACK_MODEL", raising=False)
        monkeypatch.setattr(
            "reviewer.reviewer_runner.get_agent_defaults", lambda: {},
        )
        from reviewer.reviewer_runner import _cc_fallback_for_base
        assert _cc_fallback_for_base("dev") is None

    def test_explicit_large_alias_not_double_suffixed(self, monkeypatch):
        monkeypatch.setenv("CORSIA_REVIEWER_MODEL", "claude-opus-4-8[1m]")
        from reviewer.reviewer_runner import _cc_model_for_base
        model, _ = _cc_model_for_base("main")
        assert model == "claude-opus-4-8[1m]"

    def test_codex_model_not_cc_suffixed_for_main(self, monkeypatch):
        monkeypatch.setenv("CORSIA_REVIEWER_MODEL", "codex/codex-auto-review")
        from reviewer.reviewer_runner import _cc_model_for_base
        model, label = _cc_model_for_base("main")
        assert model == "codex/codex-auto-review"
        assert label == "codex/codex-auto-review"


class TestResolveCCBinary:
    """The CC reviewer must not depend on ``claude`` being on the backend
    process PATH: resolve it the way cc_runtime does (env override -> the
    configured claude-code connection binary -> bare ``claude``). A bare-only
    default made the reviewer die with FileNotFoundError when ``claude`` lived
    in ``~/.local/bin`` (off the backend PATH)."""

    def test_env_override_wins(self, monkeypatch):
        from reviewer.reviewer_backends import _resolve_cc_binary
        monkeypatch.setenv("CLAUDE_BIN", "/custom/path/claude")
        assert _resolve_cc_binary() == "/custom/path/claude"

    def test_falls_back_to_connection_binary(self, monkeypatch):
        from reviewer import reviewer_backends as rb
        monkeypatch.delenv("CLAUDE_BIN", raising=False)
        monkeypatch.setattr(
            "connections.models.get_connection",
            lambda *a, **k: {"config": {"binary": "/Users/x/.local/bin/claude"}},
        )
        assert rb._resolve_cc_binary() == "/Users/x/.local/bin/claude"

    def test_defaults_to_bare_claude_when_no_config(self, monkeypatch):
        from reviewer import reviewer_backends as rb
        monkeypatch.delenv("CLAUDE_BIN", raising=False)
        monkeypatch.setattr(
            "connections.models.get_connection", lambda *a, **k: None,
        )
        assert rb._resolve_cc_binary() == "claude"


class TestCodexJsonlFraming:
    """``--json`` is LF-delimited JSONL, so a line boundary is string content.

    ``codex exec --help`` states ``--json`` prints events as JSONL: one JSON value per
    line, delimited by LF. JSON leaves U+2028, U+2029 and U+0085 raw inside a string,
    and ``str.splitlines()`` breaks on all three, so it is the wrong frame: one
    well-formed event arrives as two halves that neither parse.
    """

    _COMMAND = "/bin/zsh -c 'git --git-dir=/r/l/objects log -1 --format=%H %s'"

    @classmethod
    def _event_with(cls, sep: str) -> str:
        cut = len(cls._COMMAND) // 2
        item = {"id": "item_1", "type": "command_execution",
                "command": cls._COMMAND[:cut] + sep + cls._COMMAND[cut:]}
        return json.dumps({"type": "item.completed", "item": item}, ensure_ascii=False)

    @pytest.mark.parametrize("sep", ["\u2028", "\u2029", "\x85"])
    def test_line_boundary_inside_a_string_is_not_an_event_break(self, sep):
        from reviewer.reviewer_backends import _codex_error_from_jsonl

        line = self._event_with(sep)
        assert sep in line, "the serializer is allowed to leave this boundary raw"
        assert json.loads(line)["item"]["command"], "the whole line is one valid event"
        assert _codex_error_from_jsonl(line + "\n") is None

    def test_failure_events_are_still_found_after_a_separator_bearing_event(self):
        """The frame must not stop the scan, or a real failure would read as success."""
        from reviewer.reviewer_backends import CCRateLimitError, _codex_error_from_jsonl

        failure = json.dumps({
            "type": "turn.failed",
            "error": {"message": "Rate limit reached"},
            "account": {"rateLimits": {"rateLimitReachedType": "window"}},
        })
        assert isinstance(_codex_error_from_jsonl(
            self._event_with("\u2028") + "\n" + failure + "\n"), CCRateLimitError)

    @pytest.mark.parametrize("sep", ["\u2028", "\u2029", "\x85"])
    def test_review_gate_passes_on_a_real_command_event(self, sep):
        """End to end: the same stdout must let the reviewer return its result."""
        from reviewer.reviewer_backends import invoke_codex_reviewer

        def _run(cmd, **kwargs):
            Path(cmd[cmd.index("--output-last-message") + 1]).write_text("[]")
            return _proc(stdout=self._event_with(sep) + '\n{"type":"turn.completed"}\n')

        with patch("reviewer.reviewer_backends.subprocess.run", side_effect=_run):
            assert invoke_codex_reviewer(
                "p", "/tmp", model="codex-auto-review", **_CODEX_SOURCE) == "[]"

    def test_blank_lines_and_crlf_stay_accepted(self):
        from reviewer.reviewer_backends import _codex_error_from_jsonl

        assert _codex_error_from_jsonl("") is None
        assert _codex_error_from_jsonl("\n\r\n  \n") is None
        assert _codex_error_from_jsonl(
            json.dumps({"type": "turn.completed"}) + "\r\n\r\n") is None

    def test_truncated_and_non_object_events_are_still_rejected(self):
        from reviewer.reviewer_backends import _codex_error_from_jsonl

        truncated = _codex_error_from_jsonl('{"type":"item.compl')
        assert isinstance(truncated, RuntimeError) and "non-JSON" in str(truncated)
        non_object = _codex_error_from_jsonl("[1, 2]\n")
        assert isinstance(non_object, RuntimeError) and "non-object" in str(non_object)

    def test_usage_limit_event_stays_unavailable(self):
        from reviewer.reviewer_backends import CCUnavailableError, _codex_error_from_jsonl

        unavailable = _codex_error_from_jsonl(json.dumps({
            "type": "turn.failed",
            "error": {"message": "Codex weekly limit reached for this plan"},
        }) + "\n")
        assert isinstance(unavailable, CCUnavailableError)
