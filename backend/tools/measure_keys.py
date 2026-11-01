"""Agent-facing tool: mint, list and revoke the keys an outside tool sends measures with."""
from __future__ import annotations

import json
from typing import Any

from infra import measure_keys, otlp_state
from infra.database import get_db
from infra.db_async import run_in_db
from infra.product_events import emit_product_event
from tools.base import Tool

_ACTIONS = ("mint", "list", "revoke")


def _context(kwargs: dict[str, Any]):
    from tools.delegation import _require_context
    return _require_context(kwargs)


class MeasureKeyTool(Tool):
    @property
    def name(self) -> str:
        return "measure_key"

    @property
    def description(self) -> str:
        return ("Manage the keys an outside tool (anything that speaks OpenTelemetry) uses to send your declared "
                "measure events to CORSIA. action mint (needs label) returns the key once, with the endpoint to "
                "send to; CORSIA keeps only a hash. CORSIA mints it for you and it works only on this machine; it "
                "is not a person's credential, so putting it in the outside tool's env or config is the intended "
                "use. Never write it into a file CORSIA tracks or a message. The key stamps your identity: records "
                "may not claim another. "
                "action list shows your keys (never the key itself) and the current endpoint, which can change if the "
                "port is taken at a later start; action revoke (needs key_id) stops one.")

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "additionalProperties": False,
                "properties": {"action": {"type": "string", "enum": list(_ACTIONS)},
                               "label": {"type": "string", "description": "mint: what the key is for, up to 80 characters"},
                               "key_id": {"type": "string", "description": "revoke: the key_id from list"}},
                "required": ["action"]}

    async def execute(self, **kwargs: Any) -> str:
        context = _context(kwargs)
        if isinstance(context, str):
            return context
        thread_id, actor_row = context
        action = kwargs.get("action")
        if action == "list":
            keys = await run_in_db(lambda: measure_keys.list_keys(get_db(), thread_id))
            return json.dumps({"keys": keys, "endpoint": otlp_state.endpoint()}, sort_keys=True)
        if action == "revoke":
            key_id = kwargs.get("key_id")
            if not isinstance(key_id, str):
                return "Error: key_id is required"
            if not await run_in_db(measure_keys.revoke, thread_id, key_id):
                return "Error: you have no live key with that key_id"
            return json.dumps({"revoked": key_id})
        if action == "mint":
            endpoint = otlp_state.endpoint()
            if endpoint is None:
                return "Error: the measure receiver is not listening, so a key would be useless. Restart CORSIA or free its port, then retry."
            label = kwargs.get("label")
            if not isinstance(label, str):
                return "Error: label is required"
            try:
                key_id, key = await run_in_db(measure_keys.mint, actor_row["user_id"], thread_id, label)
            except ValueError as exc:
                return f"Error: {exc}"
            send = (f"POST {endpoint}/v1/logs (OTLP/HTTP, protobuf or JSON) with Authorization: Bearer <key>; "
                    "each record needs an event.name attribute you declared in measures.yaml. If the port is taken at a later "
                    "start CORSIA uses the next free one; action list shows the current endpoint.")
            emit_product_event("measure_key", "minted", thread_id=thread_id)
            return json.dumps({"key_id": key_id, "key": key, "endpoint": endpoint, "send": send, "shown_once": True})
        return f"Error: action must be one of {', '.join(_ACTIONS)}"
