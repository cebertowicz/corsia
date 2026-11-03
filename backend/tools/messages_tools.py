"""The Messages tools that set up and read: ``messages_connect``, ``messages_chats``, ``messages_read``, ``messages_search``.

Reads open the Mac's Messages database read-only in a worker thread. A thread may read only while a
connected Messages row reaches it (the same "Where it works" rule as email); every state the Mac can
be in answers with one fixed sentence from ``tools.messages_texts``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from connections.messages import account
from infra.db_async import run_in_db
from tools import messages_db, messages_texts as texts
from tools.base import Tool
from tools.corsia_api import ApiCallError, conductor_user

log = logging.getLogger(__name__)


def limit_of(value: object) -> int:
    """How many rows: the integer asked for, 1 to 50, default 20."""
    return min(max(value, 1), 50) if isinstance(value, int) and not isinstance(value, bool) else 20


def _query(job: Callable[..., str], *args: Any) -> str:
    db = messages_db.open_db()
    try:
        return job(db, *args)
    finally:
        db.close()


async def read_messages(thread_id: object, job: Callable[..., str], *args: Any) -> str:
    """Run one read job for a thread that may use Messages; every refusal is a fixed sentence."""
    if not messages_db.is_mac():
        return texts.NOT_A_MAC
    if await account.usable_for_thread(thread_id) is None:
        return f"Error: {texts.NOT_CONNECTED}"
    try:
        return await asyncio.to_thread(_query, job, *args)
    except messages_db.MessagesError as exc:
        return f"Error: {exc}"
    except Exception:
        log.exception("Messages read failed")
        return f"Error: {texts.UNREADABLE}"


class MessagesConnectTool(Tool):
    self_describing_errors = True

    @property
    def name(self) -> str:
        return "messages_connect"

    @property
    def description(self) -> str:
        return texts.CONNECT

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "additionalProperties": False, "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        thread_id = kwargs.get("_thread_id")
        if not messages_db.is_mac():
            return texts.NOT_A_MAC
        if not isinstance(thread_id, str) or not thread_id:
            return "Error: current thread identity is unavailable; retry from a live turn."
        try:
            await run_in_db(conductor_user, thread_id)
        except ApiCallError:
            return texts.ONLY_CORSIA
        try:
            row, created = await account.connect(thread_id)
        except Exception:
            log.exception("messages_connect could not add the Messages row")
            return "Error: Couldn't turn on Messages."
        if row is None:
            return f"Error: {texts.NOT_REACHING}"
        counted = await read_messages(thread_id, lambda db: str(db.execute("SELECT COUNT(*) FROM chat").fetchone()[0]))
        if counted.startswith("Error: "):
            return f"{texts.ADDED if created else ''} {counted}".strip()
        return texts.READABLE.format(count=counted)


class MessagesChatsTool(Tool):
    self_describing_errors = True

    @property
    def name(self) -> str:
        return "messages_chats"

    @property
    def description(self) -> str:
        return texts.CHATS

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object", "additionalProperties": False,
            "properties": {"limit": {"type": "integer", "description": "How many chats; default 20, at most 50."}},
        }

    async def execute(self, **kwargs: Any) -> str:
        return await read_messages(kwargs.get("_thread_id"), messages_db.chats, limit_of(kwargs.get("limit")))


class MessagesReadTool(Tool):
    self_describing_errors = True

    @property
    def name(self) -> str:
        return "messages_read"

    @property
    def description(self) -> str:
        return texts.READ

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object", "additionalProperties": False, "required": ["chat_id"],
            "properties": {
                "chat_id": {"type": "string", "description": "A chat_id from messages_chats or messages_search."},
                "limit": {"type": "integer", "description": "How many texts; default 20, at most 50."},
            },
        }

    async def execute(self, **kwargs: Any) -> str:
        chat_id = str(kwargs.get("chat_id") or "")
        if not chat_id.isdigit():
            return texts.NEEDS_CHAT_ID
        return await read_messages(kwargs.get("_thread_id"), messages_db.read, int(chat_id), limit_of(kwargs.get("limit")))


class MessagesSearchTool(Tool):
    self_describing_errors = True

    @property
    def name(self) -> str:
        return "messages_search"

    @property
    def description(self) -> str:
        return texts.SEARCH

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object", "additionalProperties": False, "required": ["query"],
            "properties": {
                "query": {"type": "string", "description": "A word or phrase to find."},
                "limit": {"type": "integer", "description": "How many texts; default 20, at most 50."},
            },
        }

    async def execute(self, **kwargs: Any) -> str:
        query = kwargs.get("query")
        if not isinstance(query, str) or not query.strip():
            return texts.NEEDS_QUERY
        return await read_messages(kwargs.get("_thread_id"), messages_db.search, query.strip(), limit_of(kwargs.get("limit")))
