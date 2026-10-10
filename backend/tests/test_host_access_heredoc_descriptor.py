"""Here-document bodies reach the host-access reviewer as written (#2025)."""

from __future__ import annotations

import hashlib
import json
import os
from types import SimpleNamespace

import pytest

_SCRIPT = (
    "import sqlite3\n"
    "db = sqlite3.connect('x.db')\n"
    "rows = db.execute(\"select id from t where kind = 'a'\")\n"
    "if rows and mode == \"text\":\n"
    "    print('ok')"
)

# (body line, hidden value, the line as shown, or None for the token view).
# A ``$`` value is a mediated reference; every other value is a literal.
_CREDENTIAL_LINES = [
    (
        'curl -H "Authorization: Bearer fake-hd-01" https://example.test',
        "fake-hd-01",
        'curl -H "Authorization: Bearer [REDACTED]" https://example.test',
    ),
    (
        "curl -H 'Cookie: session=fake-hd-02' https://example.test",
        "fake-hd-02",
        "curl -H 'Cookie: [REDACTED]' https://example.test",
    ),
    (
        "http https://example.test Authorization: Bearer fake-hd-03",
        "fake-hd-03",
        "http https://example.test Authorization: Bearer [REDACTED]",
    ),
    (
        "git clone https://alice:fake-hd-04@example.test/repo.git",
        "fake-hd-04",
        "git clone https://alice:[REDACTED]@example.test/repo.git",
    ),
    (
        'curl "https://alice:fake-hd-05@example.test/x"',
        "fake-hd-05",
        'curl "https://alice:[REDACTED]@example.test/x"',
    ),
    (
        "curl 'https://example.test/api?access_token=fake-hd-06'",
        "fake-hd-06",
        "curl 'https://example.test/api?access_token=[REDACTED]'",
    ),
    # The shared classifier re-flags its own redacted JSON rendering, so this
    # form keeps the token view (as its token descriptor always has).
    (
        """curl --data '{"password":"fake-hd-07"}' https://example.test""",
        "fake-hd-07",
        None,
    ),
    (
        "PGPASSWORD=fake-hd-08 psql app",
        "fake-hd-08",
        "PGPASSWORD=[REDACTED] psql app",
    ),
    (
        'export API_TOKEN="fake-hd-09"',
        "fake-hd-09",
        'export API_TOKEN="[REDACTED]"',
    ),
    (
        "tool --password fake-hd-10",
        "fake-hd-10",
        "tool --password [REDACTED]",
    ),
    (
        "tool --password='fake-hd-11'",
        "fake-hd-11",
        "tool --password='[REDACTED]'",
    ),
    ("mysql -pfake-hd-12 app", "fake-hd-12", "mysql -p[REDACTED] app"),
    ("mysql -p 'fake-hd-13' app", "fake-hd-13", "mysql -p '[REDACTED]' app"),
    (
        'curl -u "alice:fake-hd-14" https://example.test',
        "fake-hd-14",
        'curl -u "alice:[REDACTED]" https://example.test',
    ),
    (
        "printf 'fake-hd-15' | docker login --password-stdin registry.example",
        "fake-hd-15",
        "printf '[REDACTED]' | docker login --password-stdin registry.example",
    ),
    (
        "gh auth login --with-token <<< fake-hd-16",
        "fake-hd-16",
        "gh auth login --with-token <<< [REDACTED]",
    ),
    (
        "security add-generic-password -a me -s app -w fake-hd-17",
        "fake-hd-17",
        "security add-generic-password -a me -s app -w [REDACTED]",
    ),
    ("printf 'token=fake-hd-18'", "fake-hd-18", "printf 'token=[REDACTED]'"),
    (
        'curl -H "Authorization: Bearer $API_TOKEN" https://example.test',
        "$API_TOKEN",
        'curl -H "Authorization: Bearer [REDACTED]" https://example.test',
    ),
    (
        'export API_TOKEN="$VAULT_TOKEN"',
        "$VAULT_TOKEN",
        'export API_TOKEN="[REDACTED]"',
    ),
    (
        'git clone "https://alice:$GIT_PASSWORD@example.test/r.git"',
        "$GIT_PASSWORD",
        'git clone "https://alice:[REDACTED]@example.test/r.git"',
    ),
    ('mysql -p"$DB_PASSWORD" app', "$DB_PASSWORD", 'mysql -p"[REDACTED]" app'),
    (
        'curl --user "alice:$PASSWORD" https://example.test',
        "$PASSWORD",
        'curl --user "alice:[REDACTED]" https://example.test',
    ),
]


