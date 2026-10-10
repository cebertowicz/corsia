"""Catalog identity and checkpoint enrichment for network model rows."""

from __future__ import annotations

import logging
import sqlite3

from infra.database import get_db
from llm.context_window import provider_window_facts


log = logging.getLogger(__name__)


def add_catalog_ids(models: list[dict], provider: str) -> None:
    """Enrich network rows with catalog IDs and runtime-resolved window facts."""
    if not provider:
        return
    try:
        db = get_db()
        try:
            rows, facts, overrides = provider_window_facts(provider, db=db)
        finally:
            db.close()
    except sqlite3.Error as exc:
        log.debug("network catalog enrichment failed: %s", exc)
        return
    catalog = {
        row["provider_model_id"]: (row["id"], window, overrides.get(row["id"]))
        for row, window in zip(rows, facts)
    }
    for model in models:
        entry = catalog.get(model["model_id"])
        if not entry:
            continue
        db_id, window, override = entry
        model["db_id"] = db_id
        model["provider_context_length"] = model.get("context_length")
        model["context_length"] = window.effective
        model["context_window_source"] = window.source
        model["context_window_override"] = override
