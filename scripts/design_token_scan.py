"""Scan frontend source for frozen design-token compliance debt."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

CATEGORIES = (
    "raw_colors",
    "raw_px",
    "heavy_borders",
    "raw_shadows",
    "row_menu_buttons",
)

_COLOR = re.compile(r"(?<![\w&])#[0-9a-fA-F]{3,8}\b|\b(?:rgb|hsl)a?\(", re.IGNORECASE)
_PX = re.compile(r"(?<![\w.-])(-?(?:\d+(?:\.\d+)?|\.\d+))px\b", re.IGNORECASE)
_PROPERTY = re.compile(
    r"\b(?P<name>font-size|fontSize|padding(?:-[\w-]+)?|padding[A-Z]\w*|"
    r"margin(?:-[\w-]+)?|margin[A-Z]\w*|(?:row-|column-)?gap|"
    r"(?:top|right|bottom|left)|(?:min-|max-)?(?:width|height)|"
    r"border-radius|borderRadius|box-shadow|boxShadow|border(?:-[\w-]+)?)"
    r"\s*[:=]\s*(?P<value>[^;{}\n]+)"
)
_SHADOW_TOKENS = re.compile(
    r"^\s*var\(--[\w-]+(?:\s*,[^()]*)?\)(?:\s*,\s*"
    r"var\(--[\w-]+(?:\s*,[^()]*)?\))*\s*$"
)
_STYLE_BLOCK = re.compile(r"<style\b[^>]*>(?P<value>.*?)</style\s*>", re.IGNORECASE | re.DOTALL)
_STYLE_ATTRIBUTE = re.compile(
    r"\bstyle(?:\:[\w-]+)?\s*=\s*(?P<quote>[\"'])(?P<value>.*?)(?P=quote)",
    re.IGNORECASE | re.DOTALL,
)
_BUTTON_START = re.compile(r"<(?P<tag>button|Button|IconButton)\b", re.IGNORECASE)
_MORE_LABEL = re.compile(
    r"\b(?:aria-label|title)\s*=\s*[\"'][^\"']*\bmore (?:actions|options)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Violation:
    """One mechanically detectable style violation."""

    category: str
    line: int
    text: str


def source_paths(root: Path) -> list[Path]:
    """Return all governed frontend style sources, except the token definition."""
    source = root / "frontend" / "src"
    return sorted(
        path
        for path in source.rglob("*")
        if path.suffix in {".svelte", ".css", ".ts"}
        and not path.name.endswith((".test.ts", ".spec.ts", ".test.svelte", ".spec.svelte"))
        and path.relative_to(root).as_posix() != "frontend/src/app-tokens.css"
    )


def _mask_comments(text: str) -> str:
    """Keep source offsets while ignoring CSS and TypeScript comments."""
    def mask(match: re.Match[str]) -> str:
        return "".join("\n" if char == "\n" else " " for char in match.group())

    masked = re.sub(r"/\*.*?\*/", mask, text, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", mask, masked)


def _line(text: str, offset: int) -> tuple[int, str]:
    line = text.count("\n", 0, offset) + 1
    start = text.rfind("\n", 0, offset) + 1
    end = text.find("\n", offset)
    return line, text[start : len(text) if end == -1 else end].strip()


def _inside_var_fallback(value: str, offset: int) -> bool:
    """Return whether offset is in the fallback portion of ``var(--token, x)``."""
    for match in re.finditer(r"\bvar\(", value):
        depth = 1
        comma = None
        for index in range(match.end(), len(value)):
            if value[index] == "(":
                depth += 1
            elif value[index] == ")":
                depth -= 1
                if depth == 0:
                    if comma is not None and comma < offset < index:
                        return True
                    break
            elif value[index] == "," and depth == 1 and comma is None:
                comma = index
    return False


def _append(violations: list[Violation], category: str, text: str, offset: int) -> None:
    line, source_line = _line(text, offset)
    violations.append(Violation(category, line, source_line))


def _style_ranges(text: str, suffix: str) -> list[tuple[int, int]]:
    """Return style-only source spans for each supported frontend file kind."""
    if suffix == ".css":
        return [(0, len(text))]
    if suffix == ".svelte":
        matches = list(_STYLE_BLOCK.finditer(text)) + list(_STYLE_ATTRIBUTE.finditer(text))
        return sorted((match.start("value"), match.end("value")) for match in matches)
    return _string_ranges(text) if suffix == ".ts" else []


def _string_ranges(text: str) -> list[tuple[int, int]]:
    """Return TypeScript string contents without treating prose outside strings as CSS."""
    ranges: list[tuple[int, int]] = []
    start = None
    quote = ""
    escaped = False
    comment_end = 0
    for index, char in enumerate(text):
        if index < comment_end:
            continue
        if start is None:
            # A comment's apostrophe must not open a string.
            if text.startswith("//", index):
                newline = text.find("\n", index)
                comment_end = len(text) if newline == -1 else newline
            elif text.startswith("/*", index):
                close = text.find("*/", index + 2)
                comment_end = len(text) if close == -1 else close + 2
            elif char in "'\"`":
                start, quote = index + 1, char
        elif escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == quote:
            ranges.append((start, index))
            start = None
    return ranges


def _is_issue_reference(fragment: str, offset: int) -> bool:
    """Reject ``PR #123`` and ``issue #123`` text while preserving hex colours."""
    return bool(re.search(r"\b(?:pr|issue)\s*$", fragment[:offset], re.IGNORECASE))