def _bash_descriptor(command: dict[str, object]) -> dict[str, object]:
    return {
        "version": 1,
        "tool": "bash",
        "command": command,
        "working_directory": "/tmp/work",
        "timeout_seconds": 120,
        "background": False,
    }


def test_issue_2025_script_body_keeps_its_quoting() -> None:
    from host_access.redaction import (
        redact_shell_command,
        validate_action_descriptor,
    )

    command = f"python3 - <<'PY'\n{_SCRIPT}\nPY"
    descriptor = redact_shell_command(command).descriptor()

    assert descriptor == {
        "tokens": ["python3", "-", "<<", "PY"],
        "redactions": 0,
        "literal_credentials": 0,
        "heredocs": [
            {"delimiter": "PY", "quoted_delimiter": True, "body": _SCRIPT},
        ],
    }
    validate_action_descriptor("bash", _bash_descriptor(descriptor))


def test_shell_text_around_the_body_stays_tokenized() -> None:
    from host_access.redaction import redact_shell_command

    command = (
        "cd /tmp/work && python3 - <<-PY\n\tprint('a')\n\tPY\necho done"
    )
    descriptor = redact_shell_command(command).descriptor()

    assert descriptor["tokens"] == [
        "cd", "/tmp/work", "&&", "python3", "-", "<<", "-PY", ";", "echo",
        "done",
    ]
    assert descriptor["heredocs"] == [
        {"delimiter": "PY", "quoted_delimiter": False, "body": "\tprint('a')"},
    ]


def test_subshell_body_keeps_its_closing_parenthesis_as_data() -> None:
    from host_access.redaction import redact_shell_command

    command = "( cat <<'EOF'\nharmless )\necho data\nEOF\n)"
    descriptor = redact_shell_command(command).descriptor()

    assert descriptor["heredocs"] == [
        {
            "delimiter": "EOF",
            "quoted_delimiter": True,
            "body": "harmless )\necho data",
        },
    ]


def test_each_heredoc_on_a_line_keeps_its_own_body() -> None:
    from host_access.redaction import redact_shell_command

    command = "cat <<A <<'B'\none 'x'\nA\ntwo \"y\"\nB"
    descriptor = redact_shell_command(command).descriptor()

    assert descriptor["tokens"] == ["cat", "<<", "A", "<<", "B"]
    assert descriptor["heredocs"] == [
        {"delimiter": "A", "quoted_delimiter": False, "body": "one 'x'"},
        {"delimiter": "B", "quoted_delimiter": True, "body": 'two "y"'},
    ]


@pytest.mark.parametrize(
    "command",
    [
        "cat <<EOF\nhello 'x'\n",
        'x=$(cat <<EOF\nprint("a")\nEOF\n)',
        'x=`cat <<EOF\nprint("a")\nEOF\n`',
        "cat <(cat <<'EOF'\nharmless )\necho smuggled\nEOF\n)",
        "a=(x <<'EOF'\nharmless )\necho smuggled\nEOF\n)",
        (
            "shopt -s expand_aliases; alias q='cat <(cat '\n"
            "q <<'EOF'\nharmless )\necho smuggled\nEOF\n)"
        ),
        "set -e\npython3 - <<'PY'\nprint('a')\nPY",
        "cat <<\"E-O-F\"\nprint('a')\nE-O-F",
        "cat <<EOF\nline \\\nEOF\nEOF",
        "cat <<EOF\n\tEOF\nEOF",
        "echo hi # <<EOF\necho \"a b\"",
        "cat <<< \"a b\"",
        "python3 -c \"import x; print('a')\"",
    ],
    ids=[
        "unterminated",
        "command-substitution",
        "backquote",
        "process-substitution",
        "array-assignment",
        "alias-from-an-earlier-line",
        "operator-after-the-first-line",
        "non-identifier-delimiter",
        "escaped-newline-in-expanding-body",
        "body-holds-tab-indented-terminator",
        "operator-in-comment",
        "here-string",
        "inline-script",
    ],
)
def test_inexact_or_absent_heredocs_keep_the_token_view(command: str) -> None:
    from host_access.redaction import redact_shell_command
    from host_access.shell_view import shell_words

    descriptor = redact_shell_command(command).descriptor()

    assert descriptor == {
        "tokens": list(shell_words(command)),
        "redactions": 0,
        "literal_credentials": 0,
    }


