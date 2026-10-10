from __future__ import annotations

import pytest

from host_access import credential_sanitizer
from host_access.credential_sanitizer import (
    REDACTION_MARKER,
    sanitize_credential_data,
    sanitize_credential_text,
)


def _credential_text() -> str:
    return (
        "service status: ready\n"
        "Authorization: Bearer auth-secret\n"
        "Cookie: session=cookie-secret; theme=dark\n"
        "dsn=postgresql://alice:dsn-secret@db.example/app\n"
        "api_token=token-secret\n"
        'password: "password-secret"\n'
        "client_secret='client-secret'\n"
        "-----BEGIN PRIVATE KEY-----\n"
        "pem-private-secret\n"
        "-----END PRIVATE KEY-----"
    )


def test_sanitize_credential_text_redacts_recognized_forms_and_keeps_structure():
    safe = sanitize_credential_text(_credential_text())

    for secret in (
        "auth-secret",
        "cookie-secret",
        "dsn-secret",
        "token-secret",
        "password-secret",
        "client-secret",
        "pem-private-secret",
    ):
        assert secret not in safe
    assert "Authorization: Bearer [REDACTED]" in safe
    assert "Cookie: session=[REDACTED]; theme=[REDACTED]" in safe
    assert "postgresql://alice:[REDACTED]@db.example/app" in safe
    assert "api_token=[REDACTED]" in safe
    assert 'password: "[REDACTED]"' in safe
    assert "client_secret='[REDACTED]'" in safe
    assert "-----BEGIN PRIVATE KEY-----" in safe
    assert "-----END PRIVATE KEY-----" in safe
    assert "service status: ready" in safe


def test_sanitize_credential_data_uses_sensitive_keys_without_flattening():
    safe = sanitize_credential_data(
        {
            "headers": {"Authorization": "Bearer structured-secret"},
            "config": {
                "token": "token-value",
                "nested": [{"password": "password-value", "status": "ready"}],
            },
            "nullable": {"api_key": None},
        }
    )

    assert safe == {
        "headers": {"Authorization": "Bearer [REDACTED]"},
        "config": {
            "token": REDACTION_MARKER,
            "nested": [{"password": REDACTION_MARKER, "status": "ready"}],
        },
        "nullable": {"api_key": None},
    }


def test_sanitize_suffix_named_credential_assignments_and_structured_keys():
    safe_text = sanitize_credential_text(
        "PGPASSWORD=pg-secret\n"
        "GITHUB_TOKEN=github-secret\n"
        "AWS_SECRET_ACCESS_KEY=aws-secret"
    )
    safe_data = sanitize_credential_data(
        {
            "PGPASSWORD": "pg-secret",
            "GITHUB_TOKEN": "github-secret",
            "AWS_SECRET_ACCESS_KEY": "aws-secret",
            "token_count": 42,
            "password_policy": "strict",
        }
    )

    for secret in ("pg-secret", "github-secret", "aws-secret"):
        assert secret not in safe_text
        assert secret not in safe_data.values()
    assert "PGPASSWORD=[REDACTED]" in safe_text
    assert "GITHUB_TOKEN=[REDACTED]" in safe_text
    assert "AWS_SECRET_ACCESS_KEY=[REDACTED]" in safe_text
    assert safe_data == {
        "PGPASSWORD": REDACTION_MARKER,
        "GITHUB_TOKEN": REDACTION_MARKER,
        "AWS_SECRET_ACCESS_KEY": REDACTION_MARKER,
        "token_count": 42,
        "password_policy": "strict",
    }


def test_sanitize_text_redacts_netrc_whitespace_and_header_forms():
    safe = sanitize_credential_text(
        "machine github.com login alice password netrc-secret\n"
        "oauth_token ghp_result_secret\n"
        "Authorization Bearer auth-result-secret"
    )

    for secret in (
        "netrc-secret",
        "ghp_result_secret",
        "auth-result-secret",
    ):
        assert secret not in safe
    assert "password [REDACTED]" in safe
    assert "oauth_token [REDACTED]" in safe
    assert "Authorization Bearer [REDACTED]" in safe


def test_sanitize_text_redacts_http_userinfo_without_password_separator():
    safe = sanitize_credential_text(
        "origin https://ghp_actual_secret_123@github.com/acme/private.git\n"
        "mirror http://opaque-access-token@example.test/archive\n"
        "fetch git+https://vcs-access-token@example.test/repository"
    )

    assert "ghp_actual_secret_123" not in safe
    assert "opaque-access-token" not in safe
    assert "vcs-access-token" not in safe
    assert "https://[REDACTED]@github.com/acme/private.git" in safe
    assert "http://[REDACTED]@example.test/archive" in safe
    assert "git+https://[REDACTED]@example.test/repository" in safe


