"""The body of a saved setup report: each failed row's cause and the service-log lines about it.

The service log can hold anything a vendor tool printed, so a line is copied only when it
carries a failed row's reference as a whole `reference=<ref>` token, which CORSIA writes only
into its own fixed-format failure lines. A copied line that still looks credential-shaped
(a credential word, sign-in code, OAuth value or email address) is withheld whole.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from infra.failure_reference import REFERENCE

TAIL_BYTES = 2 * 1024 * 1024
MAX_LINES = 60
MAX_LINE_CHARS = 400
WITHHELD = "[line withheld: it may contain a credential]"
# Any of these anywhere in a line withholds the whole line; nothing is partially masked.
_CREDENTIAL = re.compile(
    r"token|passw|secret|authoriz|bearer|api[_-]?key|apikey|cookie|credential|session|"
    r"private[_-]?key|\bsk-|\bghp_|\bgho_|-----BEGIN|"
    r"\b(?:code|state)\s*[:=]|(?:user|device)[_ -]?code|verification|"
    r"[\w.+-]+@[\w-]+\.[\w.-]+",
    re.IGNORECASE,
)


def _tail(path: Path) -> list[str] | None:
    """The log's last lines, or None when it couldn't be read."""
    try:
        with path.open("rb") as log:
            log.seek(0, 2)
            size = log.tell()
            log.seek(max(0, size - TAIL_BYTES))
            text = log.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    lines = text.splitlines()
    return lines[1:] if size > TAIL_BYTES else lines


def relevant_lines(lines: Iterable[str], rows: list[dict]) -> list[str]:
    """The last log lines carrying a failed row's reference, with credential-shaped lines withheld."""
    # Only a reference CORSIA minted selects lines, and only as its whole log token.
    refs = {f"reference={row['reference']}" for row in rows if REFERENCE.match(str(row.get("reference") or ""))}
    picked = []
    for line in lines:
        if not refs.intersection(line.split()):
            continue
        picked.append(WITHHELD if _CREDENTIAL.search(line) else line[:MAX_LINE_CHARS])
    return picked[-MAX_LINES:]


def report_details(rows: list[dict], log_path: Path) -> str:
    """Each failed row's cause and reference, then the relevant service-log lines."""
    parts = ["Failed rows:"]
    for row in rows:
        line = f"  {row.get('name') or row.get('key')}: {row.get('cause') or 'no cause recorded'}"
        if row.get("reference"):
            line += f" (reference {row['reference']})"
        parts.append(line)
    lines = _tail(log_path)
    parts += ["", f"Service log lines about these rows ({log_path}):"]
    if lines is None:
        parts.append("  CORSIA couldn’t read the service log")
    else:
        parts += [f"  {line}" for line in relevant_lines(lines, rows)] or ["  none found"]
    return "\n".join(parts) + "\n"
