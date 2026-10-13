"""The unavailable catalog row a harness shows before its first discovery.

Until a refresh stores real rows, each harness keeps one placeholder row so a
saved selection still resolves. Discovery removes it, or keeps it as a hidden
reference while a thread or reviewer setting still names it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from connections.harness_model_rows import CACHE_KEY

_LEGACY_FALLBACKS = {
    "claude-code": {
        "selector": "opus", "display_name": "Claude Code", "backend": "cc",
        "description": "Runs this chat as a live Claude Code session.",
    },
    "codex": {
        "selector": "gpt-5-codex", "display_name": "Codex · GPT-5 Codex",
        "backend": "codex",
        "description": "Runs this chat as a live Codex (subscription) session.",
    },
}


def sync_legacy_fallback(
    provider: str, *, status: str, config: dict[str, Any] | None, db
) -> None:
    """Synchronize the pre-discovery unavailable row inside caller transaction."""
    fallback = _LEGACY_FALLBACKS.get(provider)
    if fallback is None:
        return
    if not any(row[1] == "harness" for row in db.execute("PRAGMA table_info(models)")):
        return
    envelope_exists = isinstance(config, dict) and CACHE_KEY in config
    should_exist = status not in {"available", "disabled"} and not envelope_exists
    if not should_exist:
        remove_legacy_fallback(provider, db=db)
        return
    selector = fallback["selector"]
    model_id = f"{provider}/{selector}"
    existing = db.execute(
        "SELECT harness,provider,provider_model_id,source FROM models WHERE id = ?",
        (model_id,),
    ).fetchone()
    if existing and not (
        existing["harness"] == provider
        and existing["provider"] == provider
        and existing["provider_model_id"] == selector
        and existing["source"] in {"legacy_fallback", "reference"}
    ):
        raise ValueError(f"legacy fallback collision: {model_id}")
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        "INSERT INTO models (id,harness,provider,provider_model_id,litellm_model,"
        "display_name,description,backend,source,enabled,available,context_window,"
        "metadata_json,default_temperature,default_max_tokens,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,?,?) "
        "ON CONFLICT(id) DO UPDATE SET "
        "litellm_model=excluded.litellm_model,display_name=excluded.display_name,"
        "description=excluded.description,backend=excluded.backend,source=excluded.source,"
        "enabled=MAX(models.enabled,models.source='reference'),available=0,context_window=NULL,"
        "metadata_json=excluded.metadata_json,last_error=NULL,discovered_at=NULL,last_seen_at=NULL,"
        "default_temperature=NULL,default_max_tokens=NULL,updated_at=excluded.updated_at",
        (
            model_id, provider, provider, selector, model_id,
            fallback["display_name"], fallback["description"], fallback["backend"],
            "legacy_fallback", 1, 0, None,
            json.dumps({"discovery_order": 0}, sort_keys=True), now, now,
        ),
    )


def remove_legacy_fallback(provider: str, *, db) -> None:
    """Delete fallback projection or restore one still-referenced hidden row."""
    rows = db.execute(
        "SELECT id,provider_model_id FROM models "
        "WHERE harness = ? AND source = 'legacy_fallback'",
        (provider,),
    ).fetchall()
    for row in rows:
        model_id = str(row["id"])
        referenced = db.execute(
            "SELECT 1 FROM threads WHERE model = ? LIMIT 1", (model_id,),
        ).fetchone() is not None
        settings = db.execute(
            "SELECT value FROM settings WHERE key = 'agents'"
        ).fetchone()
        if settings:
            parsed = json.loads(settings["value"])
            if not isinstance(parsed, dict):
                raise ValueError("agents settings must be an object")
            defaults = parsed.get("defaults") or {}
            if not isinstance(defaults, dict):
                raise ValueError("agents.defaults must be an object")
            for key in ("reviewer_model", "reviewer_fallback_model",
                        "host_access_reviewer_model"):
                configured = defaults.get(key)
                if not isinstance(configured, str) or not configured.strip():
                    continue
                configured = configured.strip()
                referenced = referenced or configured == model_id
                if provider == "claude-code":
                    # Historical bare reviewer selectors are Claude Code
                    # selectors; preserve the same row the cutover created.
                    referenced = referenced or configured == str(
                        row["provider_model_id"]
                    )
        if not referenced:
            db.execute("DELETE FROM models WHERE id = ?", (model_id,))
            continue
        metadata: dict[str, Any] = {
            "resolved_identity": None,
            "discovered_capabilities": {},
            "is_default": False,
            "availability_reason": None,
            "harness_version": None,
            "discovery_order": 0,
            "reference_only": True,
        }
        db.execute(
            "UPDATE models SET display_name = ?, description = '', "
            "source = 'reference', enabled = 0, available = 0, "
            "context_window = NULL, supports_vision = 0, supports_reasoning = 0, "
            "metadata_json = ?, last_error = NULL, discovered_at = NULL, "
            "last_seen_at = NULL, updated_at = ? WHERE id = ?",
            (
                str(row["provider_model_id"]),
                json.dumps(metadata, sort_keys=True),
                datetime.now(timezone.utc).isoformat(),
                model_id,
            ),
        )
