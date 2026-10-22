"""A saved setup report names each failed row's cause and copies only the safe log lines about it."""

from __future__ import annotations

import pytest

from onboarding.setup_report import MAX_LINES, WITHHELD, relevant_lines, report_details

ROWS = [
    {"key": "claude-code", "name": "Claude Code", "cause": "Couldn’t install Claude Code.", "reference": "cli-0a1b2c3d4e5f6a7b"},
    {"key": "omlx", "name": "Local model", "cause": "CORSIA couldn’t set up the local model.", "reference": "local-9f8e7d6c5b4a3f2e"},
]


def test_report_names_each_failed_row_and_its_log_lines(tmp_path):
    log = tmp_path / "service.log"
    log.write_text(
        "INFO: Application startup complete.\n"
        "CLI install for claude-code failed reference=cli-0a1b2c3d4e5f6a7b cause=installer_failed\n"
        "codex sign-in failed cause=vendor_login_failed\n"
        "Local setup failed reference=local-9f8e7d6c5b4a3f2e cause=other\n"
    )
    text = report_details(ROWS, log)
    assert "Claude Code: Couldn’t install Claude Code. (reference cli-0a1b2c3d4e5f6a7b)" in text
    assert "Local model: CORSIA couldn’t set up the local model. (reference local-9f8e7d6c5b4a3f2e)" in text
    assert "CLI install for claude-code failed reference=cli-0a1b2c3d4e5f6a7b" in text
    assert "Local setup failed reference=local-9f8e7d6c5b4a3f2e" in text
    # Lines without a failed row's reference stay out, even when they name its tool.
    assert "codex sign-in failed" not in text and "startup complete" not in text
    assert relevant_lines(["claude-code wrote its login"], ROWS) == []


@pytest.mark.parametrize("line", [
    "claude-code login wrote access_token=abc123",
    "claude-code Authorization: Basic Zm9vOmJhcg==",
    "claude-code password reset",
    "claude-code key sk-ant-REDACTEDREDACTED",
    "claude-code session cookie set",
])
def test_a_line_with_a_credential_word_is_withheld_whole(line):
    assert relevant_lines([f"{line} reference={ROWS[0]['reference']}"], ROWS[:1]) == [WITHHELD]


def test_only_the_last_lines_are_kept_and_a_missing_log_is_said(tmp_path):
    lines = [f"step {index} reference={ROWS[0]['reference']}" for index in range(MAX_LINES + 10)]
    assert relevant_lines(lines, ROWS[:1]) == lines[-MAX_LINES:]
    assert "none found" in report_details(ROWS, tmp_path / "absent.log")


def test_report_route_returns_the_body_before_the_first_account(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api import setup
    from infra import accounts, config

    monkeypatch.setattr(accounts, "has_users", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(setup, "_trusted_json_post", lambda _request: True)
    monkeypatch.setattr(config, "LOGS_DIR", tmp_path)
    (tmp_path / "service.log").write_text("codex sign-in failed reference=signin-0123456789abcdef cause=vendor_login_failed\n")
    app = FastAPI()
    app.include_router(setup.router)
    client = TestClient(app)
    row = {"key": "codex", "name": "Codex", "cause": "Sign-in didn’t finish.", "reference": "signin-0123456789abcdef"}
    response = client.post("/api/setup/report", json={"rows": [row]})
    assert response.status_code == 200
    assert "Codex: Sign-in didn’t finish." in response.json()["text"]
    assert "codex sign-in failed reference=signin-0123456789abcdef" in response.json()["text"]
    assert client.post("/api/setup/report", json={"rows": [{"key": "shell", "name": "x"}]}).status_code == 422
    # Only a minted reference is accepted, so a one-letter "reference" cannot select log lines.
    loose = {"key": "api", "name": "API", "reference": "e"}
    assert client.post("/api/setup/report", json={"rows": [loose]}).status_code == 422


def test_a_reference_selects_only_its_whole_log_token():
    row = {"key": "api", "name": "API", "reference": "signin-0123456789abcdef"}
    lines = ["sign-in failed reference=signin-0123456789abcdef cause=x",
             "other reference=signin-0123456789abcdef0 cause=y",
             "mentions signin-0123456789abcdef in passing"]
    assert relevant_lines(lines, [row]) == lines[:1]
    assert relevant_lines(lines, [{**row, "reference": "e"}]) == []


@pytest.mark.parametrize("line", [
    "claude-code opened https://example.test/oauth?code=abc&state=xyz",
    "codex enter the one-time device code ABCD-1234",
    "claude-code signed in as person@example.test",
])
def test_sign_in_codes_and_emails_are_withheld_whole(line):
    assert relevant_lines([f"{line} reference={ROWS[0]['reference']}"], ROWS[:1]) == [WITHHELD]
