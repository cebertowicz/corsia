"""Shared step handler — DB persistence + WS broadcast for agent turn steps."""

from __future__ import annotations

import logging
import uuid
from typing import Any, Awaitable, Callable

from infra.database import get_db, write_transaction
from infra.time import utc_now_iso
from infra.db_async import run_in_db
from infra.context_receipts import attach_delivered_context_to_latest_assistant as _attach_delivered_context_to_latest_assistant
from infra.context_receipts import (
    ContextReceiptIdentity,
    context_usage_receipt,
    context_usage_summary, live_context_usage_payload,
)
from infra.queries import insert_message
from core.plan_turn_end import record_turn_end_logged
from core.turn_core import TurnIdentity
from tools.tool_display import tool_start_frame

log = logging.getLogger(__name__)


async def _step_seq(thread_id: str, message_id: str) -> int | None:
    """Return one persisted Step's indexed transcript position off the event loop."""
    from infra.db_async import run_in_db
    from infra.queries import message_transcript_seq

    try:
        return await run_in_db(message_transcript_seq, thread_id, message_id)
    except Exception:
        log.exception("native Step %s sequence read failed", message_id)
        return None


class StepHandler:
    """Persists agent turn steps and broadcasts them to connected clients."""

    def __init__(
        self,
        thread_id: str,
        model_info: dict,
        broadcast: Callable[[dict], Awaitable[None]],
        turn_id: str,
        extra_meta: dict | None = None,
    ):
        self.thread_id = thread_id
        self.model_info = model_info
        self.broadcast = broadcast
        self.turn_id = turn_id
        self._turn_identity: TurnIdentity | None = None
        self.extra_meta = extra_meta or {}
        self.step_ids: list[str] = []
        self.tool_hints: list[str] = []
        self.tool_calls: list[dict] = []
        self.tool_step_id: str | None = None
        self.tool_step_created_at: str | None = None
        self.tool_step_content = ""
        self.tool_step_reasoning: str | None = None
        self.tool_step_reasoning_provenance: dict[str, Any] | None = None
        self.reasoning_parts: list[str] = []
        self.reasoning_provenance: dict[str, Any] | None = None

    def bind_turn_identity(self, identity: TurnIdentity) -> None:
        """Bind the durable order once, after setup succeeds and start persists."""
        if identity.turn_id != self.turn_id:
            raise ValueError("StepHandler Turn ID differs from durable identity")
        if self._turn_identity is not None and self._turn_identity != identity:
            raise RuntimeError("StepHandler Turn identity is already bound")
        self._turn_identity = identity

    async def _emit(self, payload: dict) -> None:
        identity = self._turn_identity
        if identity is None:
            raise RuntimeError("StepHandler emitted before Turn identity was bound")
        await self.broadcast({**payload, **identity.frame_fields()})

    def _base_meta(self) -> dict:
        """Common metadata fields for DB writes."""
        meta = {
            "model": self.model_info.get("model"),
            "model_name": self.model_info.get("model_name"),
            "is_cloud": self.model_info.get("is_cloud", False),
        }
        meta.update(self.extra_meta)
        meta["turn_id"] = self.turn_id
        return meta

    async def handle(self, step_type: str, payload: dict):
        if step_type == "text":
            await self._handle_text(payload)
        elif step_type == "text_delta":
            await self._handle_text_delta(payload)
        elif step_type == "reasoning":
            await self._handle_reasoning(payload)
        elif step_type == "tool_calls_start":
            return await self._handle_tool_calls_start(payload)
        elif step_type == "tool_complete":
            return await self._handle_tool_complete(payload)
        elif step_type == "turn_complete":
            await self._handle_turn_complete(payload)
        elif step_type == "error":
            await self._handle_error(payload)
        elif step_type == "turn_cancelled":
            await self._handle_turn_cancelled(payload)
        elif step_type == "tool_calls_done":
            await self._handle_tool_calls_done(payload)
        elif step_type == "tool_start":
            await self._handle_tool_start(payload)
        elif step_type == "context_usage":
            await self._handle_context_usage(payload)
        else:
            raise ValueError(
                f"Unhandled step type '{step_type}' — "
                f"known types: text, text_delta, reasoning, tool_calls_start, "
                f"tool_start, tool_complete, context_usage, turn_complete, "
                f"error, turn_cancelled, tool_calls_done"
            )

    def _take_reasoning(self, meta: dict) -> None:
        """Move buffered reasoning onto a new Step's metadata."""
        if self.reasoning_parts:
            meta["reasoning"] = "".join(self.reasoning_parts)
        if self.reasoning_provenance is not None:
            meta["reasoning_provenance"] = self.reasoning_provenance
        self.reasoning_parts.clear()
        self.reasoning_provenance = None

    async def _handle_text(self, payload: dict):
        step_id = str(uuid.uuid4())
        step_now = utc_now_iso()
        step_content = payload["content"]
        meta = self._base_meta()
        meta["tool_hints"] = self.tool_hints
        meta["step_type"] = "text"
        self._take_reasoning(meta)

        def _persist():
            with write_transaction(None, scope="persist_step") as db:
                insert_message(self.thread_id, "assistant", step_content, meta,
                               msg_id=step_id, db=db, created_at=step_now)

        await run_in_db(_persist)

        self.step_ids.append(step_id)
        pending_hints = self.tool_hints[:]
        pending_tool_calls = self.tool_calls[:]
        self.tool_hints = []
        self.tool_calls = []

        ws_meta = self._base_meta()
        ws_meta["tool_hints"] = pending_hints
        ws_meta["tool_calls"] = pending_tool_calls
        if meta.get("reasoning"):
            ws_meta["reasoning"] = meta["reasoning"]
        if meta.get("reasoning_provenance"):
            ws_meta["reasoning_provenance"] = meta["reasoning_provenance"]

        await self._emit({
            "type": "step_text",
            "thread_id": self.thread_id,
            "message_id": step_id,
            "content": step_content,
            "metadata": ws_meta,
            "created_at": step_now,
            "seq": await _step_seq(self.thread_id, step_id),
            "finalized_tool_ids": [],
        })

    async def _handle_text_delta(self, payload: dict):
        """Broadcast a content chunk without persisting — final text is saved by _handle_text."""
        await self._emit({
            "type": "text_delta",
            "thread_id": self.thread_id,
            "content": payload["content"],
        })

    async def _handle_reasoning(self, payload: dict):
        content = payload["content"]
        if content:
            self.reasoning_parts.append(content)
        if "provenance" in payload:
            from core.reasoning_provenance import normalize_reasoning_provenance
            self.reasoning_provenance = normalize_reasoning_provenance(
                payload.get("provenance")
            )
        if content:
            await self._emit({
                "type": "step_reasoning",
                "thread_id": self.thread_id,
                "content": content,
            })

    async def _handle_context_usage(self, payload: dict) -> None:
        """Broadcast one measured boundary without adding a transcript write."""
        usage = payload.get("context_usage")
        identity = payload.get("_context_usage_identity")
        valid = isinstance(usage, dict) and isinstance(identity, ContextReceiptIdentity)
        if not valid:
            raise ValueError("context_usage step requires usage and exact provider identity")
        await self._emit({
            "type": "context_usage_updated",
            "thread_id": self.thread_id,
            "context_usage": live_context_usage_payload(usage),
        })

    async def _handle_tool_start(self, payload: dict):
        """Emit a tool_start WS event during streaming."""
        await self._emit(tool_start_frame(
            payload.get("thread_id", self.thread_id),
            payload["tool_id"],
            payload["name"],
            payload.get("hint", ""),
            payload.get("args"),
            payload.get("args", ""),
        ))

    async def _handle_tool_calls_start(self, payload: dict) -> str:
        """Persist the batch's Assistant Step and return its exact id."""
        if self.tool_step_id is not None:
            raise RuntimeError("cannot start an Assistant Step before the prior step settles")
        tc_step_id = str(uuid.uuid4())
        tc_step_now = utc_now_iso()
        tc_content = payload.get("content", "") or ""
        tc_dicts = payload.get("tool_call_dicts", [])

        for tool in payload["tools"]:
            self.tool_hints.append(tool["hint"])
            self.tool_calls.append({
                "id": tool["id"],
                "name": tool["name"],
                "hint": tool["hint"],
                "args": tool.get("args", ""),
                "result": None,
            })

        meta = self._base_meta()
        meta["step_type"] = "tool_calls"
        meta["tool_hints"] = [t["hint"] for t in payload["tools"]]
        meta["llm_tool_calls"] = tc_dicts
        self._take_reasoning(meta)

        def _persist():
            with write_transaction(None, scope="persist_tool_calls") as db:
                insert_message(self.thread_id, "assistant", tc_content, meta,
                               msg_id=tc_step_id, db=db, created_at=tc_step_now)

        await run_in_db(_persist)
        self.step_ids.append(tc_step_id)
        self.tool_step_id = tc_step_id
        self.tool_step_created_at = tc_step_now
        self.tool_step_content = tc_content
        self.tool_step_reasoning = meta.get("reasoning")
        self.tool_step_reasoning_provenance = meta.get("reasoning_provenance")

        if not payload.get("skip_broadcast", False):
            for tool in payload["tools"]:
                await self._emit(tool_start_frame(
                    self.thread_id, tool["id"], tool["name"], tool["hint"],
                    tool.get("args"), tool.get("args", ""),
                ))
        return tc_step_id

    async def _handle_tool_complete(self, payload: dict):
        from core.native_tool_result import handle_native_tool_complete

        return await handle_native_tool_complete(self, payload, (run_in_db, write_transaction))

    async def _handle_tool_calls_done(self, payload: dict):
        if self.tool_step_id is None or self.tool_step_created_at is None:
            raise RuntimeError("tool batch completed without an active Assistant Step")
        finalized_ids = list(payload.get("tool_ids") or [])
        declared_ids = [call["id"] for call in self.tool_calls]
        if finalized_ids != declared_ids:
            raise RuntimeError("finalized tool IDs differ from the active Assistant Step")
        if any(call.get("result") is None for call in self.tool_calls):
            raise RuntimeError("Assistant Step finalized before every tool result settled")

        metadata = self._base_meta()
        metadata["tool_hints"] = self.tool_hints[:]
        metadata["tool_calls"] = [dict(call) for call in self.tool_calls]
        if self.tool_step_reasoning:
            metadata["reasoning"] = self.tool_step_reasoning
        if self.tool_step_reasoning_provenance is not None:
            metadata["reasoning_provenance"] = self.tool_step_reasoning_provenance
        await self._emit({
            "type": "step_text",
            "thread_id": self.thread_id,
            "message_id": self.tool_step_id,
            "content": self.tool_step_content,
            "metadata": metadata,
            "created_at": self.tool_step_created_at,
            "seq": await _step_seq(self.thread_id, self.tool_step_id),
            "finalized_tool_ids": finalized_ids,
        })

        self.tool_hints = []
        self.tool_calls = []
        self.tool_step_id = None
        self.tool_step_created_at = None
        self.tool_step_content = ""
        self.tool_step_reasoning = None
        self.tool_step_reasoning_provenance = None

    async def _persist_terminal_context(
        self,
        payload: dict,
    ) -> dict:
        """Attach one terminal CTX receipt and return its WS projection."""
        context_usage = payload.get("context_usage")
        if not context_usage:
            return {}
        receipt = context_usage_summary(context_usage)
        context_message_id = None
        context_identity = payload.get("_context_usage_identity")
        if isinstance(context_identity, ContextReceiptIdentity):
            try:
                attached = await run_in_db(
                    _attach_delivered_context_to_latest_assistant,
                    thread_id=self.thread_id,
                    step_ids=self.step_ids[:],
                    context_usage=context_usage,
                    identity=context_identity,
                )
                if attached is not None:
                    context_message_id, receipt = attached
            except Exception as exc:
                from loguru import logger

                logger.warning(
                    f"context receipt attach failed for {self.thread_id}: {exc}"
                )
        terminal_context = {"context_usage": live_context_usage_payload(context_usage)}
        if context_message_id:
            terminal_context["context_message_id"] = context_message_id
        return terminal_context

    async def _handle_turn_complete(self, payload: dict):
        terminal_context = await self._persist_terminal_context(payload)
        terminal_context.setdefault("context_usage", None)
        ws_payload = {
            "type": "turn_complete",
            "thread_id": self.thread_id,
            "step_ids": self.step_ids,
            **terminal_context,
        }
        await self._emit(ws_payload)

        try:
            def _query():
                db = get_db()
                row = db.execute(
                    """SELECT s.title, s.updated_at,
                       (SELECT substr(m.content, 1, 100) FROM messages m
                        WHERE m.thread_id = s.id AND m.role IN ('user', 'assistant')
                        ORDER BY m.created_at DESC LIMIT 1) as last_message
                       FROM threads s WHERE s.id = ?""",
                    (self.thread_id,)
                ).fetchone()
                db.close()
                return dict(row) if row else None

            row = await run_in_db(_query)
            if row:
                await self._emit({
                    "type": "session_updated",
                    "thread_id": self.thread_id,
                    "title": row["title"],
                    "last_message": row["last_message"],
                    "updated_at": row["updated_at"],
                })
        except Exception as e:
            from loguru import logger
            logger.warning(f"sidebar refresh after turn completion failed: {e}")

        # Plan-liveness: arm/cancel a wake so unfinished plan work resumes past
        # the turn boundary instead of dying. Never break turn completion on it.
        await record_turn_end_logged(
            self.thread_id, "plan-liveness arm after turn completion failed",
            "parent drive after turn completion failed",
        )

    async def _handle_error(self, payload: dict):
        terminal_context = await self._persist_terminal_context(payload)
        err_id = str(uuid.uuid4())
        err_now = utc_now_iso()
        err_content = payload.get("message", "An error occurred")
        err_meta = {
            "type": "llm_error",
            "model": self.model_info.get("model"),
            "turn_id": self.turn_id,
        }

        def _persist():
            with write_transaction(None, scope="persist_error") as db:
                insert_message(self.thread_id, "system", err_content, err_meta,
                               msg_id=err_id, db=db, created_at=err_now)

        await run_in_db(_persist)

        await record_turn_end_logged(
            self.thread_id, "plan-liveness error wake failed; parent may wait",
            "parent drive after turn error failed; parent may wait",
            level="ERROR", outcome="error", error=err_content, turn_id=self.turn_id,
        )

        ws_payload = {
            "type": "error",
            "thread_id": self.thread_id,
            "message": err_content,
            **terminal_context,
        }
        await self._emit(ws_payload)

    async def _handle_turn_cancelled(self, payload: dict):
        effect_type = (
            "turn_interrupted"
            if payload.get("_replacement_requested")
            else "turn_cancelled"
        )
        terminal_context = await self._persist_terminal_context(payload)
        await self._emit({
            "type": effect_type,
            "thread_id": self.thread_id,
            **terminal_context,
        })
