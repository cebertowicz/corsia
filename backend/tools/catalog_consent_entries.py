"""Catalog metadata for the consent-gated reads of the person's other tools."""

from __future__ import annotations

from tools.catalog_entry import ToolCatalogEntry

_Entry = ToolCatalogEntry


CONSENT_CATALOG_ENTRIES: dict[str, ToolCatalogEntry] = {
    "project_discovery_list": _Entry(
        "projects", authority="consented-history-read",
        summary="List the folders the user works in, after their consent decision",
        activity="Finding projects",
    ),
    "agent_import": _Entry(
        "memory", authority="consented-history-read",
        summary=(
            "CORSIA agent: bring over what the person's other tools know, "
            "and their skills, after their consent decision"
        ),
        activity="Bringing things over",
    ),
}
