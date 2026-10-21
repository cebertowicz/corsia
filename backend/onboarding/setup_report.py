"""The body of a saved setup report: each failed row's cause and the service-log lines about it.

The service log can hold anything a vendor tool printed, so a line is copied only when it
names a failed row (its reference or its tool) and carries no credential-shaped word.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

TAIL_BYTES = 2 * 1024 * 1024
MAX_LINES = 60
MAX_LINE_CHARS = 400
WITHHELD = "[line withheld: it may contain a credential]"
# Any of these anywhere in a line withholds the whole line; nothing is partially masked.
_CREDENTIAL = re.compile(
    r"token|passw|secret|authoriz|bearer|api[_-]?key|apikey|cookie|credential|session|"
    r"private[_-]?key|\bsk-|\bghp_|\bgho_|-----BEGIN",
    re.IGNORECASE,
)
# What a row's tool is called in the log, beside its reference.
_ROW_TERMS = {
    "claude-code": ("claude-code", "claude code"),
    "codex": ("codex",),
    "omlx": ("local setup", "omlx", "local model"),
}


def _tail(path: Path) -> list[str]:
    try:
        with path.open("rb") as log:
            log.seek(0, 2)
            size = log.tell()
            log.seek(max(0, size - TAIL_BYTES))
            text = log.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = text.splitlines()
    return lines[1:] if size > TAIL_BYTES else lines


def relevant_lines(lines: Iterable[str], rows: list[dict]) -> list[str]:
    """The last log lines naming a failed row, with credential-shaped lines withheld."""
    terms = {term for row in rows for term in _ROW_TERMS.get(str(row.get("key")), ())}
    terms |= {str(row["reference"]).lower() for row in rows if row.get("reference")}
    picked = []
    for line in lines:
        lowered = line.lower()
        if not any(term in lowered for term in terms):
            continue
        picked.append(WITHHELD if _CREDENTIAL.search(line) else line[:MAX_LINE_CHARS])
    return picked[-MAX_LINES:]


def report_details(rows: list[dict], log_path: Path) -> str:
    """Each failed row's cause and reference, then the relevant service-log lines."""
    parts = ["Failed rows:"]
    if not rows:
        parts.append("  none")
    for row in rows:
        line = f"  {row.get('name') or row.get('key')}: {row.get('cause') or 'no cause recorded'}"
        if row.get("reference"):
            line += f" (reference {row['reference']})"
        parts.append(line)
    found = relevant_lines(_tail(log_path), rows) if rows else []
    parts += ["", f"Service log lines about these rows ({log_path}):"]
    parts += [f"  {line}" for line in found] or ["  none found"]
    return "\n".join(parts) + "\n"
