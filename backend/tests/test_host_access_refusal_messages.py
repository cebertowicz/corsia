"""Refused host actions tell the agent the cause and the next step (#1757).

The messages may surface only what is safe to hand the holder: redactor
causes that are fixed literals, exception types instead of standard-library
text, and the reviewer's stated reason flattened to one bounded line. Every
message must also survive the registry's credential sanitizer unchanged, or
the agent would receive a mangled instruction.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from host_access.credential_sanitizer import sanitize_credential_text
from host_access.redaction import CommandRedactionError
from host_access.refusal_messages import (
    LITERAL_CREDENTIAL_REFUSAL,
    REVIEWER_REASON_LIMIT,
    _NORMALIZATION_FIXES,
    descriptor_refusal,
    exception_chain_summary,
    normalization_refusal,
    reviewer_refusal,
    safehouse_unavailable_refusal,
    single_line,
)

_BACKEND = Path(__file__).resolve().parents[1]
_SCHEME_ONLY = "abc:" + "//"


def _redaction_error_calls() -> list[tuple[Path, ast.Call]]:
    calls: list[tuple[Path, ast.Call]] = []
    for path in _BACKEND.rglob("*.py"):
        relative = path.relative_to(_BACKEND)
        if relative.parts[0] == "tests":
            continue
        source = path.read_text(encoding="utf-8")
        if "CommandRedactionError(" not in source:
            continue
        for node in ast.walk(ast.parse(source)):
            func = getattr(node, "func", None)
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if isinstance(node, ast.Call) and name == "CommandRedactionError":
                calls.append((relative, node))
    return calls


def _literal_causes() -> set[str]:
    return {
        literal.value
        for _path, call in _redaction_error_calls()
        for literal in call.args[:1]
        if isinstance(literal, ast.Constant) and isinstance(literal.value, str)
    }


def test_every_redactor_cause_is_a_fixed_literal():
    """Surfacing ``str(exc)`` is safe only while no cause interpolates input.

    A future ``CommandRedactionError(f"... {token}")`` could hand a credential
    straight back to the holder, so this fails the moment one appears.
    """
    calls = _redaction_error_calls()
    assert len(calls) >= 20
    for path, call in calls:
        assert not call.keywords, f"{path}:{call.lineno}"
        assert len(call.args) == 1, f"{path}:{call.lineno}"
        literal = call.args[0]
        assert isinstance(literal, ast.Constant), f"{path}:{call.lineno}"
        assert isinstance(literal.value, str), f"{path}:{call.lineno}"


def test_every_specific_fix_is_keyed_to_a_real_cause():
    assert set(_NORMALIZATION_FIXES) <= _literal_causes()


@pytest.mark.parametrize(
    ("command", "cause", "fix"),
    [
        (f"echo '{_SCHEME_ONLY}'", "URL identity is invalid", "complete URL"),
        (
            'echo "[bug] a token here"',
            "structured credential input is invalid",
            "must be valid JSON",
        ),
        (
            "curl --user 'unterminated",
            "command text is unparseable",
            "balance its quotes",
        ),
    ],
)
def test_guard_names_the_cause_and_its_fix(command, cause, fix):
    from tools.shell_guard import check_command_safety

    message = check_command_safety(command)

    assert message is not None
    assert message.startswith("Error: Bash command could not be safely normalized")
    assert f": {cause}. Nothing ran." in message
    assert fix in message
    assert "host access does not change it" in message


def test_different_causes_give_different_messages():
    """The issue's success signal: two causes are no longer one string."""
    from tools.shell_guard import check_command_safety

    first = check_command_safety(f"echo '{_SCHEME_ONLY}'")
    second = check_command_safety('echo "[bug] a token here"')

    assert first and second and first != second


@pytest.mark.parametrize(
    ("backend_path", "expected_path"),
    [
        ("/opt/homebrew/bin:/usr/bin:/bin", "/opt/homebrew/bin:/usr/bin:/bin"),
        (None, "<unset>"),
    ],
)
def test_safehouse_unavailable_refusal_names_discovery_and_both_recoveries(
    backend_path,
    expected_path,
):
    message = safehouse_unavailable_refusal(backend_path)

    assert f"backend PATH: {expected_path}" in message
    assert "install Agent Safehouse" in message
    assert "already installed" in message
    assert "CORSIA service PATH" in message
    assert "Nothing ran" in message
    assert sanitize_credential_text(message) == message


def test_unmapped_cause_falls_back_to_a_generic_fix():
    message = normalization_refusal(CommandRedactionError("command text is empty"))

    assert "command text is empty" in message
    assert "rewrite the part of the command it names" in message


def test_literal_credential_refusal_says_what_to_do_instead():
    from tools.shell_guard import check_command_safety

    message = check_command_safety("tool --password=hunter2")

    assert message == f"Error: {LITERAL_CREDENTIAL_REFUSAL}"
    assert "hunter2" not in message
    assert "not a permission problem" in message
    assert "environment variable" in message
    # The existing callers and tests key on this phrase.
    assert "literal credential" in message


