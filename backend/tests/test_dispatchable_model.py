"""Dispatchability check: which model strings the dispatch layer can route.

Guards the catalog-format silent no-op (a delegation thread created with a
selection id like ``omlx/<id>`` instead of its dispatch form
``omlx@<server>/<model>``). The fix fails loud; this proves *every* valid form
still routes and only genuinely-unroutable forms are rejected.
"""

from __future__ import annotations

from catalog_omlx_fixtures import fresh_db  # noqa: F401


def _seed_model(litellm_model: str, *, model_id: str = "m-seed",
                provider: str = "customprov", harness: str = "native",
                selector: str = "special-x", backend: str = "api") -> None:
    """Insert one catalog row with the given short id + litellm string."""
    from infra.database import get_db

    db = get_db()
    db.execute(
        "INSERT INTO models (id, harness, provider, provider_model_id, litellm_model, "
        "display_name, backend, source, enabled, available, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,1,1,?,?)",
        (model_id, harness, provider, selector, litellm_model, "Special X",
         backend, "discovered", "t", "t"),
    )
    if harness != "native":
        db.execute(
            "INSERT OR REPLACE INTO connections "
            "(id,type,provider,status,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            (harness, "cli", harness, "available", "t", "t"),
        )
    db.commit()


class TestIsDispatchableModel:
    def test_harness_catalog_rows_are_dispatchable_without_prefix_shortcuts(self, fresh_db):
        from llm.catalog import is_dispatchable_model

        _seed_model(
            "claude-code/opus", model_id="claude-code/opus",
            provider="claude-code", harness="claude-code", selector="opus",
            backend="cc",
        )
        _seed_model(
            "codex/gpt-5-codex", model_id="codex/gpt-5-codex",
            provider="codex", harness="codex", selector="gpt-5-codex",
            backend="codex",
        )
        assert is_dispatchable_model("claude-code/opus") is True
        assert is_dispatchable_model("codex/gpt-5-codex") is True

    def test_cloud_provider_forms_require_catalog_identity(self, fresh_db):
        from llm.catalog import is_dispatchable_model

        assert is_dispatchable_model("anthropic/claude-sonnet-x") is False
        _seed_model(
            "anthropic/claude-sonnet-x", model_id="claude-sonnet-x",
            provider="anthropic", selector="claude-sonnet-x",
        )
        assert is_dispatchable_model("anthropic/claude-sonnet-x") is True

    def test_local_backend_forms_require_catalog_identity(self, fresh_db):
        from llm.catalog import is_dispatchable_model

        assert is_dispatchable_model("omlx@srv-1/qwen-7b") is False
        _seed_model(
            "omlx@srv-1/qwen-7b", model_id="qwen-7b", provider="omlx",
            selector="qwen-7b", backend="network",
        )
        assert is_dispatchable_model("omlx@srv-1/qwen-7b") is True

    def test_catalog_known_nonstructural_form_is_dispatchable(self, fresh_db):
        from llm.catalog import is_dispatchable_model

        # A form the structural router doesn't recognize is dispatchable once
        # the catalog knows its exact litellm string — the catalog is the
        # source of truth, not a hardcoded provider list.
        assert is_dispatchable_model("customprov/special-x") is False
        _seed_model("customprov/special-x")
        assert is_dispatchable_model("customprov/special-x") is True

    def test_omlx_selection_id_is_not_dispatchable(self, fresh_db):
        from llm.catalog import is_dispatchable_model

        # The catalog selection id (slash) is NOT a dispatch form; its dispatch
        # form is omlx@<server>/<model>. This is the bug being fixed.
        assert is_dispatchable_model("omlx/abc123") is False

    def test_bare_short_id_resolves_only_when_catalog_knows_it(self, fresh_db):
        from llm.catalog import is_dispatchable_model

        # A bare short id with no structural provider route is dispatchable only
        # when the catalog resolves it by id — the native turn routes via
        # get_model(id) before get_model_by_litellm.
        assert is_dispatchable_model("claude-sonnet-x") is False
        _seed_model("anthropic/claude-sonnet-x", model_id="claude-sonnet-x")
        assert is_dispatchable_model("claude-sonnet-x") is True

    def test_native_target_uses_catalog_context_without_profile_override_table(
        self, fresh_db
    ):
        from llm.resolution import resolve_execution_target

        _seed_model("anthropic/claude-sonnet-x", model_id="claude-sonnet-x")

        target = resolve_execution_target("claude-sonnet-x")

        assert target.context_window == 128000

    def test_empty_or_none_is_not_dispatchable(self, fresh_db):
        from llm.catalog import is_dispatchable_model

        assert is_dispatchable_model("") is False
        assert is_dispatchable_model("   ") is False
        assert is_dispatchable_model(None) is False


class TestDispatchFormError:
    def test_error_names_the_model_and_the_dispatch_form(self):
        from llm.catalog import dispatch_form_error

        msg = dispatch_form_error("omlx/abc123")
        assert "omlx/abc123" in msg
        assert "canonical execution target" in msg.lower()
        assert "catalog" in msg.lower()
