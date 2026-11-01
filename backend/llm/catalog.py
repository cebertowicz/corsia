"""DB-backed model catalog."""

from __future__ import annotations

from datetime import datetime, timezone

from infra.database import get_db, write_transaction
from llm.catalog_cloud import discover_cloud_models
from llm.catalog_network_models import discover_network_models


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _projected(rows, db) -> list[dict]:
    """Read Claude Code and Codex rows through what signed-in accounts list now."""
    from llm.harness_catalog import project_stored_rows

    try:
        return project_stored_rows([row for row in rows if row], db=db)
    finally:
        db.close()


def get_model(model_id: str) -> dict | None:
    """Look up a model by its short ID (e.g. 'claude-haiku')."""
    db = get_db()
    row = db.execute("SELECT * FROM models WHERE id = ?", (model_id,)).fetchone()
    return next(iter(_projected([row], db)), None)


def get_model_by_litellm(litellm_model: str) -> dict | None:
    """Reverse lookup: litellm model string → model row.

    For ``omlx@<server_id>/<provider_model_id>`` strings we also try a
    server_id-agnostic fallback, since a physical install keeps the same
    ``instance_id`` but can be represented by different server rows (and
    thus different server_ids) across restarts / DB moves. Old session rows
    reference an outdated server_id; this lets them resolve cleanly.
    """
    db = get_db()
    row = db.execute(
        "SELECT * FROM models WHERE litellm_model = ?", (litellm_model,)
    ).fetchone()

    if row is None and "@" in litellm_model:
        provider, _, rest = litellm_model.partition("@")
        if provider == "omlx":
            _, _, provider_model_id = rest.partition("/")
            if provider_model_id:
                row = db.execute(
                    "SELECT * FROM models WHERE provider = ? AND provider_model_id = ? AND available = 1",
                    (provider, provider_model_id),
                ).fetchone()
    return next(iter(_projected([row], db)), None)


def list_models(
    include_disabled: bool = True,
    include_unavailable: bool = True,
    provider: str | None = None,
    backend: str | None = None,
) -> list[dict]:
    """Return models from DB, optionally filtered."""
    clauses: list[str] = []
    params: list = []
    if not include_disabled:
        clauses.append("enabled = 1")
    if provider:
        clauses.append("provider = ?")
        params.append(provider)
    if backend:
        clauses.append("backend = ?")
        params.append(backend)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    db = get_db()
    rows = db.execute(
        f"SELECT * FROM models{where} ORDER BY provider, display_name", params
    ).fetchall()
    return [row for row in _projected(rows, db) if include_unavailable or row["available"]]


def get_selectable_models() -> list[dict]:
    """Return only models eligible for selectors (enabled + available)."""
    return list_models(include_disabled=False, include_unavailable=False)


def get_model_status_catalog_rows() -> list[dict]:
    """Return the canonical public-status rows in stable presentation order."""
    db = get_db()
    rows = db.execute(
        """
        SELECT * FROM models
        WHERE (harness = 'native' AND enabled = 1 AND available = 1)
           OR (harness IN ('claude-code', 'codex') AND enabled = 1)
        ORDER BY
          CASE harness
            WHEN 'native' THEN 0
            WHEN 'claude-code' THEN 1
            WHEN 'codex' THEN 2
            ELSE 3
          END,
          CASE WHEN harness = 'native' THEN provider END,
          CASE WHEN harness = 'native' THEN display_name END,
          display_name,
          id
        """
    ).fetchall()
    db.close()
    return [dict(row) for row in rows]


def is_valid_model_choice(litellm_model: str) -> bool:
    """Check if a litellm model string is a valid selection (enabled + available)."""
    row = get_model_by_litellm(litellm_model)
    return bool(row and row["enabled"] and row["available"])


def is_dispatchable_model(model: str | None) -> bool:
    """True when the canonical catalog can construct an execution target."""
    if not isinstance(model, str):
        return False
    m = model.strip()
    if not m:
        return False
    from llm.resolution import ModelUnavailableError, resolve_execution_target

    try:
        resolve_execution_target(m)
        return True
    except (ModelUnavailableError, ValueError):
        return False


def dispatch_form_error(model: str | None) -> str:
    """Shared repair-guidance message for a non-dispatchable model string.

    Used by both fail-loud sites (delegation create and native dispatch) so the
    guidance can't drift.
    """
    return (
        f"model {model!r} has no unambiguous canonical execution target; "
        "register or discover the exact model in the catalog before dispatch."
    )


def get_display_name(litellm_model: str) -> str:
    """Return display name for a litellm model string, falling back to the raw string."""
    m = get_model_by_litellm(litellm_model)
    if m:
        return m.get("alias") or m["display_name"]
    return litellm_model


def update_model(model_id: str, updates: dict) -> bool:
    """Update fields on a model row. Returns True if row existed."""
    allowed = {"enabled", "alias", "available", "context_window", "display_name", "description"}
    filtered = {k: v for k, v in updates.items() if k in allowed}
    if not filtered:
        return False
    filtered["updated_at"] = _now()
    with write_transaction(None, scope="update_model") as db:
        set_parts = ", ".join(f"{k} = ?" for k in filtered)
        params = list(filtered.values()) + [model_id]
        cursor = db.execute(f"UPDATE models SET {set_parts} WHERE id = ?", params)
        return cursor.rowcount > 0
