from __future__ import annotations

import json
import math

import pytest


@pytest.mark.parametrize(
    ("command", "secret", "expected_tokens"),
    [
        (
            "env PGPASSWORD=db-secret psql app",
            "db-secret",
            ["env", "PGPASSWORD=[REDACTED]", "psql", "app"],
        ),
        (
            "curl -H 'Cookie: session=cookie-secret' https://example.test",
            "cookie-secret",
            [
                "curl",
                "-H",
                "Cookie: [REDACTED]",
                "https://example.test",
            ],
        ),
        (
            "mysql -pmysql-secret app",
            "mysql-secret",
            ["mysql", "-p[REDACTED]", "app"],
        ),
        (
            "mysql -p mysql-secret app",
            "mysql-secret",
            ["mysql", "-p", "[REDACTED]", "app"],
        ),
        (
            "printf 'token=actual-secret'",
            "actual-secret",
            ["printf", "token=[REDACTED]"],
        ),
    ],
)
def test_literal_credential_forms_are_redacted_and_classified_literal(
    command: str,
    secret: str,
    expected_tokens: list[str],
) -> None:
    from host_access.redaction import redact_shell_command
    from tools.shell_guard import check_command_safety

    result = redact_shell_command(command)

    assert result.tokens == tuple(expected_tokens)
    assert result.redactions == 1
    assert result.literal_credentials == 1
    assert secret not in json.dumps(result.descriptor())
    denial = check_command_safety(command)
    assert denial is not None
    assert secret not in denial


@pytest.mark.parametrize(
    ("command", "secret"),
    [
        (
            "curl --proxy-user alice:proxy-secret https://example.test",
            "proxy-secret",
        ),
        (
            "printf 'stdin-secret' | "
            "docker login --password-stdin registry.example",
            "stdin-secret",
        ),
        (
            "gh auth login --with-token <<< ghp_literal_secret",
            "ghp_literal_secret",
        ),
        (
            "security add-generic-password -a user -s app "
            "-w keychain-secret",
            "keychain-secret",
        ),
    ],
)
def test_literal_credential_sources_for_stdin_and_host_auth_fail_closed(
    command: str,
    secret: str,
) -> None:
    from host_access.redaction import redact_shell_command
    from tools.shell_guard import check_command_safety

    result = redact_shell_command(command)

    assert result.literal_credentials >= 1
    assert secret not in json.dumps(result.descriptor())
    assert check_command_safety(command) is not None


@pytest.mark.parametrize(
    "command",
    [
        "env PGPASSWORD=${DB_PASSWORD:-fallback-secret} psql app",
        """tool --data '{"PGPASSWORD":"json-secret"'""",
        "tool --password '${PASSWORD:-fallback-secret}'",
        'tool --password "$(printf fallback-secret)"',
        (
            'tool --password "$(security find-generic-password -w -s app '
            '>/tmp/leak)"'
        ),
        "tool --password `printf fallback-secret`",
        "tool --password $'hunter2'",
    ],
)
def test_ambiguous_credential_expansions_fail_closed(command: str) -> None:
    from host_access.redaction import (
        CommandRedactionError,
        redact_shell_command,
    )
    from tools.shell_guard import check_command_safety

    with pytest.raises(CommandRedactionError):
        redact_shell_command(command)
    assert check_command_safety(command) is not None


@pytest.mark.parametrize(
    ("command", "expected_redactions"),
    [
        ("env PGPASSWORD=$DB_PASSWORD psql app", 1),
        (
            "env PGPASSWORD=$(security find-generic-password -w -s app) "
            "psql app",
            1,
        ),
        ("curl --user alice https://example.test", 0),
        ("gh auth login --with-token < token.txt", 0),
        ("docker login --password-stdin registry.example", 0),
        (
            "printf '%s' \"$REGISTRY_PASSWORD\" | "
            "docker login --password-stdin registry.example",
            1,
        ),
        ("gh auth login --with-token <<< \"$GH_TOKEN\"", 1),
        (
            "security find-generic-password -w -s app | "
            "gh auth login --with-token",
            0,
        ),
        ("tool --token-limit 4096", 0),
        ("printf 'token=example'", 0),
        ("ssh homeassistant", 0),
    ],
)
def test_mediated_credentials_and_noncredential_options_remain_allowed(
    command: str,
    expected_redactions: int,
) -> None:
    from host_access.redaction import redact_shell_command
    from tools.shell_guard import check_command_safety

    result = redact_shell_command(command)

    assert result.redactions == expected_redactions
    assert result.literal_credentials == 0
    assert check_command_safety(command) is None


def _valid_bash_descriptor() -> dict[str, object]:
    return {
        "version": 1,
        "tool": "bash",
        "command": {
            "tokens": ["ps", "-ef"],
            "redactions": 0,
            "literal_credentials": 0,
        },
        "working_directory": "/tmp/work",
        "timeout_seconds": 120,
        "background": False,
    }


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.pop("command"),
        lambda value: value.pop("working_directory"),
        lambda value: value.pop("timeout_seconds"),
        lambda value: value.pop("background"),
        lambda value: value.__setitem__("version", True),
        lambda value: value.__setitem__("version", 1.0),
        lambda value: value.__setitem__("working_directory", 42),
        lambda value: value.__setitem__("timeout_seconds", True),
        lambda value: value.__setitem__("timeout_seconds", "120"),
        lambda value: value.__setitem__("timeout_seconds", 0),
        lambda value: value.__setitem__("timeout_seconds", 601),
        lambda value: value.__setitem__("timeout_seconds", math.nan),
        lambda value: value.__setitem__("background", 0),
        lambda value: value.__setitem__("extra", "field"),
    ],
)
def test_bash_descriptor_requires_the_complete_typed_contract(mutation) -> None:
    from host_access.redaction import (
        CommandRedactionError,
        validate_action_descriptor,
    )

    descriptor = _valid_bash_descriptor()
    mutation(descriptor)

    with pytest.raises(CommandRedactionError):
        validate_action_descriptor("bash", descriptor)


@pytest.mark.parametrize(
    "working_directory",
    [
        "/tmp/token=raw-secret",
        "/tmp?password=raw-secret",
        "https://alice:raw-secret@example.test/work",
    ],
)
def test_bash_descriptor_rejects_credential_bearing_working_directory(
    working_directory: str,
) -> None:
    from host_access.redaction import (
        CommandRedactionError,
        validate_action_descriptor,
    )

    descriptor = _valid_bash_descriptor()
    descriptor["working_directory"] = working_directory

    with pytest.raises(CommandRedactionError):
        validate_action_descriptor("bash", descriptor)


@pytest.mark.parametrize("working_directory", [None, "/tmp/work"])
@pytest.mark.parametrize("timeout_seconds", [1, 120, 600, 12.5])
def test_bash_descriptor_accepts_valid_bounded_values(
    working_directory: str | None,
    timeout_seconds: int | float,
) -> None:
    from host_access.redaction import validate_action_descriptor

    descriptor = _valid_bash_descriptor()
    descriptor["working_directory"] = working_directory
    descriptor["timeout_seconds"] = timeout_seconds

    validate_action_descriptor("bash", descriptor)
