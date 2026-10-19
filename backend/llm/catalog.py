"""DB-backed model catalog."""

from __future__ import annotations

from datetime import datetime, timezone

from infra.database import get_db, write_transaction
from llm.catalog_cloud import discover_cloud_models
from llm.catalog_network_models import discover_network_models


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row_to_dict(row) -> dict | None:
    return dict(row) if row else None


def _is_routable_model(row: dict | None) -> bool:
    """Reject catalog rows for oMLX processes CORSIA does not own.

    Test fixtures deliberately have no network-server record. Production oMLX
    rows are discoverable only from CORSIA's state-root managed process.
    """
    if row is None or row.get("provider") != "omlx" or row.get("source") == "test":
        return row is not None
    litellm_model = row.get("litellm_model")
    if not isinstance(litellm_model, str):
        return False
    identity, separator, _model = litellm_model.partition("/")
    if not separator or not identity.startswith("omlx@"):
        return False
    from llm.local_setup_managed import is_corsia_managed_server
    from llm.network_discovery import get_server

    server = get_server(identity.removeprefix("omlx@")) or {}
    # Explicit External providers are user-configured and observe-only; only
    # historical managed/local rows need the private-process ownership proof.
    return server.get("kind") == "external" or is_corsia_managed_server(server)


def get_model(model_id: str) -> dict | None:
    """Look up a model by its short ID (e.g. 'claude-haiku')."""
    db = get_db()
    row = db.execute("SELECT * FROM models WHERE id = ?", (model_id,)).fetchone()
    db.close()
    result = _row_to_dict(row)
    return result if _is_routable_model(result) else None


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

    result = _row_to_dict(row)
    if not _is_routable_model(result):
        result = None
    if result is None and "@" in litellm_model:
        provider, _, rest = litellm_model.partition("@")
        if provider == "omlx":
            _, _, provider_model_id = rest.partition("/")
            if provider_model_id:
                candidates = db.execute(
                    "SELECT * FROM models WHERE provider = ? AND provider_model_id = ? AND available = 1",
                    (provider, provider_model_id),
                ).fetchall()
                result = next(
                    (candidate for candidate in map(_row_to_dict, candidates) if _is_routable_model(candidate)),
                    None,
                )

    db.close()
    return result


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
    if not include_unavailable:
        clauses.append("available = 1")
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
    db.close()
    return [row for raw in rows if _is_routable_model(row := dict(raw))]


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
          CASE WHEN harness != 'native'
            THEN COALESCE(json_extract(metadata_json, '$.discovery_order'), 2147483647)
          END,
          display_name,
          id
        """
    ).fetchall()
    db.close()
    return [row for raw in rows if _is_routable_model(row := dict(raw))]


def is_valid_model_choice(litellm_model: str) -> bool:
    """Check if a litellm model string is a valid selection (enabled + available)."""
    db = get_db()
    row = db.execute(
        "SELECT * FROM models WHERE litellm_model = ? AND enabled = 1 AND available = 1",
        (litellm_model,),
    ).fetchone()
    db.close()
    result = _row_to_dict(row)
    return _is_routable_model(result)


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
