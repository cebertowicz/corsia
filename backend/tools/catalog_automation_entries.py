"""Catalog entries for a thread's schedules and the event rules it writes."""

from __future__ import annotations

from tools.catalog_entry import ToolCatalogEntry

AUTOMATION_CATALOG_ENTRIES: dict[str, ToolCatalogEntry] = {
    "automation_schedule": ToolCatalogEntry(
        "automations", status="substrate", authority="automation-write",
        summary="Schedule a clock or cadence wake for this thread",
        activity="Scheduling",
    ),
    "automation_cancel": ToolCatalogEntry(
        "automations", status="substrate", authority="automation-write",
        summary="Cancel one of this thread's schedules",
        activity="Cancelling automation",
    ),
    "automation_list": ToolCatalogEntry(
        "automations", status="substrate", authority="automation-read",
        summary="List this thread's schedules",
        activity="Listing automations",
    ),
    "event_rules": ToolCatalogEntry(
        "automations", status="active", authority="event-rules-write",
        summary="Get woken, with your own note, when a connected app's event matches; or stop one",
        activity="Setting event rules",
    ),
}
