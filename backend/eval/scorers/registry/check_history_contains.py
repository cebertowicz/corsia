from __future__ import annotations

import re

from inspect_ai.scorer import (
    Score, Target, accuracy, mean, scorer, stderr, CORRECT, INCORRECT,
)
from inspect_ai.solver import TaskState

from ._events import get_history


_VALID_MODES = ("contains", "regex")


def _find_match_index(
    eligible: list[dict],
    *,
    mode: str,
    value: str,
    case_sensitive: bool,
    compiled: re.Pattern | None,
) -> int | None:
    match_index: int | None = None
    for i, msg in enumerate(eligible):
        content = _stringify(msg.get("content"))
        if not content:
            continue
        if mode == "contains":
            hay = content if case_sensitive else content.lower()
            needle = value if case_sensitive else value.lower()
            if needle in hay:
                match_index = i
                break
        else:  # regex
            assert compiled is not None
            if compiled.search(content):
                match_index = i
                break
    return match_index


@scorer(metrics=[accuracy(), mean(), stderr()])
def check_history_contains(
    *,
    mode: str,
    value: str,
    roles: list[str] | None = None,
    case_sensitive: bool = False,
):
    if mode not in _VALID_MODES:
        raise ValueError(
            f"check_history_contains: mode {mode!r} not in {list(_VALID_MODES)}"
        )
    if value is None:
        raise ValueError("check_history_contains: value is required")

    role_filter: frozenset[str] | None = (
        frozenset(roles) if roles is not None else None
    )

    compiled: re.Pattern | None = None
    if mode == "regex":
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            compiled = re.compile(value, flags=flags)
        except re.error as e:
            raise ValueError(
                f"check_history_contains: regex {value!r} invalid: {e}"
            ) from e

    async def score(state: TaskState, target: Target) -> Score:
        history = get_history(state)
        eligible = [
            msg for msg in history
            if role_filter is None or msg.get("role") in role_filter
        ]

        if not history:
            return Score(
                value=INCORRECT,
                answer="empty_history",
                explanation=(
                    f"Empty history; cannot verify mode={mode} value={value!r}"
                ),
                metadata={
                    "mode": mode,
                    "value": value,
                    "roles": sorted(role_filter) if role_filter else None,
                    "history_size": 0,
                    "eligible_size": 0,
                },
            )

        match_index = _find_match_index(
            eligible, mode=mode, value=value,
            case_sensitive=case_sensitive, compiled=compiled,
        )

        passed = match_index is not None
        return Score(
            value=CORRECT if passed else INCORRECT,
            answer="match" if passed else "no_match",
            explanation=(
                f"mode={mode} value={value!r} "
                f"roles={sorted(role_filter) if role_filter else 'any'} "
                f"({'PASS' if passed else 'FAIL'}); "
                f"history_size={len(history)} eligible={len(eligible)}"
            ),
            metadata={
                "mode": mode,
                "value": value,
                "roles": sorted(role_filter) if role_filter else None,
                "case_sensitive": case_sensitive,
                "history_size": len(history),
                "eligible_size": len(eligible),
                "match_index": match_index,
            },
        )

    return score


def _stringify(content: object) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                txt = item.get("text")
                if isinstance(txt, str):
                    parts.append(txt)
        return "\n".join(parts)
    return str(content)


__all__ = ["check_history_contains"]
