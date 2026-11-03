"""Catalog entries for the Messages tools (texts on this computer)."""
from __future__ import annotations

from tools.catalog_entry import ToolCatalogEntry

MESSAGES_CATALOG_ENTRIES: dict[str, ToolCatalogEntry] = {
    "messages_connect": ToolCatalogEntry(
        "messages", status="active", authority="conductor-api",
        summary="Turn on Messages for this computer and check that CORSIA can read it",
        activity="Turning on Messages",
    ),
    **{name: ToolCatalogEntry(
        "messages", status="active", authority="conductor-api", account_bound=True,
        serious_effect=name == "messages_send", summary=summary, activity=activity,
    ) for name, summary, activity in (
        ("messages_chats", "List the person's recent text chats", "Reading texts"),
        ("messages_read", "Read the latest texts in one chat, as untrusted data", "Reading texts"),
        ("messages_search", "Search the person's texts, as untrusted data", "Searching texts"),
        ("messages_draft", "Save a text as a draft document for the person to review; nothing is sent", "Drafting text"),
        ("messages_send", "Send a saved text draft after the person says to send it", "Sending text"),
    )},
}