def test_descriptor_refusal_reports_the_type_never_the_value():
    with pytest.raises(UnicodeError) as raised:
        "bad \ud800 text".encode("utf-8")
    messages = {
        descriptor_refusal(raised.value),
        descriptor_refusal(TypeError("Object of type bytes is not JSON serializable")),
        descriptor_refusal(
            ValueError("Out of range float values are not JSON compliant: nan")
        ),
    }

    assert len(messages) == 3
    for message in messages:
        assert message.startswith("Host action descriptor could not be safely")
        assert "Nothing ran" in message
        for leaked in ("\\ud800", "surrogates not allowed", "bytes", "compliant"):
            assert leaked not in message


def test_block_verdict_carries_the_reason_and_the_live_lease():
    message = reviewer_refusal(
        {"verdict": "block", "reason": "Writes outside the approved repository."}
    )

    assert message.startswith(
        "Host action was blocked by independent safety review (verdict: block)."
    )
    assert (
        "Reviewer's stated reason: \"Writes outside the approved repository.\""
        in message
    )
    assert "lease is still active" in message


def test_revoke_verdict_says_the_lease_ended():
    message = reviewer_refusal(
        {"verdict": "revoke", "reason": "Purpose expanded beyond the application."}
    )

    assert "(verdict: revoke)" in message
    assert "Purpose expanded beyond the application." in message
    assert "was revoked" in message
    assert "still active" not in message


def test_unknown_verdict_makes_no_lease_claim_and_missing_reason_is_named():
    message = reviewer_refusal({"verdict": ["allow"], "reason": None})

    assert message == (
        "Host action was blocked by independent safety review. "
        "The reviewer stated no reason."
    )


def test_reviewer_reason_is_one_bounded_printable_line():
    reason = (
        "first line\nsecond\r\nline\x1b[31m red\x00 nul ‮"
        "reversed​" + "x" * 5000
    )

    message = reviewer_refusal({"verdict": "block", "reason": reason})
    stated = message.split('Reviewer\'s stated reason: "', 1)[1].split('".', 1)[0]

    assert "first line second line [31m red nul reversed" in stated
    assert len(stated) == REVIEWER_REASON_LIMIT
    assert stated.endswith("…")
    for char in ("\n", "\r", "\x1b", "\x00", "‮", "​"):
        assert char not in message


def test_single_line_keeps_short_text_and_drops_blank_text():
    assert single_line("  plain   text ", 50) == "plain text"
    assert single_line("\n\t\x00", 50) == ""


@pytest.mark.parametrize(
    "message",
    [
        LITERAL_CREDENTIAL_REFUSAL,
        *(
            normalization_refusal(CommandRedactionError(cause))
            for cause in _NORMALIZATION_FIXES
        ),
        descriptor_refusal(TypeError()),
        reviewer_refusal({"verdict": "block", "reason": "Out of scope."}),
        reviewer_refusal({"verdict": "revoke", "reason": "Out of scope."}),
    ],
)
def test_messages_reach_the_agent_unmangled(message):
    """The registry sanitizes every tool error; our own text must pass intact."""
    assert sanitize_credential_text(message) == message


def _provider_call():
    raise ValueError("auth failed: password=hunter2\nretry later")


def _review_step():
    try:
        _provider_call()
    except ValueError as exc:
        raise RuntimeError("application review failed") from exc


def test_exception_chain_summary_names_every_link_without_secrets():
    with pytest.raises(RuntimeError) as raised:
        _review_step()

    summary = exception_chain_summary(raised.value)

    outer, inner = summary.split(" <- ")
    assert outer.startswith("RuntimeError: application review failed at ")
    assert outer.endswith(" in _review_step")
    assert inner.startswith("ValueError: auth failed: password=[REDACTED] retry")
    assert inner.endswith(" in _provider_call")
    assert "hunter2" not in summary
    assert "\n" not in summary


@pytest.mark.parametrize(
    "key",
    [
        "sk-proj-" + "A1b2C3d4E5f6G7h8I9j0K1l2",
        "sk-ant-api03-" + "Zx9" * 12,
        "ghp_" + "a1" * 18,
        "AKIA" + "ABCDEFGH23456789",
        "hf_" + "Q7" * 15,
    ],
)
def test_exception_chain_summary_drops_bare_provider_keys(key):
    exc = RuntimeError(f"Incorrect API key provided: {key}. Check the key.")

    summary = exception_chain_summary(exc)

    assert key not in summary
    assert summary == (
        "RuntimeError: Incorrect API key provided: [REDACTED]. Check the key."
    )


def test_exception_chain_summary_keeps_readable_identifiers():
    exc = ValueError("record_application_review_validation_failed for lease")

    assert exception_chain_summary(exc) == (
        "ValueError: record_application_review_validation_failed for lease"
    )


def test_exception_chain_summary_follows_context_and_stops_on_cycles():
    def handle_badly():
        try:
            raise KeyError("missing")
        except KeyError:
            raise OSError("while handling")

    with pytest.raises(OSError) as raised:
        handle_badly()
    implicit = raised.value
    looped = RuntimeError("loop")
    looped.__cause__ = looped
    suppressed = RuntimeError("outer")
    suppressed.__context__ = ValueError("hidden")
    suppressed.__suppress_context__ = True

    assert "OSError: while handling" in exception_chain_summary(implicit)
    assert "KeyError: 'missing'" in exception_chain_summary(implicit)
    assert exception_chain_summary(looped) == "RuntimeError: loop"
    assert exception_chain_summary(suppressed) == "RuntimeError: outer"
    assert exception_chain_summary(RuntimeError()) == "RuntimeError: no detail"
