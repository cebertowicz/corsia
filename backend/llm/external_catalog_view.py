"""The provider card's view of one External endpoint's catalog rows.

Split from ``external_provider`` (discovery and dispatch): this reads the rows
discovery wrote, for the settings surface. Turning one on or off is the
catalog's own update by row id.
"""

from __future__ import annotations

from llm.external_provider import PREFIX, PROVIDER, served_model_id


def external_catalog_models(server_id: str) -> dict:
    """One External server's catalog rows in the provider card's shape.

    Enabled rows are the models the selector offers; disabled ones are listed
    so they can be turned back on. There is nothing to load: the provider runs
    them. A row without a known context window cannot run, and says so. The
    window comes from the one resolver, so a per-model checkpoint window the
    user saved counts here exactly as it does at dispatch.
    """
    from infra.database import get_db
    from llm.context_window import resolve_window_facts_for_rows, saved_overrides_by_catalog_id
    from llm.external_image_input import image_input_state

    db = get_db()
    rows = db.execute(
        "SELECT id, harness, provider_model_id, litellm_model, enabled, context_window, "
        "supports_vision, supports_tools, supports_reasoning, metadata_json FROM models "
        "WHERE provider = ? AND litellm_model LIKE ? AND available = 1 "
        "ORDER BY litellm_model",
        (PROVIDER, f"{PREFIX}{server_id}/%"),
    ).fetchall()
    overrides = saved_overrides_by_catalog_id(rows, db=db)
    shaped: dict[str, list[dict]] = {"loaded": [], "available": []}
    for row, window in zip(rows, resolve_window_facts_for_rows(rows, db=db)):
        pmid = served_model_id(row["litellm_model"])
        model = {
            "id": pmid,
            "model_id": pmid,
            "display_name": pmid,
            "db_id": row["id"],
            "context_length": window.effective,
            "provider_context_length": window.discovered,
            "context_window_source": window.source,
            "context_window_override": overrides.get(row["id"]),
            "vision": bool(row["supports_vision"]),
            "supports_tools": bool(row["supports_tools"]),
            "supports_reasoning": bool(row["supports_reasoning"]),
            # How image support was settled: detected, set by the user, or not yet.
            "image_input": image_input_state(row["metadata_json"]),
        }
        if not window.effective:
            model["unusable_reason"] = "context window unknown"
        shaped["loaded" if row["enabled"] else "available"].append(model)
    return shaped
