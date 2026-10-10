from host_access.credential_sanitizer import REDACTION_MARKER, sanitize_credential_text


def test_sanitizer_redacts_call_arguments_and_cookie_assignments():
    values = (
        "Client(api_key=never-show-this, timeout=3)",
        "client.call(token=never.show.this, timeout=5)",
        "curl -b cookie=never-show-this",
    )

    for value in values:
        safe = sanitize_credential_text(value)
        assert "never-show-this" not in safe
        assert "never.show.this" not in safe
        assert REDACTION_MARKER in safe
