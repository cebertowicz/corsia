"""A small read-only JSON5 reader for OpenClaw's configuration file.

OpenClaw documents its config as JSON5: comments, trailing commas, unquoted
keys, single-quoted strings and signed or hexadecimal numbers. This returns
plain Python values and raises ``ValueError`` on anything else, so a malformed
file reads as absent exactly like a malformed JSON file does. It is not a
general library: it never evaluates anything, and nesting is bounded.
"""
from __future__ import annotations

import re
from typing import Any

_MAX_DEPTH = 64
_NUMBER = re.compile(r"[+-]?(?:Infinity|NaN|0[xX][0-9a-fA-F]+|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)")
_ESCAPES = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "0": "\0"}
_LITERALS = {"true": True, "false": False, "null": None}
_QUOTES = ("\"", "'")


class _Reader:
    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0

    def fail(self, why: str) -> ValueError:
        return ValueError(f"invalid JSON5 at {self.pos}: {why}")

    def skip(self) -> None:
        text = self.text
        while self.pos < len(text):
            char = text[self.pos]
            if char.isspace() or char == "﻿":
                self.pos += 1
            elif text.startswith("//", self.pos):
                end = text.find("\n", self.pos)
                self.pos = len(text) if end < 0 else end + 1
            elif text.startswith("/*", self.pos):
                end = text.find("*/", self.pos + 2)
                if end < 0:
                    raise self.fail("unterminated comment")
                self.pos = end + 2
            else:
                return

    def peek(self) -> str:
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def value(self, depth: int) -> Any:
        if depth > _MAX_DEPTH:
            raise self.fail("nested too deeply")
        self.skip()
        char = self.peek()
        if char == "{":
            return self.container(depth, "}", self.member, {})
        if char == "[":
            return self.container(depth, "]", self.element, [])
        if char in _QUOTES:
            return self.string()
        match = _NUMBER.match(self.text, self.pos)
        if match:
            self.pos = match.end()
            return self.number(match.group())
        for word, result in _LITERALS.items():
            if self.text.startswith(word, self.pos):
                self.pos += len(word)
                return result
        raise self.fail("unexpected character")

    def container(self, depth: int, close: str, item, result):
        self.pos += 1
        while True:
            self.skip()
            if self.peek() == close:
                self.pos += 1
                return result
            item(depth, result)
            self.skip()
            if self.peek() == ",":
                self.pos += 1
            elif self.peek() != close:
                raise self.fail("expected a comma or the end of the container")

    def element(self, depth: int, result: list) -> None:
        result.append(self.value(depth + 1))

    def member(self, depth: int, result: dict) -> None:
        key = self.string() if self.peek() in _QUOTES else self.identifier()
        self.skip()
        if self.peek() != ":":
            raise self.fail("expected a colon")
        self.pos += 1
        result[key] = self.value(depth + 1)

    def identifier(self) -> str:
        start = self.pos
        while self.pos < len(self.text) and (self.text[self.pos].isalnum() or self.text[self.pos] in "_$"):
            self.pos += 1
        if start == self.pos or self.text[start].isdigit():
            raise self.fail("expected a key")
        return self.text[start:self.pos]

    @staticmethod
    def number(token: str) -> int | float:
        sign = -1 if token.startswith("-") else 1
        body = token.lstrip("+-")
        if body == "Infinity":
            return sign * float("inf")
        if body == "NaN":
            return float("nan")
        if body[:2].lower() == "0x":
            return sign * int(body, 16)
        return sign * (float(body) if re.search(r"[.eE]", body) else int(body))

    def string(self) -> str:
        quote = self.text[self.pos]
        self.pos += 1
        out: list[str] = []
        while self.pos < len(self.text):
            char = self.text[self.pos]
            self.pos += 1
            if char == quote:
                return "".join(out)
            if char != "\\":
                out.append(char)
                continue
            escape = self.text[self.pos:self.pos + 1]
            self.pos += 1
            if escape in _ESCAPES:
                out.append(_ESCAPES[escape])
            elif escape in ("x", "u"):
                width = 2 if escape == "x" else 4
                digits = self.text[self.pos:self.pos + width]
                if len(digits) != width or not re.fullmatch(r"[0-9a-fA-F]+", digits):
                    raise self.fail("bad escape")
                out.append(chr(int(digits, 16)))
                self.pos += width
            elif escape == "\r":
                if self.peek() == "\n":
                    self.pos += 1
            elif escape not in ("\n", " ", " "):
                out.append(escape)
        raise self.fail("unterminated string")


def loads(text: str) -> Any:
    reader = _Reader(text)
    result = reader.value(0)
    reader.skip()
    if reader.pos != len(text):
        raise reader.fail("trailing characters")
    return result
