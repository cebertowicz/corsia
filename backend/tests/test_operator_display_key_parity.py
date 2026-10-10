"""The backend and client must agree on every server-owned display key.

The client renders server keys and falls back to "Unavailable" for anything it
does not know. That fallback is correct as a last resort but silent as a
contract check: a key added on one side reaches a user as "Unavailable"
instead of failing a build. This compares the two vocabularies directly.
"""

from __future__ import annotations

import pathlib
import re

from plans import (
    not_moving,
    operator_display_state,
    operator_node_state,
    operator_plan_display,
)

FRONTEND = (
    pathlib.Path(__file__).resolve().parents[2] / "frontend" / "src" / "lib"
)
TYPES = FRONTEND / "api" / "plansTypes.ts"
DISPLAY = FRONTEND / "components" / "plans" / "planDisplay.ts"


def _union(source: str, name: str) -> set[str]:
    """Return the string members of one exported TypeScript union type."""
    match = re.search(
        rf"export type {name} =\s*(?P<body>(?:\s*\|\s*'[a-z_-]+')+)\s*;",
        source,
    )
    assert match is not None, f"{name} is not a string union in plansTypes.ts"
    return set(re.findall(r"'([a-z_-]+)'", match.group("body")))


def _record_keys(source: str, name: str) -> set[str]:
    """Return the keys of one const Record literal in planDisplay.ts.

    Scans with brace depth rather than a non-greedy match, so a Record whose
    values contain braces (an object, or a template literal) does not silently
    truncate the key set and turn this check into a no-op.
    """
    start = re.search(rf"const {name}: Record<[^>]+> = \{{", source)
    assert start is not None, f"{name} is not a Record literal in planDisplay.ts"
    depth, index = 1, start.end()
    while depth and index < len(source):
        depth += {"{": 1, "}": -1}.get(source[index], 0)
        index += 1
    assert depth == 0, f"{name} literal is unbalanced"
    body = source[start.end():index - 1]
    # Only keys at the literal's own top level, never keys of nested objects.
    top_level, depth = [], 0
    for line in body.splitlines():
        if depth == 0:
            match = re.match(r"\s*'?([a-z_-]+)'?\s*:", line)
            if match:
                top_level.append(match.group(1))
        depth += line.count("{") - line.count("}")
    return set(top_level)


def test_node_label_keys_match_the_client_union():
    declared = _union(TYPES.read_text(), "OperatorNodeLabelKey")

    assert operator_node_state.NODE_LABEL_KEYS == declared


def test_plan_label_keys_match_the_client_union():
    declared = _union(TYPES.read_text(), "OperatorPlanLabelKey")

    assert operator_node_state.PLAN_LABEL_KEYS == declared


def test_tone_keys_match_the_client_union():
    declared = _union(TYPES.read_text(), "OperatorToneKey")

    assert operator_node_state.TONE_KEYS == declared


def test_the_client_maps_every_key_it_declares():
    """Declaring a key is not the same as rendering it."""
    source = DISPLAY.read_text()

    assert _record_keys(source, "PLAN_STATES") == operator_node_state.PLAN_LABEL_KEYS
    assert _record_keys(source, "NODE_LABELS") == operator_node_state.NODE_LABEL_KEYS
    assert _record_keys(source, "NODE_STATES") == operator_node_state.NODE_LABEL_KEYS
    assert _record_keys(source, "TONE_LABELS") == operator_node_state.TONE_KEYS


def test_every_emitted_key_is_in_the_declared_vocabulary():
    """Guard the source itself, so a new literal cannot skip the vocabulary."""
    source = "\n".join(
        pathlib.Path(module.__file__).read_text()
        for module in (
            operator_display_state, operator_node_state, operator_plan_display, not_moving,
        )
    )
    emitted_labels = set(re.findall(r'"label_key": "([a-z_]+)"', source))
    emitted_tones = set(re.findall(r'"tone_key": "([a-z-]+)"', source))
    known = operator_node_state.NODE_LABEL_KEYS | operator_node_state.PLAN_LABEL_KEYS

    assert emitted_labels <= known
    assert emitted_tones <= operator_node_state.TONE_KEYS
