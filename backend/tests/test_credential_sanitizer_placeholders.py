"""Placeholder references must not mask or suppress real credential shapes."""

import pytest

from host_access.credential_sanitizer import (
    contains_credential, sanitize_credential_data, sanitize_credential_text,
)


@pytest.mark.parametrize("text", [
    "Authorization: Bearer YOUR_API_KEY",
    ' -H "Authorization: Bearer $TOKEN"',
    'api_key = "YOUR_API_KEY"',
    'curl -H "Authorization: Bearer YOUR_API_KEY"',  # gitleaks:allow
    "Authorization: Bearer $API_KEY",
    "Authorization: Bearer ${API_KEY}",
    "Authorization: Bearer <token>",
    "Bearer $API_KEY",
    "Bearer ${API_KEY}",
    "Bearer <token>",
    '"Authorization": "Bearer {api_key}"',
    '{"Authorization": "Bearer YOUR_API_KEY"}',
    "{'Authorization': 'Bearer YOUR_API_KEY'}",
    r'{\"api_key\": \"YOUR_API_KEY\"}',
    'api_key = "<your-api-key>"',
    "export OPENAI_API_KEY=$OPENAI_API_KEY",
    "password: ${DB_PASSWORD}",
    "Cookie: session=${SESSION}",
    "Authorization: Bearer example",
    "Authorization: Bearer ${GITHUB_PERSONAL_ACCESS_TOKEN}",
    "Authorization: Bearer {self.api_key}",
    "Authorization: Bearer YOUR_OPENAI_API_KEY",
])
def test_placeholder_is_unchanged(text):
    assert not contains_credential(text)
    assert sanitize_credential_text(text) == text


def test_authorization_data_placeholder_is_unchanged():
    data = {"Authorization": "Bearer YOUR_API_KEY"}
    assert sanitize_credential_data(data) == data


SHAPES = {
    "openai": "sk-" + "proj-" + "a" * 24,
    "github": "ghp_" + "b" * 24,
    "aws": "AKIA" + "C" * 16,
    "jwt": "eyJ" + "d" * 10 + ".eyJ" + "e" * 10 + "." + "f" * 20,
}
PLACEHOLDERS = ["$API_KEY", "${API_KEY}", "{api_key}", "<token>", "YOUR_API_KEY"]


@pytest.mark.parametrize("shape", SHAPES.values(), ids=SHAPES.keys())
def test_shape_alone(shape):
    assert contains_credential(shape)


@pytest.mark.parametrize("shape", SHAPES.values(), ids=SHAPES.keys())
@pytest.mark.parametrize("placeholder", PLACEHOLDERS)
def test_shape_next_to_placeholder(shape, placeholder):
    assert contains_credential(f"Authorization: Bearer {placeholder} {shape}")


@pytest.mark.parametrize("text", [
    'password = "$ecretP4ss"',
    'password = "$password1"',
    'token = $token_abc',
    'api_key = "YOUR_KEY_9f8a7b6c5d4e"',
    'token = "<a8Fk29dLqZ>"',
    'api_key = "9f8a7b6c5d4e3f2a1b0c"',  # gitleaks:allow
    'api_key = "YOUR_API_KEY-9f8a7b"',
])
def test_real_assignments_are_redacted(text):
    assert contains_credential(text)


@pytest.mark.parametrize("value", [
    "{9f8a7b6c5d4e3f2a}", "{abc123def456}", "${A1B2C3D4:-x}",
    "a8Fk29dLqZ7xP0", "$API_KEY.extra123",
])
@pytest.mark.parametrize("template", [
    "Authorization: Bearer {}", '{{"Authorization": "Bearer {}"}}',
])
def test_real_authorization_values_are_redacted(value, template):
    assert contains_credential(template.format(value))


BODY = "a_" * 12
EMBEDDED = {
    "your-openai": "YOUR_" + "sk-" + "a" * 24,
    "your-github": "YOUR_" + "ghp_" + "a" * 36,
    "template-long": "{prefix_" + "a" * 24 + "}",
    "angle-openai": "<name_" + "sk-" + "a" * 24 + ">",
    "braced-aws": "${X_" + "AKIA" + "A" * 16 + "}",
    "env-aws": "$" + "AKIA" + "A" * 16,
    "your-openai-underscores": "YOUR_" + "sk-" + BODY,
    "your-github-underscores": "YOUR_" + "ghp_" + BODY,
    "template-github-underscores": "{prefix_" + "ghp_" + BODY + "}",
    "angle-openai-underscores": "<prefix_" + "sk-" + BODY + ">",
    "your-jwt": "YOUR_" + SHAPES["jwt"],
    "shell-suffix": "${API_KEY}a8Fk29dLqZ7xP0",
    "template-suffix": "{api_key}suffix",
    "long-env": "$ABCDEFGHJKLMNPQRSTUV",
    "conservative-angle": "<your-task-management-key-long>",
}


@pytest.mark.parametrize("value", EMBEDDED.values(), ids=EMBEDDED.keys())
@pytest.mark.parametrize("template", [
    "Authorization: Bearer {}", '{{"Authorization": "Bearer {}"}}',
    'api_key = "{}"',
])
def test_embedded_real_values_are_redacted(value, template):
    assert contains_credential(template.format(value))


def test_bare_assignment_reference_with_suffix_is_redacted():
    assert contains_credential("api_key = ${API_KEY}a8Fk29dLqZ7xP0")


# Colon-less header and oauth_token forms stop their value at '}', so the brace must be re-attached.
@pytest.mark.parametrize("text", [
    "Authorization Bearer {api_key}",
    "Authorization Bearer ${API_KEY}",
    "authorization basic {credentials}",
    "oauth_token {api_key}",
    "oauth-token ${OAUTH_TOKEN}",
])
def test_space_scheme_brace_placeholder_is_unchanged(text):
    assert sanitize_credential_text(text) == text
    assert not contains_credential(text)


@pytest.mark.parametrize("text", [
    "Authorization Bearer q8Zt3mWv1Kp9Lx2Rb7Nc}",  # gitleaks:allow
    "oauth_token q8Zt3mWv1Kp9Lx2Rb7Nc}",  # gitleaks:allow
])
def test_space_scheme_real_value_before_brace_is_redacted(text):
    assert "q8Zt3mWv1Kp9Lx2Rb7Nc" not in sanitize_credential_text(text)
    assert contains_credential(text)
