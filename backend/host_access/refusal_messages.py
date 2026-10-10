"""Explanations for refused host actions and failed host-access reviews.

A refusal that says only *that* an action was refused leaves the agent to
guess why, and the guess usually reads as a missing permission it then tries
to fix with lease requests. Each holder-visible message here names the cause
and the next step, and none widens what a holder may learn:

- the command redactor's causes are fixed literals describing the command's
  shape, never its text (a test pins every raise site to a literal);
- a descriptor that cannot be encoded is reported by exception type only,
  because the standard library's text can quote the offending value; and
- the action reviewer's stated reason is model-written from the redacted
  action descriptor and the holder's own application, so it is flattened to
  one bounded, printable line before it is shown.

Lease terminal reasons are a separate durable surface with an enumerated
gate; see ``host_access.failure_reasons``.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import re
import traceback
from typing import Any
import unicodedata

from host_access.credential_sanitizer import (
    REDACTION_MARKER,
    sanitize_credential_text,
)
from host_access.redaction import CommandRedactionError

REVIEWER_REASON_LIMIT = 600

# Bare keys in provider error text ("Incorrect API key provided: sk-...")
# carry no credential-named prefix for the shared sanitizer to anchor on, so
# the server log also drops long opaque runs that contain a digit.
_OPAQUE_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?:AKIA[0-9A-Z]{16}"
    r"|(?=[A-Za-z_-]*[0-9])[A-Za-z0-9_-]{24,})(?![A-Za-z0-9_-])"
)

LITERAL_CREDENTIAL_REFUSAL = (
    "Command blocked by safety guard: it passes a literal value where a "
    "credential goes (a credential-named flag, key, header, or URL field). "
    "Nothing ran. This is not a permission problem: host access never "
    "authorizes a literal credential. Reference the value instead through an "
    "environment variable, a Keychain lookup, a connector, or an "
    "agent-mediated value."
)

_VERDICT_NEXT_STEPS = {
    "block": (
        "Your host-access lease is still active. Change the action to address "
        "that reason before retrying; an unchanged retry is reviewed again "
        "against the same application."
    ),
    "revoke": (
        "Your host-access lease was revoked, so no further host-scope action "
        "can run under it. Apply for a new lease only if the work still needs "
        "host scope within your authorized task."
    ),
}


def single_line(text: str, limit: int) -> str:
    """Collapse text to one printable line of at most ``limit`` characters.

    Control and format characters (including bidirectional overrides and
    zero-width marks) become spaces before whitespace is collapsed, so the
    result can neither span lines nor disguise what it says.
    """
    printable = "".join(
        " " if unicodedata.category(char).startswith("C") else char
        for char in text
    )
    flattened = " ".join(printable.split())
    if len(flattened) > limit:
        return flattened[: limit - 1] + "…"
    return flattened


# Keyed by the redactor's literal causes; a test keeps every key live. A cause
# without an entry falls back to the generic fix rather than a wrong one.
_NORMALIZATION_FIXES = {
    "URL identity is invalid": (
        "an argument containing '://' must be a complete URL with a scheme "
        "and host; keep '://' out of other text"
    ),
    "structured credential input is invalid": (
        "an argument that starts with '{' or '[' and mentions a credential "
        "name (such as token) is read as JSON and must be valid JSON; fix the "
        "JSON, or reword text that is not JSON so it does not start with a "
        "bracket"
    ),
    "credential expansion cannot be safely represented": (
        "a command that references a credential cannot also use $'...', "
        '$"...", or backticks'
    ),
    "credential parameter expansion cannot be safely represented": (
        "a command that references a credential may use only plain ${NAME} "
        "parameter expansions"
    ),
    "credential command substitution cannot be safely represented": (
        "a command that references a credential may use $(...) only for a "
        "Keychain lookup"
    ),
    "command text is unsafe": (
        "remove NUL bytes and any [REDACTED] marker copied from redacted "
        "output"
    ),
    "command text is unparseable": "balance its quotes and escapes",
}


def normalization_refusal(exc: CommandRedactionError) -> str:
    """Explain a Bash command the redactor could not normalize for review."""
    cause = str(exc)
    fix = _NORMALIZATION_FIXES.get(cause, "rewrite the part of the command it names")
    return (
        f"Bash command could not be safely normalized: {cause}. Nothing ran. "
        f"To fix: {fix}, then retry. This checks the command's shape, not "
        "your permissions; host access does not change it."
    )


def protected_scope_denial(reason: str, *, lease_active: bool) -> str:
    """Return state-aware next-step guidance for a protected-scope refusal."""
    preamble = (
        f"Blocked by protected scope: {reason}.\n"
        "First check for a protected-scope way to reach the goal — a "
        "registered repo, a `.worktrees/` worktree, or a workspace path "
        "usually covers it, and it's faster than review."
    )
    if lease_active:
        return (
            f"{preamble}\n"
            "If this exact action genuinely needs host scope, retry with "
            "`host: true` — it gets one independent review under your active "
            "lease.\n"
            "Never use host scope to route around a repository policy (branch "
            "pin / protected base); your lease must carry the matching policy "
            "exception."
        )
    return (
        f"{preamble}\n"
        "If this exact action genuinely needs host scope, request a "
        "host-access lease (`request_host_access`), then retry with "
        "`host: true`.\n"
        "Never use host scope to route around a repository policy (branch pin "
        "/ protected base); request the matching policy exception in the lease "
        "application instead."
    )


def safehouse_unavailable_refusal(backend_path: str | None) -> str:
    """Explain Safehouse discovery failure from the backend process."""
    searched_path = single_line(backend_path or "<unset>", REVIEWER_REASON_LIMIT)
    return (
        "Agent Safehouse was not found on the backend PATH: "
        f"{searched_path}. Nothing ran. If it is not installed, install Agent "
        "Safehouse. If it is already installed, add its directory to the "
        "CORSIA service PATH and run `corsia service restart`."
    )


def descriptor_refusal(exc: BaseException) -> str:
    """Explain a host action whose parameters could not be encoded for review.

    Only the exception type is interpreted: the standard library's message
    can quote the offending character or value.
    """
    if isinstance(exc, UnicodeError):
        cause = "a text parameter is not valid Unicode (such as a lone surrogate)"
    elif isinstance(exc, TypeError):
        cause = "a parameter has a value type that cannot be recorded"
    else:
        cause = "a parameter value cannot be recorded (such as a non-finite number)"
    return (
        f"Host action descriptor could not be safely normalized: {cause}. "
        "Nothing ran; correct that parameter, then retry."
    )


def reviewer_refusal(review: Mapping[str, Any]) -> str:
    """Explain an action the independent reviewer did not allow.

    Names the verdict, quotes the reviewer's stated reason as one bounded
    line, and says what the verdict means for the holder's lease.
    """
    verdict = review.get("verdict")
    known = verdict if verdict in ("block", "revoke") else None
    heading = "Host action was blocked by independent safety review"
    if known is not None:
        heading += f" (verdict: {known})"
    raw_reason = review.get("reason")
    reason = (
        single_line(raw_reason, REVIEWER_REASON_LIMIT)
        if isinstance(raw_reason, str)
        else ""
    )
    stated = (
        f'Reviewer\'s stated reason: "{reason}".'
        if reason
        else "The reviewer stated no reason."
    )
    message = f"{heading}. {stated}"
    if known is not None:
        message += f" {_VERDICT_NEXT_STEPS[known]}"
    return message


def exception_chain_summary(exc: BaseException, *, limit: int = 300) -> str:
    """Describe an exception and everything it chains to, for the server log.

    Each link gives its type, its credential-sanitized one-line message, and
    the frame that raised it, outermost first. No traceback is rendered: the
    default log sink prints frame locals, which can hold credentials. Never
    hand this to a holder; ``failure_reasons`` owns what a holder may see.
    """
    links: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        text = single_line(
            _OPAQUE_TOKEN_RE.sub(
                REDACTION_MARKER, sanitize_credential_text(str(current))
            ),
            limit,
        )
        frames = traceback.extract_tb(current.__traceback__)
        where = (
            f" at {Path(frames[-1].filename).name}:{frames[-1].lineno}"
            f" in {frames[-1].name}"
            if frames
            else ""
        )
        links.append(f"{type(current).__name__}: {text or 'no detail'}{where}")
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
    return " <- ".join(links)