def test_literal_credential_in_shell_text_is_still_classified() -> None:
    from host_access.redaction import redact_shell_command
    from host_access.refusal_messages import LITERAL_CREDENTIAL_REFUSAL
    from tools.shell_guard import check_command_safety

    command = "PGPASSWORD=fake-hd-30 psql app <<'SQL'\nselect 'a';\nSQL"
    result = redact_shell_command(command)

    assert result.descriptor() == {
        "tokens": ["PGPASSWORD=[REDACTED]", "psql", "app", "<<", "SQL"],
        "redactions": 1,
        "literal_credentials": 1,
        "heredocs": [
            {"delimiter": "SQL", "quoted_delimiter": True, "body": "select 'a';"},
        ],
    }
    assert check_command_safety(command) == (
        f"Error: {LITERAL_CREDENTIAL_REFUSAL}"
    )


def test_body_with_unbalanced_shell_quoting_still_fails_closed() -> None:
    from host_access.redaction import CommandRedactionError, redact_shell_command
    from tools.shell_guard import check_command_safety

    command = "python3 - <<'PY'\n# don't\nprint(1)\nPY"

    with pytest.raises(CommandRedactionError, match="unparseable"):
        redact_shell_command(command)
    assert check_command_safety(command) is not None


@pytest.mark.parametrize("delimiter", ["'EOF'", "EOF"])
@pytest.mark.parametrize(("line", "hidden", "shown"), _CREDENTIAL_LINES)
def test_credentials_inside_bodies_stay_redacted_and_classified(
    delimiter: str,
    line: str,
    hidden: str,
    shown: str | None,
) -> None:
    from host_access.redaction import (
        redact_shell_command,
        validate_action_descriptor,
    )
    from host_access.refusal_messages import LITERAL_CREDENTIAL_REFUSAL
    from tools.shell_guard import check_command_safety

    command = f"bash <<{delimiter}\nset -e\n{line}\necho done\nEOF"
    result = redact_shell_command(command)
    descriptor = result.descriptor()

    assert hidden not in json.dumps(descriptor)
    assert result.redactions >= 1
    if shown is None:
        assert "heredocs" not in descriptor
    else:
        assert descriptor["heredocs"][0]["body"] == (
            f"set -e\n{shown}\necho done"
        )
    guard = check_command_safety(command)
    if hidden.startswith("$"):
        assert result.literal_credentials == 0
        assert guard is None
        validate_action_descriptor("bash", _bash_descriptor(descriptor))
    else:
        assert result.literal_credentials >= 1
        assert guard == f"Error: {LITERAL_CREDENTIAL_REFUSAL}"


_PY_HEREDOC = {"delimiter": "PY", "quoted_delimiter": True, "body": "print('a')"}


def _heredoc_command(**heredoc: object) -> dict[str, object]:
    item: dict[str, object] = {**_PY_HEREDOC, **heredoc}
    return {
        "tokens": ["python3", "-", "<<", "PY"],
        "redactions": 0,
        "literal_credentials": 0,
        "heredocs": [item],
    }


@pytest.mark.parametrize(
    "command",
    [
        {"tokens": ["ps", "-ef"], "redactions": 0, "literal_credentials": 0},
        _heredoc_command(),
        _heredoc_command(quoted_delimiter=False),
        {
            **_heredoc_command(body='curl -u "alice:[REDACTED]" x'),
            "redactions": 1,
        },
    ],
    ids=["without-heredocs", "quoted", "unquoted", "redacted-body"],
)
def test_validator_accepts_canonical_command_descriptors(command) -> None:
    from host_access.redaction import validate_action_descriptor

    validate_action_descriptor("bash", _bash_descriptor(command))


