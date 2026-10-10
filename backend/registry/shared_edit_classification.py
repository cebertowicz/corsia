"""Classify shared-document edits and render bounded owner-facing diffs."""

from __future__ import annotations

from difflib import SequenceMatcher, unified_diff
from pathlib import Path
import re

DIFF_CAP_BYTES = 16 * 1024
_HEADER_FIELD_RE = re.compile(r"^\s*\*\*[^*\n]+:\*\*.*$")
_MARKDOWN_LINK_RE = re.compile(r"(\[[^\]]+\]\()([^)]+)(\))")
_BACKTICK_TARGET_RE = re.compile(r"`([^`]+)`")


def _status_header_fields(lines: list[str]) -> set[int]:
    fields: set[int] = set()
    started = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if _HEADER_FIELD_RE.match(line):
            started = True
            fields.add(index)
            continue
        if started:
            break
        if stripped and not stripped.startswith("#"):
            break
    return fields


def _normalized_targets(line: str, *, allow_backtick: bool) -> str:
    normalized = _MARKDOWN_LINK_RE.sub(r"\1<TARGET>\3", line)
    return (
        _BACKTICK_TARGET_RE.sub("`<TARGET>`", normalized)
        if allow_backtick
        else normalized
    )


def _only_link_target_changed(
    before: str,
    after: str,
    *,
    allow_backtick: bool,
) -> bool:
    if before == after:
        return False
    before_targets = _MARKDOWN_LINK_RE.findall(before)
    after_targets = _MARKDOWN_LINK_RE.findall(after)
    if allow_backtick and not before_targets and not after_targets:
        before_targets = _BACKTICK_TARGET_RE.findall(before)
        after_targets = _BACKTICK_TARGET_RE.findall(after)
    return bool(before_targets and after_targets) and (
        _normalized_targets(before, allow_backtick=allow_backtick)
        == _normalized_targets(after, allow_backtick=allow_backtick)
    )


def _line_targets(line: str) -> list[str]:
    markdown = [match[1] for match in _MARKDOWN_LINK_RE.findall(line)]
    return markdown or _BACKTICK_TARGET_RE.findall(line)


def _local_index_targets(index_path: Path, line: str) -> list[Path]:
    return [
        (index_path.parent / target.split("#", 1)[0]).resolve(strict=False)
        for target in _line_targets(line)
        if target and "://" not in target and not target.startswith("#")
    ]


def _removed_index_target_is_missing(index_path: Path, line: str) -> bool:
    targets = _local_index_targets(index_path, line)
    return bool(targets) and all(not target.exists() for target in targets)


def _inserted_index_entry_is_real(index_path: Path, line: str) -> bool:
    """An inserted index line is mechanical only as an entry for a file that exists."""
    stripped = line.strip()
    if not stripped:
        return True
    if not stripped.startswith(("-", "*", "|")):
        return False
    targets = _local_index_targets(index_path, line)
    return bool(targets) and all(target.exists() for target in targets)


def classify_edit(
    path: Path,
    before: bytes | str,
    after: bytes | str,
    *,
    relocated: bool = False,
) -> str:
    """Return ``mechanical`` only when every changed line matches the design rule."""
    if relocated:
        return "mechanical"
    before_text = before.decode("utf-8", errors="replace") if isinstance(before, bytes) else before
    after_text = after.decode("utf-8", errors="replace") if isinstance(after, bytes) else after
    old = before_text.splitlines()
    new = after_text.splitlines()
    old_headers = _status_header_fields(old)
    new_headers = _status_header_fields(new)
    is_index = path.name == "INDEX.md"

    matcher = SequenceMatcher(None, old, new, autojunk=False)
    for operation, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if operation == "equal":
            continue
        old_lines = old[old_start:old_end]
        new_lines = new[new_start:new_end]
        if operation == "replace" and len(old_lines) == len(new_lines):
            for offset, (old_line, new_line) in enumerate(zip(old_lines, new_lines)):
                if old_start + offset in old_headers and new_start + offset in new_headers:
                    continue
                if _only_link_target_changed(
                    old_line,
                    new_line,
                    allow_backtick=is_index,
                ):
                    continue
                return "content"
            continue
        if operation in ("delete", "replace"):
            for offset, line in enumerate(old_lines):
                if old_start + offset in old_headers:
                    continue
                if is_index and _removed_index_target_is_missing(path, line):
                    continue
                return "content"
        if operation in ("insert", "replace"):
            for offset, line in enumerate(new_lines):
                if new_start + offset in new_headers:
                    continue
                if is_index and _inserted_index_entry_is_real(path, line):
                    continue
                return "content"
    return "mechanical"


def cap_rendered_diff(rendered: str) -> str:
    """Cap a composed diff to 16 KiB plus an explicit truncation line."""
    encoded = rendered.encode("utf-8")
    if len(encoded) <= DIFF_CAP_BYTES:
        return rendered
    prefix = encoded[:DIFF_CAP_BYTES].decode("utf-8", errors="ignore")
    return prefix + f"\n... (diff truncated at {DIFF_CAP_BYTES} bytes)\n"


def combine_capped_diffs(*parts: str) -> str:
    """Join non-empty diff segments and apply the shared render cap once."""
    present = [part.rstrip("\n") for part in parts if part]
    return cap_rendered_diff("\n".join(present) + "\n") if present else ""


def capped_unified_diff(path: Path, before: bytes, after: bytes) -> str:
    """Return a unified diff capped to 16 KiB plus an explicit truncation line."""
    old = before.decode("utf-8", errors="replace").splitlines(keepends=True)
    new = after.decode("utf-8", errors="replace").splitlines(keepends=True)
    rendered = "".join(unified_diff(old, new, fromfile=str(path), tofile=str(path)))
    return cap_rendered_diff(rendered)
