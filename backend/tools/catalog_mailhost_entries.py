"""Catalog entries for the mail-host account tool."""
from __future__ import annotations

from tools.catalog_entry import ToolCatalogEntry

MAILHOST_CATALOG_ENTRIES: dict[str, ToolCatalogEntry] = {
    "mail_host_connect": ToolCatalogEntry(
        "google", status="active", authority="conductor-api",
        summary="Add the person's email account on another host and get where they make its app password",
        activity="Adding email account",
    ),
}
