"""Discovery may not invent per-model inference defaults.

A 503,808-token model carried ``default_max_tokens=8192`` because that number
was a literal in the discovery INSERT, identical on every row. The catalog tier
outranks the calling code, so a checkpoint that had computed a 32,768-token
budget was handed 8,192, spent it on reasoning, stopped at ``length`` mid-JSON,
and was accepted as degraded prose.
"""

from __future__ import annotations

from vendor_account_fixtures import seed_catalog


def _discovered_model(db, model_id: str, *, temperature, max_tokens) -> None:
    db.execute(
        """INSERT INTO models
           (id,harness,provider,provider_model_id,litellm_model,display_name,
            backend,source,context_window,default_temperature,default_max_tokens,
            created_at,updated_at)
           VALUES (?,'native','omlx',?,?,?,'network','discovered',503808,?,?,'t','t')""",
        (model_id, model_id, f"omlx@srv/{model_id}", model_id, temperature, max_tokens),
    )


def test_the_discovery_insert_stamps_no_sampling_or_ceiling_literal() -> None:
    """The INSERT itself carries no invented value.

    Asserted on the statement because that is where the constant lived: the row
    it writes is the only evidence a running system ever sees.
    """
    import re
    from pathlib import Path

    import llm.catalog_network_models as discovery

    source = Path(discovery.__file__).read_text()
    insert = re.search(r"INSERT INTO models.*?VALUES \(([^)]*)\)", source, re.S)
    assert insert is not None
    values = insert.group(1)
    assert "8192" not in values, (
        "discovery is stamping an output ceiling it never discovered; the "
        "catalog tier outranks the caller, so this value silently overrules "
        "every budget the calling code computes"
    )
    assert "0.3" not in values


def test_a_checkpoint_budget_is_not_capped_by_an_invented_ceiling(
    isolated_db,
) -> None:
    """The caller's own budget survives resolution without a catalog ceiling."""
    from infra.database import get_db
    from llm.capabilities import get_model_max_output_tokens

    db = get_db()
    try:
        _discovered_model(db, "vmlx-ceiling", temperature=None, max_tokens=None)
        db.commit()

        requested = 32_768
        resolved = get_model_max_output_tokens("vmlx-ceiling", fallback=requested)
        assert min(resolved, requested) == requested, (
            "the checkpoint's own budget is still being overruled by the catalog"
        )
    finally:
        db.close()


def _stale_harness_row(db, model_id: str, selector: str, source: str) -> None:
    """A row written before the fix: the schema DEFAULTs supplied 0.3/8192."""
    provider = model_id.split("/", 1)[0]
    db.execute("DELETE FROM models WHERE id = ? OR (provider = ? AND provider_model_id = ?)",
               (model_id, provider, selector))
    db.execute(
        """INSERT INTO models
           (id,harness,provider,provider_model_id,litellm_model,display_name,
            backend,source,created_at,updated_at)
           VALUES (?,?,?,?,?,?,'cc',?,'t','t')""",
        (model_id, provider, provider, selector, model_id, model_id, source),
    )
    stale = db.execute(
        "SELECT default_temperature, default_max_tokens FROM models WHERE id = ?",
        (model_id,),
    ).fetchone()
    assert tuple(stale) == (0.3, 8192), "fixture no longer reproduces a stale row"


def _defaults(db, model_id: str) -> tuple:
    row = db.execute(
        "SELECT default_temperature, default_max_tokens FROM models WHERE id = ?",
        (model_id,),
    ).fetchone()
    return tuple(row)


def test_harness_discovery_clears_defaults_stamped_on_existing_rows(isolated_db) -> None:
    from connections.harness_models import normalize_claude_models
    from infra.database import get_db

    db = get_db()
    try:
        _stale_harness_row(db, "claude-code/sonnet", "sonnet", "discovered")
        rows = normalize_claude_models(
            {"models": [{"value": "sonnet", "resolvedModel": "claude-sonnet-4-8"}]},
            harness_version="2.1.218",
            timestamp="2026-09-23T00:00:00+00:00",
        )
        seed_catalog(
            "claude-code", rows, harness_version="2.1.218",
            source="claude-stream-json:list_models",
            timestamp="2026-09-23T00:00:00+00:00", db=db,
        )
        assert _defaults(db, "claude-code/sonnet") == (None, None)
    finally:
        db.close()


def test_legacy_fallback_sync_clears_defaults_stamped_on_existing_rows(isolated_db) -> None:
    from connections.harness_legacy_fallback import sync_legacy_fallback
    from infra.database import get_db

    db = get_db()
    try:
        _stale_harness_row(db, "codex/gpt-5-codex", "gpt-5-codex", "legacy_fallback")
        sync_legacy_fallback("codex", status="not_installed", config={}, db=db)
        assert _defaults(db, "codex/gpt-5-codex") == (None, None)
    finally:
        db.close()
