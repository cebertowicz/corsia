"""Credentials in shell commands stay masked when a thread's history is read back.

Each command is stored as a tool call (as an OpenAI-style JSON arguments string, and as an args
dict), rendered by get_thread_history in full and preview modes, then passed through the tool
registry's result boundary. Every value here is fake.
"""
import json

import pytest

from host_access.credential_sanitizer import REDACTION_MARKER, sanitize_credential_text
from tools.query_projects import _history_line
from tools.result_sanitizer import sanitize_result

JWT = "eyJhbGciOiJIUzI1NiJ9.eyJmYWtlIjoicHJvYmUifQ.ZmFrZXNpZ25hdHVyZWZha2VzaWduYXR1cmU"  # gitleaks:allow
OPAQUE = "fakeOpaqueValue0123456789abcdefXYZ"
HOST = "https://api.example.test"

# Shapes that were returned unmasked before this rule set.
WERE_LEAKING = {
    "json auth message": f'echo \'{{"type":"auth","access_token":"{JWT}"}}\' | websocat wss://api.example.test/ws',
    "headers dict": f"python3 -c \"import requests; requests.get('{HOST}/', headers={{'Authorization': 'Bearer {JWT}'}})\"",
    "bare token argument": f"some-cli --token {JWT} state list",
    "custom header": f'curl -H "x-api-access: {JWT}" {HOST}/',
}
# Shapes that were already masked and must stay masked.
WERE_MASKED = {
    "authorization header, double quotes": f'curl -s -H "Authorization: Bearer {JWT}" {HOST}/',
    "authorization header, single quotes": f"curl -s -H 'Authorization: Bearer {JWT}' {HOST}/",
    "token assignment": f'TOKEN="{JWT}"; curl -H "Authorization: Bearer $TOKEN" {HOST}/',
    "prefixed token export": f"export SERVICE_TOKEN={JWT}",
}


def _stored(command: str, form: str) -> str:
    if form == "arguments string":
        call = {"id": "c1", "type": "function",
                "function": {"name": "bash", "arguments": json.dumps({"command": command})}}
    else:
        call = {"id": "c1", "name": "bash", "args": {"command": command}}
    return json.dumps({"tool_calls": [call]})


def _read_back(command: str, form: str, mode: str) -> str:
    row = {"content": "", "metadata": _stored(command, form), "role": "assistant",
           "seq": 7, "created_at": "2026-01-01T00:00:00", "update_id": None}
    return sanitize_result(_history_line(row, mode))


@pytest.mark.parametrize("mode", ["preview", "full"])
@pytest.mark.parametrize("form", ["arguments string", "args dict"])
@pytest.mark.parametrize("command", [*WERE_LEAKING.values(), *WERE_MASKED.values()],
                         ids=[*WERE_LEAKING, *WERE_MASKED])
def test_history_read_masks_a_jwt_in_a_shell_command(command, form, mode):
    shown = _read_back(command, form, mode)
    assert JWT[:24] not in shown
    assert JWT.split(".")[2] not in shown
    assert REDACTION_MARKER in shown


@pytest.mark.parametrize("text", [
    f"headers={{'Authorization': 'Bearer {OPAQUE}'}}",
    f'headers={{"Authorization": "Bearer {OPAQUE}"}}',
    json.dumps({"command": f'curl -H "Authorization: Bearer {OPAQUE}" {HOST}/'}),
    json.dumps({"command": f'echo \'{{"access_token":"{OPAQUE}"}}\''}),
    json.dumps({"command": f"python3 -c \"h={{'api_key': '{OPAQUE}'}}\""}),
])
def test_quoted_and_escaped_keys_mask_an_opaque_value(text):
    safe = sanitize_credential_text(text)
    assert OPAQUE not in safe
    assert REDACTION_MARKER in safe


def test_escaped_value_keeps_its_closing_escape():
    text = json.dumps({"command": f'curl -H "Authorization: Bearer {OPAQUE}" {HOST}/'})
    safe = sanitize_credential_text(text)
    assert f'Bearer {REDACTION_MARKER}\\"' in safe
    assert json.loads(safe)["command"].endswith(f"{HOST}/")


@pytest.mark.parametrize("text", [
    "eyJhbGciOiJIUzI1NiJ9",  # a lone JSON header is not a token
    "sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
    "the authorization header was missing",
    'headers={"Authorization": "Bearer [REDACTED]"}',
])
def test_text_that_is_not_a_credential_is_left_alone(text):
    assert sanitize_credential_text(text) == text
