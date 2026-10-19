"""Catalog entries for agent-declared KPI measures."""

from __future__ import annotations

from tools.catalog_entry import ToolCatalogEntry

MEASUREMENT_CATALOG_ENTRIES: dict[str, ToolCatalogEntry] = {
    "measure_sync": ToolCatalogEntry(
        "measurement", status="active", authority="measurement-write",
        summary="Load this agent's measures.yaml declaration", activity="Syncing measures",
    ),
    "measure_status": ToolCatalogEntry(
        "measurement", status="active", authority="measurement-read",
        summary="Read this agent's recorded measure rows, drops and sync state",
        activity="Reading measure status",
    ),
}
