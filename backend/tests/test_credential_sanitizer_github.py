import pytest

from host_access.credential_sanitizer import sanitize_credential_data, sanitize_credential_text


@pytest.mark.parametrize("prefix", ["ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_"])
def test_github_credential_shapes_are_redacted(prefix):
    credential = prefix + "a1B2" * 6
    assert sanitize_credential_text(f"before {credential} after") == "before [REDACTED] after"
    assert sanitize_credential_data({"body": credential}) == {"body": "[REDACTED]"}


def test_authorization_token_scheme_is_redacted():
    assert sanitize_credential_text("Authorization: token secret-value") == "Authorization: token [REDACTED]"
    assert sanitize_credential_data({"Authorization": "token secret-value"}) == {"Authorization": "token [REDACTED]"}
