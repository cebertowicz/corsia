"""Catalog entries for Microsoft sign-in tools."""
from __future__ import annotations

from tools.catalog_entry import ToolCatalogEntry

MICROSOFT_CATALOG_ENTRIES: dict[str, ToolCatalogEntry] = {
    "microsoft_connect": ToolCatalogEntry(
        "microsoft", status="active", authority="conductor-api",
        summary="Start the person's Microsoft sign-in and get the link they open",
        activity="Starting Microsoft sign-in",
    ),
    "microsoft_status": ToolCatalogEntry(
        "microsoft", status="active", authority="conductor-api",
        summary="Read the Microsoft sign-in's email, account kind and status",
        activity="Checking Microsoft sign-in",
    ),
    **{name: ToolCatalogEntry("microsoft", status="active", authority="conductor-api", summary=summary, activity=activity) for name, summary, activity in (
        ("microsoft_mail_draft", "Save a Microsoft mail draft for review", "Saving Microsoft mail draft"),
        ("microsoft_mail_search", "Search Microsoft mail in one selected account", "Searching Microsoft mail"),
        ("microsoft_mail_read", "Read a screened Microsoft message", "Reading Microsoft mail"),
        ("microsoft_mail_send", "Send Microsoft mail from one selected account", "Sending Microsoft mail"),
        ("microsoft_calendar_list", "List Microsoft calendar events", "Listing Microsoft calendar"),
        ("microsoft_calendar_draft", "Save a Microsoft calendar change for review", "Saving Microsoft calendar draft"),
        ("microsoft_calendar_create", "Create a Microsoft calendar event", "Creating Microsoft calendar event"),
        ("microsoft_calendar_update", "Update a Microsoft calendar event", "Updating Microsoft calendar event"),
        ("microsoft_files_list", "List Microsoft files", "Listing Microsoft files"),
        ("microsoft_files_read", "Read Microsoft file metadata", "Reading Microsoft file"),
        ("microsoft_files_upload", "Upload a Microsoft text file", "Uploading Microsoft file"),
    )},
}
