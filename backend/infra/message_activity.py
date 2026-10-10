"""The one SQL rule for how much activity a stored message row counts as.

A Turn's `activity_count` and each display row's `activity_weight` are the same
expression: the Turn summary sums it over a Turn, and a display read selects it
per row, so a run of rows adds up by the rule the Turn does. Nothing in Python
or the client restates it.
"""

from __future__ import annotations

# It mirrors the display projection's call fallback: nonempty llm calls win,
# then nonempty already-shaped calls, then streamed hint-only activity.
_DISPLAY_CALL_COUNT_SQL = """
CASE
    WHEN json_type(metadata, '$.llm_tool_calls') = 'array'
         AND json_array_length(metadata, '$.llm_tool_calls') > 0
        THEN json_array_length(metadata, '$.llm_tool_calls')
    WHEN json_type(metadata, '$.tool_calls') = 'array'
         AND json_array_length(metadata, '$.tool_calls') > 0
        THEN json_array_length(metadata, '$.tool_calls')
    WHEN json_type(metadata, '$.tool_hints') = 'array'
        THEN json_array_length(metadata, '$.tool_hints')
    ELSE 0
END
"""
_DISPLAY_REASONING_COUNT_SQL = """
CASE WHEN COALESCE(json_extract(metadata, '$.reasoning'), '') <> '' THEN 1 ELSE 0 END
"""
# The rows that count at all. It is also idx_messages_turn_calls' partial-index
# predicate (infra/schema.py), so a WHERE that uses it qualifies for that index.
ACTIVITY_ROW_SQL = "role='assistant' AND json_valid(metadata)"
# What one counted row adds: a said-to-you reply with nothing behind it adds
# nothing, and any other row is at least one step.
ACTIVITY_SQL = f"""
CASE
    WHEN json_extract(metadata, '$.for_user') = 1
         AND content <> ''
         AND ({_DISPLAY_CALL_COUNT_SQL}) = 0
         AND ({_DISPLAY_REASONING_COUNT_SQL}) = 0
    THEN 0
    ELSE MAX(1, ({_DISPLAY_CALL_COUNT_SQL}) + ({_DISPLAY_REASONING_COUNT_SQL}))
END
"""
# One row's share of its Turn's `activity_count`: a known 0 for every row the
# summary never counts (any other role, or metadata that is not valid JSON).
ACTIVITY_WEIGHT_SQL = f"CASE WHEN {ACTIVITY_ROW_SQL} THEN {ACTIVITY_SQL} ELSE 0 END"
# The select every display-row read starts with, so each row carries its weight.
DISPLAY_ROW_SELECT = f"SELECT *, {ACTIVITY_WEIGHT_SQL} AS activity_weight, rowid FROM messages "
