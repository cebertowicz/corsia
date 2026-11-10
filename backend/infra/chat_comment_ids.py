"""Stable identifiers for comments carried by a chat input."""

from __future__ import annotations

import re

CID_PATTERN = r"c[1-9][0-9]*\.[1-9][0-9]*"
_CID = re.compile(CID_PATTERN)


def format_cid(input_order: int, position: int) -> str:
    return f"c{input_order}.{position}"


# Any start of a cid becomes a whole one by appending some ending of the smallest cid.
_SMALLEST = format_cid(1, 1)
_COMPLETIONS = tuple(_SMALLEST[index:] for index in range(len(_SMALLEST) + 1))


def could_start_cid(text: str) -> bool:
    """Whether ``text`` is a cid or the start of one."""
    return any(_CID.fullmatch(text + completion) for completion in _COMPLETIONS)


def parse_cid(value: object) -> tuple[int, int] | None:
    if not isinstance(value, str):
        return None
    match = _CID.fullmatch(value)
    if match is None:
        return None
    try:
        order, position = value[1:].split(".")
        return int(order), int(position)
    except ValueError:
        return None
