"""Here-document bodies in the reviewer's view of a Bash command.

The token redactor passed in as ``redact`` (``host_access.redaction``) stays
the only credential classifier. This module feeds it a body's words, line by
line, and builds the view that shows each body as written.
"""

from __future__ import annotations

from typing import Callable, Sequence

from host_access.shell_credential_positions import plain_word_indices
from host_access.shell_view import ShellCommandRedaction, heredoc_view
from host_access.shell_view import shell_words, split_heredocs

TokenRedactor = Callable[
    [Sequence[str], "frozenset[int] | None"], ShellCommandRedaction
]
_QUOTE_MARKS = "'\"`"


def _line_words(line: str) -> tuple[tuple[str, ...], frozenset[int]]:
    """A body line's shell words, or its whitespace words if shlex cannot split it.

    A body is data, so prose with an unbalanced quote (``it's``) must not
    stop the scan. Quote marks are dropped from the ends of fallback words so
    a quoted credential label still reads as that label.
    """
    try:
        words = shell_words(line)
    except ValueError:
        return (
            tuple(word.strip(_QUOTE_MARKS) or word for word in line.split()),
            frozenset(),
        )
    return words, plain_word_indices(line, words)


def redact_body(
    body: str, redact: TokenRedactor
) -> tuple[tuple[str, ...], ShellCommandRedaction]:
    """Return a body's raw words and their redaction, scanned line by line."""
    raw: list[str] = []
    safe: list[str] = []
    redactions = literals = 0
    for line in body.split("\n"):
        words, plain = _line_words(line)
        if not words:
            continue
        item = redact(words, plain)
        raw.extend(words)
        safe.extend(item.tokens)
        redactions += item.redactions
        literals += item.literal_credentials
    return tuple(raw), ShellCommandRedaction(tuple(safe), redactions, literals)


def redacted_body(
    body: str, redact: TokenRedactor
) -> ShellCommandRedaction | None:
    """Redact a body's words, or ``None`` if it cannot be."""
    try:
        return redact_body(body, redact)[1]
    except ValueError:
        return None


def redacted_words(
    text: str, redact: TokenRedactor
) -> ShellCommandRedaction | None:
    """Redact the shell words of ``text``, or ``None`` if it cannot be."""
    try:
        words = shell_words(text)
        return redact(words, plain_word_indices(text, words))
    except ValueError:
        return None


def verbatim_heredocs(
    command: str,
    tokens: Sequence[str],
    redacted: ShellCommandRedaction,
    redact: TokenRedactor,
    marker: str,
) -> ShellCommandRedaction:
    """Show here-document bodies as written instead of shell-tokenized.

    ``redacted`` remains the only credential classifier. The verbatim view is
    used only when every shown part re-tokenizes to a redaction fixed point
    that retains no raw token the classifier replaced; otherwise the token
    view stands.
    """
    view = heredoc_view(command, tokens, redacted.tokens, marker)
    if view is None:
        return redacted
    shell_text, heredocs, hidden = view
    bodies = [item.body for item in heredocs]
    shell = redacted_words(shell_text, redact)
    shown = [redacted_body(body, redact) for body in bodies]
    clean = [item for item in shown if item is not None and not item.redactions]
    if shell is None or len(clean) != len(shown):
        return redacted
    words = [word for item in clean for word in item.tokens]
    texts = [*shell.tokens, *words, *bodies]
    if any(value in text for value in hidden for text in texts):
        return redacted
    return ShellCommandRedaction(
        tokens=shell.tokens,
        redactions=sum(
            text.count(marker) for text in [*shell.tokens, *bodies]
        ),
        literal_credentials=redacted.literal_credentials,
        heredocs=heredocs,
    )


def redact_split_command(
    command: str, redact: TokenRedactor, marker: str
) -> ShellCommandRedaction | None:
    """Redact a command whose here-document bodies shlex cannot split.

    The shell part is tokenized strictly and each body is scanned line by
    line. Returns the verbatim here-document view, or the line-scanned token
    view when that cannot be built (a body holding a credential), or
    ``None`` when the command has no exactly split here-document.
    """
    split = split_heredocs(command)
    if split is None:
        return None
    shell_text, heredocs = split
    try:
        tokens = shell_words(shell_text)
    except ValueError:
        return None
    shell = redact(tokens, plain_word_indices(shell_text, tokens))
    raw, safe = list(tokens), list(shell.tokens)
    redactions, literals = shell.redactions, shell.literal_credentials
    for heredoc in heredocs:
        words, body = redact_body(heredoc.body, redact)
        raw.extend(words)
        safe.extend(body.tokens)
        redactions += body.redactions
        literals += body.literal_credentials
    classified = ShellCommandRedaction(tuple(safe), redactions, literals)
    return verbatim_heredocs(command, raw, classified, redact, marker)
