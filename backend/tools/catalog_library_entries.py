"""Catalog entries for reviewed-library tools."""

from tools.catalog_entry import ToolCatalogEntry


LIBRARY_CATALOG_ENTRIES: dict[str, ToolCatalogEntry] = {
    "skill_library": ToolCatalogEntry(
        "memory", summary="Find, install, update, or remove reviewed library skills",
        activity="Managing library skills",
    ),
}