def _property_violations(
    text: str, masked: str, base: int, violations: list[Violation]
) -> None:
    """Collect declaration-level spacing, border, and shadow violations."""
    for match in _PROPERTY.finditer(masked):
        name = match.group("name")
        value = match.group("value")
        value_start = match.start("value")
        lowered = name.lower()
        if lowered in {"box-shadow", "boxshadow"}:
            shadow = value.strip().strip("'\"")
            # `none` removes a shadow; it is not a raw shadow value.
            if shadow.lower() != "none" and not _SHADOW_TOKENS.fullmatch(shadow):
                _append(violations, "raw_shadows", text, base + match.start())
            continue
        if lowered.startswith("border") and "radius" not in lowered:
            for px in _PX.finditer(value):
                if float(px.group(1)) >= 2:
                    _append(violations, "heavy_borders", text, base + value_start + px.start())
            continue
        for px in _PX.finditer(value):
            number = float(px.group(1))
            if number in {0, 1} or _inside_var_fallback(value, px.start()):
                continue
            _append(violations, "raw_px", text, base + value_start + px.start())


def _tag_end(text: str, start: int) -> int | None:
    """Find the closing ``>`` while ignoring arrows inside Svelte expressions."""
    depth = 0
    quote = ""
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
        elif char in "'\"`":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth = max(0, depth - 1)
        elif char == ">" and depth == 0:
            return index
    return None


def _visible_text(body: str) -> str:
    """Strip nested markup and Svelte expressions before comparing a button label."""
    without_tags = re.sub(r"<[^>]*>", "", body)
    without_expressions = re.sub(r"\{[^{}]*\}", "", without_tags)
    return " ".join(without_expressions.split())


def _row_menu_violations(text: str, violations: list[Violation]) -> None:
    """Flag literal ellipsis buttons and explicit More actions/options labels."""
    for match in _BUTTON_START.finditer(text):
        end = _tag_end(text, match.end())
        if end is None:
            continue
        attrs = text[match.end() : end]
        close = re.search(rf"</{re.escape(match.group('tag'))}\s*>", text[end + 1 :], re.IGNORECASE)
        body_end = len(text) if close is None else end + 1 + close.start()
        label = _visible_text(text[end + 1 : body_end])
        if label in {"⋯", "…", "..."} or _MORE_LABEL.search(attrs):
            _append(violations, "row_menu_buttons", text, match.start())


def scan_text(text: str, suffix: str) -> list[Violation]:
    """Return all detectable violations in one frontend source file."""
    violations: list[Violation] = []
    for start, end in _style_ranges(text, suffix):
        fragment = text[start:end]
        masked = _mask_comments(fragment)
        for match in _COLOR.finditer(masked):
            if match.group().startswith("#") and _is_issue_reference(fragment, match.start()):
                continue
            _append(violations, "raw_colors", text, start + match.start())
        _property_violations(text, masked, start, violations)
    if suffix == ".svelte":
        _row_menu_violations(text, violations)
    return violations


def scan(root: Path) -> dict[str, list[Violation]]:
    """Scan governed sources and return repo-relative violations by file."""
    findings: dict[str, list[Violation]] = {}
    for path in source_paths(root):
        rel_path = path.relative_to(root).as_posix()
        findings[rel_path] = scan_text(path.read_text(encoding="utf-8"), path.suffix)
    return findings


def counts(findings: dict[str, list[Violation]]) -> dict[str, dict[str, int]]:
    """Turn individual findings into the committed per-file category counts."""
    return {
        path: {category: sum(item.category == category for item in items) for category in CATEGORIES}
        for path, items in sorted(findings.items())
    }
