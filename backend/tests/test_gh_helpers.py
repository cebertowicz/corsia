"""Unit tests for reviewer.gh_helpers response parsing."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

from reviewer.gh_helpers import get_check_runs_for_sha, get_commit_status_for_context


class TestGhTransport:
    def test_run_gh_prepends_executable_and_forwards_process_options(self):
        from reviewer import gh_api

        completed = subprocess.CompletedProcess([], 0, "ok", "")
        with patch("reviewer.gh_api.subprocess.run", return_value=completed) as run:
            assert gh_api.run_gh(
                ["api", "rate_limit"], input_text="{}", timeout=12,
            ) is completed
        assert run.call_args.args[0] == ["gh", "api", "rate_limit"]
        assert run.call_args.kwargs == {
            "input": "{}", "capture_output": True, "text": True,
            "timeout": 12, "check": False,
        }


class TestGetCheckRunsForSha:
    def test_conclusion_null_falls_back_to_status(self):
        """In-progress checks have conclusion=null; state must map to status."""
        line = json.dumps({"name": "CI", "conclusion": None, "status": "in_progress"})
        with patch("reviewer.gh_helpers._run_gh", return_value=line):
            result = get_check_runs_for_sha("owner/repo", "abc123def")
        assert result == [{"name": "CI", "state": "IN_PROGRESS"}]

    def test_conclusion_present_takes_precedence_over_status(self):
        """Completed checks have conclusion set; status field is ignored."""
        line = json.dumps({"name": "CI", "conclusion": "success", "status": "completed"})
        with patch("reviewer.gh_helpers._run_gh", return_value=line):
            result = get_check_runs_for_sha("owner/repo", "abc123def")
        assert result == [{"name": "CI", "state": "SUCCESS"}]

    def test_malformed_json_line_returns_none(self):
        """A bad JSON line in the paginated output returns None, not a partial list."""
        with patch("reviewer.gh_helpers._run_gh", return_value="not-json"):
            result = get_check_runs_for_sha("owner/repo", "abc123def")
        assert result is None

    def test_command_failure_returns_none(self):
        """_run_gh returning None (rate-limit, auth error) propagates as None."""
        with patch("reviewer.gh_helpers._run_gh", return_value=None):
            result = get_check_runs_for_sha("owner/repo", "abc123def")
        assert result is None

    def test_empty_output_returns_empty_list(self):
        """No check runs yet: empty stdout → empty list, not None."""
        with patch("reviewer.gh_helpers._run_gh", return_value=""):
            result = get_check_runs_for_sha("owner/repo", "abc123def")
        assert result == []

    def test_whitespace_only_output_returns_empty_list(self):
        """Whitespace-only stdout (e.g. trailing newline) is treated as no runs."""
        with patch("reviewer.gh_helpers._run_gh", return_value="\n\n"):
            result = get_check_runs_for_sha("owner/repo", "abc123def")
        assert result == []

    def test_multiple_check_runs_across_lines(self):
        """Multiple newline-delimited JSON objects (paginated output) all parsed."""
        lines = "\n".join([
            json.dumps({"name": "unit", "conclusion": "success", "status": "completed"}),
            json.dumps({"name": "lint", "conclusion": "skipped", "status": "completed"}),
            json.dumps({"name": "build", "conclusion": None, "status": "queued"}),
        ])
        with patch("reviewer.gh_helpers._run_gh", return_value=lines):
            result = get_check_runs_for_sha("owner/repo", "abc123def")
        assert result == [
            {"name": "unit", "state": "SUCCESS"},
            {"name": "lint", "state": "SKIPPED"},
            {"name": "build", "state": "QUEUED"},
        ]

    def test_newest_duplicate_check_run_wins_by_completion_then_id(self):
        """A rerun's current result supersedes cancelled duplicate attempts."""
        lines = "\n".join([
            json.dumps({
                "id": 3, "name": "unit", "conclusion": "cancelled",
                "status": "completed", "completed_at": "2026-09-29T04:07:00Z",
                "started_at": "2026-09-29T04:06:00Z",
            }),
            json.dumps({
                "id": 4, "name": "unit", "conclusion": "success",
                "status": "completed", "completed_at": "2026-09-29T04:14:00Z",
                "started_at": "2026-09-29T04:06:00Z",
            }),
            json.dumps({
                "id": 5, "name": "lint", "conclusion": "success",
                "status": "completed", "completed_at": "2026-09-29T04:14:00Z",
            }),
            json.dumps({
                "id": 6, "name": "lint", "conclusion": "failure",
                "status": "completed", "completed_at": "2026-09-29T04:14:00Z",
            }),
        ])
        with patch("reviewer.gh_helpers._run_gh", return_value=lines):
            result = get_check_runs_for_sha("owner/repo", "abc123def")
        assert result == [
            {"name": "unit", "state": "SUCCESS"},
            {"name": "lint", "state": "FAILURE"},
        ]

    def test_duplicate_check_run_uses_started_at_when_not_completed(self):
        """An in-progress retry is newer when only its start time is available."""
        lines = "\n".join([
            json.dumps({
                "id": 3, "name": "unit", "conclusion": "success",
                "status": "completed", "completed_at": "2026-09-29T04:07:00Z",
            }),
            json.dumps({
                "id": 4, "name": "unit", "conclusion": None,
                "status": "in_progress", "started_at": "2026-09-29T04:14:00Z",
            }),
        ])
        with patch("reviewer.gh_helpers._run_gh", return_value=lines):
            result = get_check_runs_for_sha("owner/repo", "abc123def")
        assert result == [{"name": "unit", "state": "IN_PROGRESS"}]

    def test_uses_paginate_flag(self):
        """REST call includes --paginate to avoid the default 30-run page cap."""
        called_args = []
        with patch("reviewer.gh_helpers._run_gh",
                   side_effect=lambda args, **kw: called_args.append(args) or "") as m:
            get_check_runs_for_sha("owner/repo", "abc123def")
        assert called_args, "expected _run_gh to be called"
        args = called_args[0]
        assert "--paginate" in args

    def test_uses_rest_endpoint_not_graphql(self):
        """Call targets the REST commits/{sha}/check-runs endpoint, not gh pr checks."""
        called_args = []
        with patch("reviewer.gh_helpers._run_gh",
                   side_effect=lambda args, **kw: called_args.append(args) or ""):
            get_check_runs_for_sha("owner/repo", "deadbeef01")
        assert called_args
        args_str = " ".join(called_args[0])
        assert "check-runs" in args_str
        assert "deadbeef01" in args_str
        assert "pr checks" not in args_str


