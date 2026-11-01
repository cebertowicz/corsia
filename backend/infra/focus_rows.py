"""The one backend rule for which rows Focus shows on their own.

It lives in `infra` so the claim that starts a turn can read it; the Focus routes
import it from here, and nothing re-exports it.

It mirrors the client's Focus model (`transcriptFocusModel.ts`) exactly, including
how it reads a missing or unparseable metadata value, so a Focus page and the
client's presentation never disagree on where a run ends. One JSON fixture
(`testdata/focus-visibility-parity.json`) runs through both implementations, for
visibility and for `row_kind`, the name a hidden row gives its run stub.
"""

from __future__ import annotations

import json

# System rows that only record activity. Every other system row is a notice
# (an error, interrupt, sandbox denial, or a type added later) and stays visible.
ACTIVITY_SYSTEM_TYPES = (
    "context_event", "activity_receipt", "subagent_ref", "subagent_done", "send_ref",
    "artifact_removed", "model_switch", "account_switch",
)
# System rows the transcript never draws: a `seed` is a session's starting context, a
# `turn_rerun` the model-only input that re-runs a failed Turn's unanswered input.
# One is not visible, so it never splits a run or holds back an error's recovery,
# and a Focus read neither counts it in a run nor sends it (`NEVER_DRAWN_FILTER`).
NEVER_DRAWN_SYSTEM_TYPES = ("seed", "turn_rerun")
# Rows that end a turn with an error. One stays visible until the agent's next turn
# starts with nothing to read in between; that start writes `recovered: true` on it.
ERROR_SYSTEM_TYPES = (
    "llm_error", "cc_error", "tool_boundary_violation", "tool_bridge_unavailable",
)


def _metadata(raw: object) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
        return value if isinstance(value, dict) else {}
    return {}


def _system_type(meta: dict) -> str:
    kind = meta.get("type")
    return str("" if kind is None else kind)


def is_focus_visible(row: dict) -> bool:
    """Whether Focus shows `row` as its own item (it closes a run)."""
    role = row.get("role")
    meta = _metadata(row.get("metadata"))
    if role == "user":
        return meta.get("sender_kind") != "agent"
    if role == "assistant":
        return meta.get("for_user") is True and bool(row.get("content"))
    if role == "system":
        kind = _system_type(meta)
        if kind in ERROR_SYSTEM_TYPES:
            return meta.get("recovered") is not True
        if kind == "result":
            # A provider result splits only as an error with something to read.
            return meta.get("is_error") is True and bool(row.get("content"))
        return kind not in ACTIVITY_SYSTEM_TYPES and kind not in NEVER_DRAWN_SYSTEM_TYPES
    return False


def row_kind(row: dict) -> str:
    """The run-stub kind of a row Focus does not show on its own.

    A visible row has no kind of its own and returns `other`. An assistant row is
    `steps` when its `activity_weight` is above zero.
    """
    role = row.get("role")
    meta = _metadata(row.get("metadata"))
    if role == "user":
        return "agent_message" if meta.get("sender_kind") == "agent" else "other"
    if role == "assistant":
        weight = row.get("activity_weight")
        return "steps" if isinstance(weight, (int, float)) and weight > 0 else "other"
    if role != "system":
        return "other"
    kind = _system_type(meta)
    if kind in ERROR_SYSTEM_TYPES:
        return "error_recovered" if meta.get("recovered") is True else "other"
    if kind == "context_event":
        return "context_update"
    if kind == "account_switch":
        return "account_move"
    if kind == "subagent_done":
        return "subagent_finished"
    is_model_receipt = kind == "activity_receipt" and (
        meta.get("receipt_type") == "model_switch" or meta.get("event_type") == "model.switched"
    )
    return "model_switch" if kind == "model_switch" or is_model_receipt else "other"


def _meta_value(expr: str) -> str:
    return f"CASE WHEN json_valid(metadata) THEN {expr} END"


def _sql_list(values: tuple[str, ...]) -> str:
    return ",".join(f"'{value}'" for value in values)


def _meta_text(path: str) -> str:
    extract = f"json_extract(metadata, '{path}')"
    return f"COALESCE({_meta_value(extract)}, '')"


def _meta_json_type(path: str) -> str:
    kind = f"json_type(metadata, '{path}')"
    return f"COALESCE({_meta_value(kind)}, '')"


_ACTIVITY_TYPE_LIST = _sql_list(ACTIVITY_SYSTEM_TYPES)
_NEVER_DRAWN_TYPE_LIST = _sql_list(NEVER_DRAWN_SYSTEM_TYPES)
_ERROR_TYPE_LIST = _sql_list(ERROR_SYSTEM_TYPES)
_TYPE_SQL = _meta_text("$.type")

# The same rule in SQL. `for_user`, `recovered` and `is_error` must be JSON `true`,
# not merely truthy.
FOCUS_VISIBLE_SQL = f"""
CASE WHEN (CASE role
    WHEN 'user' THEN {_meta_text("$.sender_kind")} != 'agent'
    WHEN 'assistant' THEN {_meta_json_type("$.for_user")} = 'true'
        AND COALESCE(content, '') != ''
    WHEN 'system' THEN CASE
        WHEN {_TYPE_SQL} IN ({_ERROR_TYPE_LIST}) THEN {_meta_json_type("$.recovered")} != 'true'
        WHEN {_TYPE_SQL} = 'result'
            THEN {_meta_json_type("$.is_error")} = 'true' AND COALESCE(content, '') != ''
        ELSE {_TYPE_SQL} NOT IN ({_ACTIVITY_TYPE_LIST},{_NEVER_DRAWN_TYPE_LIST}) END
    ELSE 0 END) THEN 1 ELSE 0 END
"""

# A `WHERE` clause continuation that drops the rows the transcript never draws.
NEVER_DRAWN_FILTER = (
    f"AND NOT (role = 'system' AND {_TYPE_SQL} IN ({_NEVER_DRAWN_TYPE_LIST})) "
)


def focus_kind_sql(weight_sql: str) -> str:
    """The same `row_kind` in SQL; `weight_sql` is the row's activity weight expression."""
    return f"""
CASE role
    WHEN 'user' THEN CASE WHEN {_meta_text("$.sender_kind")} = 'agent'
        THEN 'agent_message' ELSE 'other' END
    WHEN 'assistant' THEN CASE WHEN COALESCE({weight_sql}, 0) > 0 THEN 'steps' ELSE 'other' END
    WHEN 'system' THEN CASE
        WHEN {_TYPE_SQL} IN ({_ERROR_TYPE_LIST})
            THEN CASE WHEN {_meta_json_type("$.recovered")} = 'true'
                THEN 'error_recovered' ELSE 'other' END
        WHEN {_TYPE_SQL} = 'context_event' THEN 'context_update'
        WHEN {_TYPE_SQL} = 'account_switch' THEN 'account_move'
        WHEN {_TYPE_SQL} = 'subagent_done' THEN 'subagent_finished'
        WHEN {_TYPE_SQL} = 'model_switch' THEN 'model_switch'
        WHEN {_TYPE_SQL} = 'activity_receipt' AND (
            {_meta_text("$.receipt_type")} = 'model_switch'
            OR {_meta_text("$.event_type")} = 'model.switched') THEN 'model_switch'
        ELSE 'other' END
    ELSE 'other' END
"""
