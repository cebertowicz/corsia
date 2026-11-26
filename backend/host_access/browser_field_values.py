"""Hide what was typed into a snapshot's text fields; browser_control decides when a snapshot needs it."""
from __future__ import annotations

import json
import re
from dataclasses import replace

from host_access import browser_cli

# Snapshot roles that hold typed text; the value follows the line's ref and attributes after ": ".
_FIELD_LINE = re.compile(r"^(?P<indent>\s*)- (?:textbox|searchbox|combobox|spinbutton)\b")
_REF = re.compile(r"\[ref=e\d+\]")
_ATTRIBUTES = re.compile(r"(?:\s*\[[a-z-]+(?:=[^\]\s]*)?\])*")
VALUE_HIDDEN = "[value hidden]"


def hide_field_values(snapshot: str) -> str:
    """Replace every text field's value in a snapshot, and drop the text nodes beneath it.

    Reads no value and needs no node identity: anything after a field's ref and attributes is hidden, so a
    line it can't parse fails closed.
    """
    lines, field_indent = [], None
    for line in snapshot.split("\n"):
        indent = len(line) - len(line.lstrip())
        if field_indent is not None and indent > field_indent and line.strip():
            continue
        field_indent = None
        if (match := _FIELD_LINE.match(line)) is None:
            lines.append(line)
            continue
        field_indent = len(match.group("indent"))
        ref = _REF.search(line, match.end())
        if ref is None:
            lines.append(f"{line[:match.end()]} {VALUE_HIDDEN}")
            continue
        attributes = _ATTRIBUTES.match(line, ref.end())
        kept = attributes.end() if attributes is not None else ref.end()
        lines.append(line if not line[kept:].strip() else f"{line[:kept]}: {VALUE_HIDDEN}")
    return "\n".join(lines)


def hidden_snapshot(result: browser_cli.CommandResult) -> browser_cli.CommandResult | None:
    """A snapshot taken while a filled field shows: only `data.snapshot`, its values hidden; None for other output.

    Any other shape (a warning line before the JSON, an error, plain text) could carry a value, so it fails closed.
    """
    if result.timed_out or result.completion == "interrupted" or result.returncode != 0:
        return None
    data = browser_cli.json_object(result.stdout).get("data")
    snapshot = data.get("snapshot") if isinstance(data, dict) else None
    if not isinstance(snapshot, str):
        return None
    stdout = json.dumps({"success": True, "data": {"snapshot": hide_field_values(snapshot)}})
    return replace(result, stdout=stdout, stderr="")
