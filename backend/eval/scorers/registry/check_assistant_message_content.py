from __future__ import annotations

import re

from inspect_ai.scorer import (
    Score, Target, accuracy, mean, scorer, stderr, CORRECT, INCORRECT,
)
from inspect_ai.solver import TaskState

from ._events import get_assistant_content


_VALID_MODES = ("contains", "equals", "regex")


@scorer(metrics=[accuracy(), mean(), stderr()])
def check_assistant_message_content(
    *,
    mode: str,
    value: str,
    case_sensitive: bool = False,
):
    if mode not in _VALID_MODES:
        raise ValueError(
            f"check_assistant_message_content: mode {mode!r} not in "
            f"{list(_VALID_MODES)}"
        )
    if value is None:
        raise ValueError("check_assistant_message_content: value is required")

    compiled: re.Pattern | None = None
    if mode == "regex":
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            compiled = re.compile(value, flags=flags)
        except re.error as e:
            raise ValueError(
                f"check_assistant_message_content: regex {value!r} invalid: {e}"
            ) from e

    async def score(state: TaskState, target: Target) -> Score:
        content = get_assistant_content(state)
        if not content:
            return Score(
                value=INCORRECT,
                answer="empty",
                explanation=(
                    f"No assistant content to compare against value={value!r}"
                ),
                metadata={"mode": mode, "value": value, "empty_content": True},
            )

        passed: bool
        if mode == "contains":
            haystack = content if case_sensitive else content.lower()
            needle = value if case_sensitive else value.lower()
            passed = needle in haystack
        elif mode == "equals":
            if case_sensitive:
                passed = content == value
            else:
                passed = content.lower() == value.lower()
        else:  # regex — compiled at factory time
            assert compiled is not None  # for type-checkers
            passed = compiled.search(content) is not None

        return Score(
            value=CORRECT if passed else INCORRECT,
            answer="match" if passed else "no_match",
            explanation=(
                f"mode={mode} value={value!r} case_sensitive={case_sensitive} "
                f"({'PASS' if passed else 'FAIL'}); content_len={len(content)}"
            ),
            metadata={
                "mode": mode,
                "value": value,
                "case_sensitive": case_sensitive,
                "content_prefix": content[:200],
                "content_len": len(content),
                "empty_content": False,
            },
        )

    return score


__all__ = ["check_assistant_message_content"]
