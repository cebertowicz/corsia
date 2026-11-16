"""Stable identifiers for comments carried by a chat input."""

from __future__ import annotations

import re

CID_PATTERN = r"c[1-9][0-9]*\.[1-9][0-9]*"
_CID = re.compile(CID_PATTERN)


# The one rule for which messages a chat comment can sit on. Attach, admission and insert all ask
# it, so widening it (say, to a person's messages) is a change here.
COMMENTABLE_ROLES = ("assistant",)


def is_commentable_role(role: object) -> bool:
    return role in COMMENTABLE_ROLES


def commentable_role_sql(column: str = "role") -> str:
    """SQL predicate for "this message row can take a chat comment"; ``column`` is its role column."""
    return f"{column} IN ({','.join(repr(role) for role in COMMENTABLE_ROLES)})"


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
