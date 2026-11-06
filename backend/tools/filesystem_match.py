"""Where an edit's `old_string` sits in a file's text, and the message when it does not.

The edit tools and the live document lane share this one matching, so both report the same best match and
give the model the same receipts.
"""

from collections import Counter
import difflib

from doceditor.live_lane import TextEdit


_MATCH_CANDIDATES = 5


def _candidate_starts(lines: list[str], old_lines: list[str], last_start: int) -> list[int]:
    """Window starts worth a character-level comparison, best first.

    A character ratio is quadratic in the window's length, so it runs only on
    the few windows that share the most lines with old_string (ignoring
    indentation). When no line is shared, a close line to the first non-blank
    line of old_string anchors the window instead; lines are short, so that
    comparison stays cheap.
    """
    positions: dict[str, list[int]] = {}
    for index, line in enumerate(lines):
        if line.strip():
            positions.setdefault(line.strip(), []).append(index)
    votes: Counter[int] = Counter()
    for offset, line in enumerate(old_lines):
        for index in positions.get(line.strip(), ()):
            if 0 <= index - offset <= last_start:
                votes[index - offset] += 1
    if votes:
        return [start for start, _count in votes.most_common(_MATCH_CANDIDATES)]
    anchor = next(((offset, line) for offset, line in enumerate(old_lines) if line.strip()), None)
    if anchor is None:
        return [0]
    offset, text = anchor
    scored = []
    for index, line in enumerate(lines):
        if 0 <= index - offset <= last_start:
            matcher = difflib.SequenceMatcher(None, text, line)
            if matcher.real_quick_ratio() > 0.5 and matcher.quick_ratio() > 0.5:
                scored.append((matcher.ratio(), index - offset))
    scored.sort(reverse=True)
    return [start for _ratio, start in scored[:_MATCH_CANDIDATES]] or [0]


def _not_found_message(old_string: str, content: str, path: str, prefix: str = "") -> str:
    """Build a helpful error when old_string is not found."""
    lines = content.splitlines(keepends=True)
    old_lines = old_string.splitlines(keepends=True)
    window = len(old_lines)

    best_ratio, best_start = 0.0, 0
    for i in _candidate_starts(lines, old_lines, max(0, len(lines) - window)):
        ratio = difflib.SequenceMatcher(None, old_string, "".join(lines[i : i + window])).ratio()
        if ratio > best_ratio:
            best_ratio, best_start = ratio, i

    if best_ratio > 0.5:
        diff = "\n".join(difflib.unified_diff(
            old_lines, lines[best_start : best_start + window],
            fromfile="old_string (provided)", tofile=f"{path} (actual, line {best_start + 1})",
            lineterm="",
        ))
        return f"Error: {prefix}old_string not found in {path}.\nBest match ({best_ratio:.0%} similar) at line {best_start + 1}:\n{diff}"
    return f"Error: {prefix}old_string not found in {path}. No similar text found. Verify the file content."


def match_edits(original: str, edits: tuple[TextEdit, ...], path: str, *, multi: bool,
                error_path: str | None = None) -> tuple[str | None, str]:
    """The live lane and closed-file tools share matching and model receipts."""
    content, applied = original, 0
    for idx, edit in enumerate(edits, start=1):
        if edit.old not in content:
            message = _not_found_message(edit.old, content, error_path or path, prefix=f"edit {idx} " if multi else "")
            if multi:
                message += "\nNo changes were written. Re-read the file, then retry."
            return None, message
        count = content.count(edit.old)
        if count > 1 and not edit.replace_all:
            message = (
                f"Error: edit {idx} old_string appears {count} times. "
                "Provide more context or set replace_all=true. No changes were written."
            ) if multi else (
                f"Warning: old_string appears {count} times. Provide more context to make it unique, or use replace_all=true."
            )
            return None, message
        content = content.replace(edit.old, edit.new, -1 if edit.replace_all else 1)
        applied += count if edit.replace_all else 1
    if multi:
        message = f"No changes needed in {path}" if content == original else (
            f"Successfully applied {len(edits)} edit(s), {applied} replacement(s), to {path}"
        )
    else:
        message = f"Successfully replaced {applied} occurrence(s) in {path}" if edits and edits[0].replace_all else f"Successfully edited {path}"
    return content, message

