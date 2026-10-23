"""Google tool catalog declarations."""

from __future__ import annotations

from tools.catalog_entry import ToolCatalogEntry

_Entry = ToolCatalogEntry

GOOGLE_CATALOG_ENTRIES: dict[str, ToolCatalogEntry] = {
    "email_search": _Entry(
        "google", status="active", authority="conductor-api",
        summary="Search the person's Gmail: headers only, no body text",
        activity="Searching mail",
    ),
    "email_read": _Entry(
        "google", status="active", authority="conductor-api",
        summary="Read one email after the local screen; a flagged message returns no body",
        activity="Reading mail",
    ),
    "google_connect": _Entry(
        "google", status="active", authority="conductor-api",
        summary="Start the person's Google sign-in and get the link they open",
        activity="Starting Google sign-in",
    ),
    "google_status": _Entry(
        "google", status="active", authority="conductor-api",
        summary="Read the Google sign-in's email, scopes and status",
        activity="Checking Google sign-in",
    ),
    "email_draft": _Entry(
        "google", status="active", authority="conductor-api",
        summary="Save an email as a draft document for the person to review; nothing is sent",
        activity="Drafting email",
    ),
    "email_send": _Entry(
        "google", status="active", authority="conductor-api",
        summary="Send a saved email draft after the person says to send it",
        serious_effect=True,
        activity="Sending email",
    ),
    "calendar_list": _Entry(
        "google", status="active", authority="conductor-api",
        summary="List the person's Google Calendar events, up to 31 days at a time",
        activity="Reading calendar",
    ),
    "calendar_draft_invite": _Entry(
        "google", status="active", authority="conductor-api",
        summary="Save a calendar invite as a draft document for the person to review; nothing is sent",
        activity="Drafting invite",
    ),
    "calendar_send_invite": _Entry(
        "google", status="active", authority="conductor-api",
        summary="Send a saved invite draft after the person says to send it; attendees get emails",
        serious_effect=True,
        activity="Sending invite",
    ),
}
