"""Identify canonical and unbound results for a provider tool echo."""

from core import _tool_result_delivery_support as support
from infra.delivery_states import TOOL_RESULT_QUARANTINED_UNBOUND


def ranked_echo_matches(
    rows,
    *,
    invocation_id: str | None,
    provider_tool_call_id: str,
):
    """Prefer executed results, then authored stubs, then quarantined writes."""
    quarantined = [
        row
        for row in rows
        if support.metadata_object(row["metadata"]).get(
            "tool_result_binding_state"
        )
        == TOOL_RESULT_QUARANTINED_UNBOUND
    ]
    authored = [
        row
        for row in rows
        if support.metadata_object(row["metadata"]).get(
            "synthetic_tool_result"
        )
    ]
    executed = [row for row in rows if row not in authored and row not in quarantined]
    for candidates in (executed, authored, quarantined):
        matches = []
        if invocation_id is not None:
            matches = [
                row
                for row in candidates
                if support.result_from_row(row).invocation_id
                == invocation_id
            ]
        if not matches:
            matches = [
                row
                for row in candidates
                if support.result_from_row(row).provider_tool_call_id
                == provider_tool_call_id
            ]
        if matches:
            return matches
    return []


def has_unbound_match(rows, *, db, thread_id: str, assistant_step_id: str,
                      provider_tool_call_id: str) -> bool:
    """Detect an unbound result that could belong to this declared call."""
    for row in rows:
        result = support.result_from_row(row)
        if result.provider_tool_call_id is not None:
            continue
        metadata = support.metadata_object(row["metadata"])
        try:
            support.select_provider_call(
                db=db, thread_id=thread_id, assistant_step_id=assistant_step_id,
                provider_tool_call_id=provider_tool_call_id, name=result.name,
                arguments=metadata.get("arguments"),
            )
        except support.ToolResultBindingError:
            continue
        return True
    return False