class TestGetCommitStatusForContext:
    _CTX = "qa/llm-reviewer"

    def test_malformed_json_line_returns_none(self):
        """A bad JSON line returns None (unreadable), never a partial answer —
        the caller's fail-open policy must see the read as failed."""
        with patch("reviewer.gh_helpers._run_gh", return_value="not-json"):
            assert get_commit_status_for_context(
                "owner/repo", "abc123def", self._CTX,
            ) is None

    def test_entry_missing_context_is_skipped(self):
        """A status entry without a ``context`` key is skipped, not a crash or
        a false match; a later matching entry is still found."""
        lines = "\n".join([
            json.dumps({"state": "success", "description": "no context key"}),
            json.dumps({
                "context": self._CTX, "state": "error", "description": "boom",
            }),
        ])
        with patch("reviewer.gh_helpers._run_gh", return_value=lines):
            assert get_commit_status_for_context(
                "owner/repo", "abc123def", self._CTX,
            ) == {"state": "error", "description": "boom"}

    def test_non_dict_entry_is_skipped(self):
        """A valid-JSON but non-dict line (string/number) is skipped, not a
        crash; the search continues to the matching entry."""
        lines = "\n".join([
            json.dumps("stray-string"),
            json.dumps(42),
            json.dumps({
                "context": self._CTX, "state": "success",
                "description": "waived",
            }),
        ])
        with patch("reviewer.gh_helpers._run_gh", return_value=lines):
            assert get_commit_status_for_context(
                "owner/repo", "abc123def", self._CTX,
            ) == {"state": "success", "description": "waived"}

    def test_command_failure_returns_none(self):
        """_run_gh returning None (rate-limit, auth error) propagates as None,
        distinct from {} (context genuinely has no status yet)."""
        with patch("reviewer.gh_helpers._run_gh", return_value=None):
            assert get_commit_status_for_context(
                "owner/repo", "abc123def", self._CTX,
            ) is None