@pytest.mark.parametrize(
    "command",
    [
        {**_heredoc_command(), "heredocs": []},
        {**_heredoc_command(), "heredocs": "print('a')"},
        {**_heredoc_command(), "heredocs": [_PY_HEREDOC] * 17},
        {**_heredoc_command(), "heredocs": [{"delimiter": "PY", "body": "x"}]},
        _heredoc_command(extra=True),
        _heredoc_command(delimiter="E-O-F"),
        _heredoc_command(quoted_delimiter="yes"),
        _heredoc_command(body=5),
        _heredoc_command(body="a\x00b"),
        _heredoc_command(body="a" * 16_001),
        _heredoc_command(body="a\nPY\nrm -rf /tmp/work"),
        _heredoc_command(body="a\n\tPY\nb"),
        _heredoc_command(
            body='curl -H "Authorization: Bearer fake-hd-40" https://example.test'
        ),
        _heredoc_command(body='curl -u "alice:[REDACTED]" x'),
        _heredoc_command(body="# don't"),
    ],
    ids=[
        "empty-list",
        "not-a-list",
        "too-many",
        "missing-key",
        "extra-key",
        "non-identifier-delimiter",
        "non-boolean-quoting",
        "non-text-body",
        "nul-in-body",
        "oversized-body",
        "body-holds-terminator",
        "body-holds-tab-terminator",
        "raw-credential-in-body",
        "uncounted-marker",
        "unparseable-body",
    ],
)
def test_validator_rejects_invalid_heredocs(command) -> None:
    from host_access.redaction import (
        CommandRedactionError,
        validate_action_descriptor,
    )

    with pytest.raises(CommandRedactionError):
        validate_action_descriptor("bash", _bash_descriptor(command))


@pytest.mark.asyncio
async def test_action_hash_binds_the_raw_heredoc_command(
    tmp_path,
    monkeypatch,
):
    from tools import host_access_gate as gate

    lease = {
        "id": "lease-1",
        "revision": 1,
        "application_hash": "a" * 64,
        "state": "active",
    }

    async def active(_thread_id):
        return lease

    reviews: list[dict] = []

    async def review(_lease_id, **kwargs):
        reviews.append(kwargs)
        return {
            "lease": lease,
            "review": {
                "lease_id": "lease-1",
                "lease_revision": 1,
                "thread_id": kwargs["thread_id"],
                "tool_name": kwargs["tool_name"],
                "action_hash": kwargs["action_hash"],
                "verdict": "allow",
                "permit_id": "permit-1",
                "permit_expires_at": "2026-07-18T12:00:10+00:00",
            },
        }

    monkeypatch.setattr(gate, "_resolve_active_lease", active)
    monkeypatch.setattr(gate, "_review_host_action", review)
    tool = SimpleNamespace(
        _workspace=None,
        _allowed_dir=None,
        working_dir=str(tmp_path),
        default_timeout=120,
    )
    commands = [
        f"bash <<'EOF'\nprint('x') and curl -u \"alice:${name}\" x\nEOF"
        for name in ("FIRST_SECRET", "SECOND_SECRET")
    ]
    for command in commands:
        await gate.authorize_host_action(
            thread_id="thread-1",
            project_id=None,
            tool=tool,
            tool_name="bash",
            params={"command": command, "host": True},
        )

    first, second = reviews
    assert first["redacted_descriptor"] == second["redacted_descriptor"]
    assert first["redacted_descriptor"]["command"]["heredocs"][0]["body"] == (
        "print('x') and curl -u \"alice:[REDACTED]\" x"
    )
    status = os.stat(tmp_path.resolve(), follow_symlinks=False)
    for command, captured in zip(commands, reviews):
        binding = {
            "version": 1,
            "tool": "bash",
            "command": command,
            "working_directory": str(tmp_path.resolve()),
            "timeout_seconds": 120,
            "background": False,
            "working_directory_identity": {
                "device": status.st_dev,
                "inode": status.st_ino,
            },
            "host": True,
        }
        encoded = json.dumps(
            binding,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        assert captured["action_hash"] == hashlib.sha256(
            encoded.encode("utf-8")
        ).hexdigest()
    assert first["action_hash"] != second["action_hash"]
