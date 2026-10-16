"""Agent tool for acknowledging System and live bulletin notices."""
from __future__ import annotations

import json
from typing import Any

from tools.base import Tool


def _record_disposition(
    bulletin_id: str, state: str, disposition: str, thread_id: str | None,
) -> tuple[str, str | None]:
    """Run every synchronous disposition operation in the DB worker."""
    import infra.bulletin_ledger as bulletin_ledger
    from infra.corsia_agent import get_corsia_agent
    from infra.database import get_db, write_transaction

    db = get_db()
    try:
        if not thread_id or get_corsia_agent(db=db) != thread_id:
            return (
                "Error: Only the CORSIA agent records bulletin dispositions. "
                "Bulletins are delivered to it; no action is needed from you.",
                None,
            )
        with write_transaction(db, scope="bulletin_disposition"):
            source = bulletin_ledger.bulletin_source(db, bulletin_id)
            if source is None:
                return (
                    "Error: Unknown bulletin id. Use an id from a delivered bulletin notice "
                    "or the current pending System-change notice.",
                    None,
                )
            if not bulletin_ledger.record_disposition(db, bulletin_id, state, disposition):
                current = bulletin_ledger.delivery_state(db, bulletin_id)
                return (
                    f"Error: Bulletin {bulletin_id} was never delivered to you (state: {current}), so it "
                    "takes no disposition. Record dispositions only for ids from a notice you received.",
                    None,
                )
            reconciled = (
                bulletin_ledger.mark_system_reconciled(db, bulletin_id)
                if source == "system_change" and state in {"acted", "not_applicable"}
                else None
            )
        result: dict[str, object] = {"id": bulletin_id, "state": state, "recorded": True}
        if reconciled is not None:
            result["reconciled"] = reconciled
        return json.dumps(result), source
    finally:
        db.close()


class BulletinDispositionTool(Tool):
    """Record the CORSIA agent's disposition for a delivered bulletin."""

    self_describing_errors = True

    @property
    def name(self) -> str:
        return "bulletin_disposition"

    @property
    def description(self) -> str:
        return "Record the CORSIA agent's disposition for a System change or bulletin"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "id": {"type": "string", "description": "The System-change or bulletin id from the notice"},
                "state": {"type": "string", "enum": ["acted", "not_applicable", "deferred"]},
                "disposition": {"type": "string", "description": "What was evaluated or why action is deferred"},
            },
            "required": ["id", "state", "disposition"],
        }

    async def execute(self, id: str, state: str, disposition: str, _thread_id: str | None = None, **_ignored: Any) -> str:
        from infra.db_async import run_in_db
        from infra.product_events import emit_product_event

        result, source = await run_in_db(_record_disposition, id, state, disposition, _thread_id)
        if source is not None:
            emit_product_event("bulletin", "disposition", properties={"source": source, "state": state})
        return result
