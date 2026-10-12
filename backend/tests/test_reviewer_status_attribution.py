"""Provenance coverage for reviewer commit-status publication."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch


def _proc(stdout: str = '{"id": 1}') -> MagicMock:
    """Return a successful ``gh`` process result."""
    result = MagicMock()
    result.returncode = 0
    result.stdout = stdout
    result.stderr = ""
    return result


def _posted_payloads(mock_run: MagicMock) -> list[dict[str, str]]:
    """Return JSON payloads sent to GitHub's status endpoint."""
    return [
        json.loads(call.kwargs["input"])
        for call in mock_run.call_args_list
        if "--method" in call.args[0]
    ]


@patch("reviewer.gh_api.subprocess.run")
def test_every_reviewer_status_write_has_instance_and_write_stamp(
    mock_run: MagicMock, monkeypatch,
):
    """Every public setter publishes a compact source stamp on GitHub."""
    from reviewer import reviewer_status

    monkeypatch.setattr(
        reviewer_status, "RUNTIME_INSTANCE_ID", "0123456789abcdeffedcba9876543210",
    )
    mock_run.return_value = _proc()
    verdict = "0 block — exact-head review passed"

    assert reviewer_status.set_pending("owner/repo", "a" * 40, verdict)
    assert reviewer_status.set_success("owner/repo", "a" * 40, verdict)
    assert reviewer_status.set_failure("owner/repo", "a" * 40, verdict)
    assert reviewer_status.set_error("owner/repo", "a" * 40, verdict)

    payloads = _posted_payloads(mock_run)
    assert len(payloads) == 4
    for payload in payloads:
        assert payload["description"].startswith(verdict)
        assert "[m:0123456789ab " in payload["description"]
        assert "[m:" in payload["description"]
        assert " w:" in payload["description"]
        assert payload["description"].endswith("]")
        assert len(payload["description"]) <= 140


@patch("reviewer.gh_api.subprocess.run")
def test_presence_status_uses_its_own_context_after_a_verdict(mock_run: MagicMock):
    """Presence is a second success context, distinct from the verdict."""
    from reviewer import reviewer_status
    from reviewer.reviewer_categories import STATUS_PRESENCE_CONTEXT

    mock_run.return_value = _proc()

    assert reviewer_status.set_presence("owner/repo", "a" * 40)

    payload = _posted_payloads(mock_run)[0]
    assert payload["state"] == "success"
    assert payload["context"] == STATUS_PRESENCE_CONTEXT
    assert payload["description"].startswith("reviewer verdict published at this head")


@patch("reviewer.gh_api.subprocess.run")
def test_failed_presence_post_reports_error_on_presence_only(mock_run: MagicMock):
    """A failed marker is visible on its own context and never on the verdict."""
    from reviewer import reviewer_status
    from reviewer.reviewer_categories import STATUS_PRESENCE_CONTEXT

    failed = _proc()
    failed.returncode = 1
    mock_run.side_effect = [failed, _proc()]

    assert not reviewer_status.set_presence("owner/repo", "a" * 40)

    payloads = _posted_payloads(mock_run)
    assert [p["state"] for p in payloads] == ["success", "error"]
    assert {p["context"] for p in payloads} == {STATUS_PRESENCE_CONTEXT}


@patch("reviewer.gh_helpers._run_gh")
def test_unattributed_success_is_returned_as_success_and_logged(
    mock_run: MagicMock, caplog,
):
    """Legacy or external statuses retain their state while becoming visible."""
    from reviewer.gh_helpers import get_commit_status_for_context

    mock_run.return_value = json.dumps({
        "context": "qa/llm-reviewer",
        "state": "success",
        "description": "0 block — exact-head review passed",
    })

    with caplog.at_level("WARNING"):
        status = get_commit_status_for_context(
            "owner/repo", "a" * 40, "qa/llm-reviewer",
        )

    assert status == {
        "state": "success",
        "description": "0 block — exact-head review passed",
    }
    assert "un-attributed" in caplog.text


@patch("reviewer.gh_helpers._run_gh")
def test_attributed_status_is_not_logged_as_unattributed(
    mock_run: MagicMock, caplog,
):
    """Pipeline status stamps are recognised on their read path."""
    from reviewer.gh_helpers import get_commit_status_for_context

    mock_run.return_value = json.dumps({
        "context": "qa/llm-reviewer",
        "state": "success",
        "description": "0 block — exact-head review passed [m:abc123def456 w:abcdef123456]",
    })

    with caplog.at_level("WARNING"):
        status = get_commit_status_for_context(
            "owner/repo", "a" * 40, "qa/llm-reviewer",
        )

    assert status is not None
    assert status["state"] == "success"
    assert "un-attributed" not in caplog.text


@patch("reviewer.gh_helpers._run_gh")
def test_malformed_status_stamp_is_unattributed_and_state_honoured(
    mock_run: MagicMock, caplog,
):
    """Malformed external suffixes retain state but never claim provenance."""
    from reviewer.gh_helpers import get_commit_status_for_context

    mock_run.return_value = json.dumps({
        "context": "qa/llm-reviewer",
        "state": "success",
        "description": "0 block — exact-head review passed [m:x w:a]",
    })

    with caplog.at_level("WARNING"):
        status = get_commit_status_for_context(
            "owner/repo", "a" * 40, "qa/llm-reviewer",
        )

    assert status is not None
    assert status["state"] == "success"
    assert "un-attributed" in caplog.text


@patch("reviewer.gh_api.subprocess.run")
def test_status_write_log_correlates_payload_to_github_target(
    mock_run: MagicMock, caplog, monkeypatch,
):
    """A visible write ID identifies the exact logged status publication."""
    from reviewer import reviewer_status

    class _FixedWriteId:
        hex = "feedfacecafebeefdeadbeef00112233"

    monkeypatch.setattr(reviewer_status, "uuid4", _FixedWriteId)
    monkeypatch.setattr(
        reviewer_status, "RUNTIME_INSTANCE_ID", "0123456789abcdeffedcba9876543210",
    )
    mock_run.return_value = _proc()

    with caplog.at_level("INFO"):
        assert reviewer_status.set_status(
            "owner/repo", "a" * 40, "success", "exact-head clean",
        )

    payload = _posted_payloads(mock_run)[0]
    assert payload["description"].endswith("w:feedfacecafe]")
    assert "posted status write" in caplog.text
    assert "repo=owner/repo" in caplog.text
    assert "sha=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" in caplog.text
    assert "state=success" in caplog.text
    assert "instance=0123456789abcdeffedcba9876543210" in caplog.text
    assert "write_id=feedfacecafebeefdeadbeef00112233" in caplog.text
