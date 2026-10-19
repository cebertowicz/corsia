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
}