@pytest.mark.parametrize(
    ("rule", "callback", "sample"),
    [
        ("_PEM_PRIVATE_KEY_RE", "_redact_private_key", "-----BEGIN PRIVATE KEY-----\nvalue\n-----END PRIVATE KEY-----"),
        ("_URL_USERINFO_RE", "_redact_url_userinfo", "https://user:password@example.test"),
        ("_COOKIE_HEADER_RE", "_redact_cookie_header", "Cookie: session=value"),
        ("_AUTH_SCHEME_RE", "_redact_auth_scheme", "Authorization: Bearer value"),
        ("_AUTH_SPACE_SCHEME_RE", "_redact_space_value", "Authorization Bearer value"),
        ("_NETRC_PASSWORD_RE", "_redact_space_value", "machine example.test password value"),
        ("_OAUTH_TOKEN_RE", "_redact_space_value", "oauth_token value"),
        ("_ASSIGNMENT_RE", "_redact_assignment", "api_key=value"),
        ("_COOKIE_ASSIGNMENT_RE", "_redact_assignment", "cookie=value"),
    ],
)
def test_text_regex_callbacks_match_their_group_contract(rule, callback, sample):
    # Drives the production rule table, so a broken production callback fails.
    regex = getattr(credential_sanitizer, rule)
    pair = next(entry for entry in credential_sanitizer._RULES if entry[0] is regex)
    assert pair[1].__name__ == callback
    assert regex.sub(pair[1], sample) != sample
    assert credential_sanitizer.credential_spans(sample)


def test_rule_table_has_a_contract_sample_for_every_rule():
    covered = {
        getattr(credential_sanitizer, name)
        for name in (
            "_PEM_PRIVATE_KEY_RE", "_URL_USERINFO_RE", "_COOKIE_HEADER_RE",
            "_AUTH_SCHEME_RE", "_AUTH_SPACE_SCHEME_RE", "_NETRC_PASSWORD_RE",
            "_OAUTH_TOKEN_RE", "_ASSIGNMENT_RE", "_COOKIE_ASSIGNMENT_RE",
        )
    }
    assert {regex for regex, _callback in credential_sanitizer._RULES} == covered | {
        credential_sanitizer._TOKEN_PREFIX_RE
    }


def test_credential_spans_report_only_the_changed_region():
    text = 'x = 1\napi_key = "' + "hunter2" + '"\nok\n'
    spans = credential_sanitizer.credential_spans(text)
    assert [text[start:end] for start, end in spans] == ["hunter2"]
    assert credential_sanitizer.credential_spans("api_key = None") == []
    assert credential_sanitizer.credential_spans("plain text") == []


@pytest.mark.parametrize("value", ["abcD]", "[REDxyz", "[REDACTED-x]"])
def test_credential_spans_keep_value_edges_that_match_the_marker(value):
    text = f'password = "{value}"'
    start = text.index(value)

    assert credential_sanitizer.credential_spans(text) == [(start, start + len(value))]


def test_token_formats_redact_without_changing_credential_recognition():
    token_samples = [
        "sk-" + "a" * 24,
        "ghp_" + "a" * 24,
        "AKIA" + "A" * 12,
    ]
    non_credentials = ["a" * 40, "https://example.test/path", "ordinary words"]

    for value in token_samples:
        assert sanitize_credential_text(value) == REDACTION_MARKER
        assert credential_sanitizer.contains_credential(value)
    for value in non_credentials:
        assert sanitize_credential_text(value) == value
        assert not credential_sanitizer.contains_credential(value)


def test_sanitize_text_redacts_incomplete_private_key_block():
    safe = sanitize_credential_text(
        "read 64 bytes\n"
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "partial-private-secret\n"
    )

    assert "partial-private-secret" not in safe
    assert safe == (
        "read 64 bytes\n"
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "[REDACTED]"
    )


def test_sanitize_credential_text_leaves_normal_code_and_status_unchanged():
    value = (
        "service status: ready\n"
        "token_count=42\n"
        "token=example\n"
        "password_policy=strict\n"
        "password policy enabled\n"
        "Authorization policy: deny\n"
        "Basic backend checks passed\n"
        "Bearer authentication is enabled\n"
        "Cookie jar: empty\n"
        "ssh://git@github.com/example/repository.git\n"
        "https://github.com/example/repository.git\n"
        "http://example.test/status\n"
        "def refresh_token_count(): return 1\n"
        "-----BEGIN PUBLIC KEY-----\n"
        "public-material\n"
        "-----END PUBLIC KEY-----"
    )

    assert sanitize_credential_text(value) == value
