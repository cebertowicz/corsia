"""Model-history projection: durable rows to provider messages, and agent framing."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from core.message_row import call_id, parse_meta
from core.reasoning_provenance import reasoning_content_for_typed_replay
from core.reply_context import REPLY_CONTEXT_KEY, reply_context_lines
from core.tool_lineage import resolve_tool_lineage
from core.viewing_context import VIEWING_DOCUMENT_KEY, viewing_context_line
from infra.delivery_states import TOOL_RESULT_QUARANTINED_UNBOUND


#: Stamped on a human message at admission; its stored presence, never the
#: current code, decides whether the reply footer renders, so a row admitted
#: before the stamp existed keeps rendering exactly as it was cached.
REPLY_VIA_POST_UPDATE_KEY = "reply_via_post_update"

#: Metadata that marks an input as written by an agent, a parent's completion
#: or a routed event: none of them is a human message. Admission stamps by
#: this rule and the envelope renders by it, so the two cannot drift.
NON_HUMAN_MARKERS = ("sender_kind", "completion", "routed_event")

#: The human-message footer: the reply affordance, then the no-reply line.
HUMAN_REPLY_FOOTER = (
    '[To reply, call post_update(text="<your reply>")]\n'
    "If no reply is needed, do not send one."
)


def _safe_agent_header_field(value: str) -> str:
    """Keep model-visible agent-send envelope fields structurally inert."""
    cleaned = " ".join(value.split())
    cleaned = cleaned.replace("[", "(").replace("]", ")").replace('"', "'")
    return cleaned[:120]


@dataclass(frozen=True)
class AgentEnvelopeRender:
    """Canonical model input plus CORSIA-owned framing components."""

    content: str
    body_content: str
    managed_prefix: str = ""
    managed_suffix: str = ""

    @property
    def managed_content(self) -> str:
        return f"{self.managed_prefix}{self.managed_suffix}"


def render_agent_envelope(
    text: str,
    metadata: dict[str, Any] | None,
) -> AgentEnvelopeRender:
    """Render stored provenance (a pause, an agent send) and keep its framing."""
    lead = ""
    if paused_since := (metadata or {}).get("plan_paused_since"):
        from prompts.plan_wakes import paused_line

        lead = paused_line(str(paused_since))
    if (metadata or {}).get(REPLY_VIA_POST_UPDATE_KEY) is True and not any(
        (metadata or {}).get(key) for key in NON_HUMAN_MARKERS
    ):
        lead += viewing_context_line((metadata or {}).get(VIEWING_DOCUMENT_KEY))
        lead += reply_context_lines((metadata or {}).get(REPLY_CONTEXT_KEY))
    if not metadata or metadata.get("sender_kind") != "agent":
        footer = (
            f"\n{HUMAN_REPLY_FOOTER}"
            if (metadata or {}).get(REPLY_VIA_POST_UPDATE_KEY) is True
            else ""
        )
        return AgentEnvelopeRender(
            f"{lead}{text}{footer}", text,
            managed_prefix=lead, managed_suffix=footer,
        )
    from_title = _safe_agent_header_field(str(metadata.get("from_title") or "unknown"))
    from_thread = str(metadata.get("from_thread") or "")
    scope = metadata.get("scope")
    header = f'[AGENT-SEND — from "{from_title}" (thread {from_thread})'
    if scope:
        header += f", scope {_safe_agent_header_field(str(scope))}"
    header += "]"
    prefix = f"{lead}{header}\n"
    reply = f'[To reply, call delegate_send(thread_id="{from_thread}", message="<your reply>")]'
    suffix = f"\n{reply}" if from_thread else ""
    return AgentEnvelopeRender(
        content=f"{prefix}{text}{suffix}",
        body_content=text,
        managed_prefix=prefix,
        managed_suffix=suffix,
    )


def frame_agent_envelope(text: str, metadata: dict[str, Any] | None) -> str:
    """Render persisted agent-send provenance into its canonical model frame."""
    return render_agent_envelope(text, metadata).content


def _llm_tool_call(call: dict) -> dict:
    """Normalize a durable new or legacy call definition for model history."""
    function = call.get("function")
    if isinstance(function, dict):
        return call
    arguments = call.get("args", "")
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments)
    return {
        "id": call_id(call),
        "type": "function",
        "function": {
            "name": call.get("name", ""),
            "arguments": arguments,
        },
    }


def _tool_history_message(
    row: dict,
    *,
    tool_result_budget_tokens: int | None,
) -> dict:
    from history.types import PortableMessage
    from core.tool_result_projection import render_tool_result_for_model

    meta = parse_meta(row.get("metadata"))
    source = PortableMessage(
        message_id=str(row.get("id") or ""),
        seq=int(row.get("seq") or 0),
        turn_id=meta.get("turn_id"),
        role="tool",
        content=row.get("content", "") or "",
        created_at=str(row.get("created_at") or ""),
        assistant_step_id=meta.get("assistant_step_id"),
        tool_call_id=meta.get("tool_call_id"),
        tool_name=meta.get("name"),
        is_error=meta.get("is_error") is True,
        synthetic_tool_result=meta.get("synthetic_tool_result") is True,
        synthetic_reason=meta.get("synthetic_reason"),
        content_sha256=meta.get("tool_result_sha256"),
        content_byte_count=meta.get("tool_result_byte_count"),
    )
    return {
        "role": "tool",
        "tool_call_id": meta.get("tool_call_id", ""),
        "name": meta.get("name", ""),
        "content": render_tool_result_for_model(
            str(row.get("thread_id") or ""),
            source,
            budget_tokens=tool_result_budget_tokens,
        ),
    }


def is_system_context_event(row: dict) -> bool:
    """Return whether a visible system row was also provider input."""
    if row.get("role") != "system":
        return False
    meta = parse_meta(row.get("metadata"))
    return (
        meta.get("type") == "context_event"
        and meta.get("event_disposition") in {"wake", "passive"}
    )


def _is_quarantined_unbound_tool_row(row: dict) -> bool:
    """True for a tool result durably marked as never provider-acknowledged."""
    if row.get("role") != "tool":
        return False
    meta = parse_meta(row.get("metadata"))
    return (
        meta.get("tool_result_binding_state")
        == TOOL_RESULT_QUARANTINED_UNBOUND
    )


def db_to_llm_history(
    rows: list[dict],
    *,
    tool_result_budget_tokens: int | None = None,
) -> list[dict]:
    """Project rows into model history with each tool result beside its call."""
    # A quarantined-unbound tool result is durable evidence that its provider
    # call was never acknowledged: it is not part of provider truth, so replay
    # excludes it and the declaring call keeps its interruption stub. Real
    # (bound or binding-pending) results keep the loud strict path below.
    replay_rows = [row for row in rows if not _is_quarantined_unbound_tool_row(row)]
    lineage = resolve_tool_lineage(replay_rows)
    messages: list[dict] = []

    for row in replay_rows:
        role = row.get("role", "")
        content = row.get("content", "") or ""
        if is_system_context_event(row):
            # The row is attributed as a system event in the visible transcript,
            # but the live provider boundary consumed it as the Turn's inbound
            # input. Recast it identically on replay so later cold starts keep
            # the Turn boundary and its guidance.
            messages.append({"role": "user", "content": content})
            continue
        if role in {"system", "tool"}:
            continue
        if role == "user":
            meta = parse_meta(row.get("metadata"))
            runtime_context = meta.get("runtime_context")
            if runtime_context:
                content = f"{content}\n\n{runtime_context}"
            messages.append({"role": "user", "content": content})
            continue
        if role != "assistant":
            continue

        meta = parse_meta(row.get("metadata"))
        assistant: dict = {"role": "assistant", "content": content}
        source_tool_calls = meta.get("llm_tool_calls") or meta.get("tool_calls") or []
        tool_calls = [
            _llm_tool_call(call)
            for call in source_tool_calls
            if isinstance(call, dict)
        ]
        if tool_calls:
            assistant["tool_calls"] = tool_calls
        reasoning_content = reasoning_content_for_typed_replay(meta)
        if reasoning_content is not None:
            assistant["reasoning_content"] = reasoning_content
        messages.append(assistant)

        step_id = row.get("id")
        step_results = lineage.get(step_id, {}) if isinstance(step_id, str) else {}
        for call in tool_calls or []:
            if not isinstance(call, dict):
                continue
            result_row = step_results.get(call_id(call))
            if result_row is not None:
                messages.append(_tool_history_message(
                    result_row,
                    tool_result_budget_tokens=tool_result_budget_tokens,
                ))

    return messages
