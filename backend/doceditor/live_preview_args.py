"""Incremental decoder for one streamed file-tool input, fed fragment by fragment.

A provider streams a tool call's JSON input in arbitrary fragments, in any key
order. This reads each fragment once (never the accumulated buffer), carries a
string escape across fragment boundaries, and exposes only what has been
decoded: the top-level string fields and ``edits[]`` entries, each marked
complete once its closing quote arrives. Nothing here guesses at partial JSON.
"""
from __future__ import annotations

from dataclasses import dataclass, field

_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
_HEX = frozenset("0123456789abcdefABCDEF")


@dataclass
class Value:
    """One string value decoded so far; ``complete`` once its closing quote arrived."""

    parts: list[str] = field(default_factory=list)
    complete: bool = False

    def text(self) -> str:
        """Join the decoded parts, keeping the joined form so repeated reads stay linear."""
        if len(self.parts) > 1:
            self.parts[:] = ["".join(self.parts)]
        return self.parts[0] if self.parts else ""


class ArgScanner:
    """Decode a tool-input object incrementally: top-level strings, booleans and ``edits[]`` strings."""

    def __init__(self) -> None:
        self.fields: dict[str, Value] = {}
        self.flags: dict[str, bool] = {}
        self.edits: list[dict[str, Value]] = []
        self.failed = False
        self._stack: list[str] = []  # 'o' object, 'a' array
        self._key: list[str | None] = []  # current key per object level
        self._expect_key = False
        self._in_string = False
        self._string_is_key = False
        self._target: Value | None = None
        self._key_parts: list[str] = []
        self._escape = ""  # pending escape: "\\" or "\\u" plus hex digits
        self._high: int | None = None  # a high surrogate waiting for its pair
        self._scalar: list[str] = []

    def feed(self, fragment: str) -> None:
        """Decode one fragment; malformed input stops decoding for good."""
        index, size = 0, len(fragment)
        while index < size and not self.failed:
            if self._in_string:
                index = self._string(fragment, index)
            else:
                self._structural(fragment[index])
                index += 1

    def _string(self, fragment: str, index: int) -> int:
        if self._escape:
            return self._escaped(fragment, index)
        stop = min((p for p in (fragment.find('"', index), fragment.find("\\", index)) if p >= 0), default=len(fragment))
        if stop > index:
            self._emit(fragment[index:stop])
        if stop >= len(fragment):
            return stop
        if fragment[stop] == "\\":
            self._escape = "\\"
            return stop + 1
        self._close_string()
        return stop + 1

    def _escaped(self, fragment: str, index: int) -> int:
        char = fragment[index]
        if self._escape == "\\":
            if char == "u":
                self._escape = "\\u"
                return index + 1
            decoded = _ESCAPES.get(char)
            if decoded is None:
                self.failed = True
                return index + 1
            self._escape = ""
            self._emit(decoded)
            return index + 1
        if char not in _HEX:
            self.failed = True
            return index + 1
        self._escape += char
        if len(self._escape) == 6:
            code, self._escape = int(self._escape[2:], 16), ""
            self._code_unit(code)
        return index + 1

    def _code_unit(self, code: int) -> None:
        if 0xD800 <= code < 0xDC00:
            self._high = code
            return
        if 0xDC00 <= code < 0xE000 and self._high is not None:
            code, self._high = 0x10000 + ((self._high - 0xD800) << 10) + (code - 0xDC00), None
        self._emit(chr(code))

    def _emit(self, text: str) -> None:
        if self._high is not None:  # an unpaired high surrogate cannot be drawn
            self.failed = True
            return
        if self._string_is_key:
            self._key_parts.append(text)
        elif self._target is not None:
            self._target.parts.append(text)

    def _close_string(self) -> None:
        self._in_string = False
        if self._string_is_key:
            self._key[-1] = "".join(self._key_parts)
            self._key_parts = []
            self._expect_key = False
        elif self._target is not None:
            self._target.complete = True
            self._target = None

    def _open_string(self) -> None:
        self._in_string = True
        self._string_is_key = self._expect_key and self._stack[-1:] == ["o"]
        if not self._string_is_key:
            self._target = self._value_slot()

    def _value_slot(self) -> Value | None:
        depth, key = len(self._stack), self._key[-1] if self._key else None
        if depth == 1 and key is not None:
            return self.fields.setdefault(key, Value())
        if depth == 3 and self._stack == ["o", "a", "o"] and self._key[0] == "edits" and key is not None:
            return self.edits[-1].setdefault(key, Value())
        return None

    def _structural(self, char: str) -> None:
        if char in " \t\r\n":
            return
        if char == '"':
            self._open_string()
        elif char in "{[":
            self._open(char)
        elif char in "}]":
            self._scalar_done()
            self._close()
        elif char == ":":
            self._expect_key = False
        elif char == ",":
            self._scalar_done()
            self._expect_key = self._stack[-1:] == ["o"]
        else:
            self._scalar.append(char)

    def _open(self, char: str) -> None:
        if char == "{" and self._stack == ["o", "a"] and self._key[0] == "edits":
            self.edits.append({})
        self._stack.append("o" if char == "{" else "a")
        if char == "{":
            self._key.append(None)
        self._expect_key = char == "{"

    def _close(self) -> None:
        if not self._stack:
            self.failed = True
            return
        if self._stack.pop() == "o":
            self._key.pop()
        self._expect_key = False

    def _scalar_done(self) -> None:
        if not self._scalar:
            return
        token, self._scalar = "".join(self._scalar), []
        key = self._key[-1] if self._key else None
        if len(self._stack) == 1 and key is not None and token in {"true", "false"}:
            self.flags[key] = token == "true"

    def string(self, key: str) -> Value | None:
        """A top-level string field decoded so far, if it has started."""
        return self.fields.get(key)
