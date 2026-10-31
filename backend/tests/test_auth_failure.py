from connections.auth_failure import SIGN_IN_EXPIRED, auth_failure_kind


def test_only_claudes_exact_unrefreshable_session_error_is_definitive() -> None:
    assert auth_failure_kind(
        "claude-code", "Failed to authenticate: OAuth session expired and could not be refreshed",
    ) == SIGN_IN_EXPIRED
    assert auth_failure_kind("claude-code", "HTTP 401 from proxy; retry the request") is None
    assert auth_failure_kind(
        "codex", "Failed to authenticate: OAuth session expired and could not be refreshed",
    ) is None
