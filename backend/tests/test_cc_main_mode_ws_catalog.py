from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


class TestCCAttachments:
    def test_block_builds_paths_skips_missing(self, tmp_path, monkeypatch):
        import infra.config as cfg
        import ws as ws_mod
        monkeypatch.setattr(cfg, "UPLOADS_DIR", tmp_path)
        (tmp_path / "steve" / "fid1").mkdir(parents=True)
        (tmp_path / "steve" / "fid1" / "x.png").write_bytes(b"img")
        block = ws_mod._cc_attachment_block([
            {"file_id": "fid1", "filename": "x.png"},
            {"file_id": "missing", "filename": "y.png"},
        ], "steve")
        assert str(tmp_path / "steve" / "fid1" / "x.png") in block
        assert "y.png" not in block

    def test_block_empty_when_none(self):
        import ws as ws_mod
        assert ws_mod._cc_attachment_block([], "steve") == ""

    def test_block_never_projects_typed_image_paths(self, tmp_path, monkeypatch):
        import infra.config as cfg
        import ws as ws_mod

        monkeypatch.setattr(cfg, "UPLOADS_DIR", tmp_path)
        path = tmp_path / "steve" / "fid1" / "x.png"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"image")

        block = ws_mod._cc_attachment_block([{
            "file_id": "fid1",
            "filename": "x.png",
            "mime_type": "image/png",
        }], "steve")

        assert block == ""
        assert str(path) not in block

    @pytest.mark.asyncio
    async def test_main_handler_feeds_paths_keeps_row_clean(
        self, main_db, monkeypatch, tmp_path
    ):
        import infra.config as cfg
        import infra.send_service as send_service
        from registry import models
        import subagents.cc_runtime as rt
        import ws as ws_mod

        monkeypatch.setattr(cfg, "WORKSPACE", tmp_path)
        path = tmp_path / ".threads" / "main-1" / "uploads" / "f" / "a.png"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"i")
        models.register_artifact(
            integration="upload",
            type="upload",
            external_id="f",
            parent_thread_id="main-1",
            path=str(path),
        )

        async def _inline(fn, *args, **kwargs):
            return fn(*args, **kwargs)
        monkeypatch.setattr(ws_mod, "run_in_db", _inline)
        monkeypatch.setattr(ws_mod, "get_db", lambda: main_db)
        monkeypatch.setattr("infra.db_async.run_in_db", _inline)
        from core.turn_core import TurnIdentity

        async def _accept(*_args, **kwargs):
            await kwargs["persist_input_once"]()
            return TurnIdentity(kwargs["turn_id"], 1)

        feed = AsyncMock(side_effect=_accept)
        monkeypatch.setattr(rt, "feed_main", feed)

        class _WS:
            async def send_json(self, m):
                pass

        session = main_db.execute("SELECT * FROM threads WHERE id='main-1'").fetchone()
        await ws_mod._handle_cc_main_message(
            _WS(), session, "main-1", "look at this",
            [{"file_id": "f", "filename": "a.png"}],
            msg_id="m9", now="t", is_retry=False, is_edited=False)
        sent = feed.call_args.args[1]
        assert "look at this" in sent
        assert sent.startswith(
            "CORSIA is delivering 2 queued inbound inputs together."
        )
        assert "[1/2]\n[PASSIVE CONTEXT DIGEST]" in sent
        assert sent.endswith(
            "[2/2]\nlook at this\n"
            '[To reply, call post_update(text="<your reply>")]\n'
            "If no reply is needed, do not send one."
        )
        row = main_db.execute(
            "SELECT content FROM messages WHERE thread_id='main-1' "
            "AND role='user' ORDER BY created_at DESC").fetchone()
        assert row["content"] == "look at this"


class TestModelsCatalog:
    @pytest.fixture(autouse=True)
    def _discovered_harness_catalog(self, monkeypatch):
        import connections.harness_refresh as harness_refresh
        from llm import catalog as model_catalog
        import llm.model_status as model_status

        monkeypatch.setattr(
            harness_refresh, "schedule_stale_refresh", lambda: None
        )
        def _row(provider, selector, display, backend, order):
            return {
                "id": f"{provider}/{selector}", "harness": provider,
                "provider": provider, "provider_model_id": selector,
                "litellm_model": f"{provider}/{selector}",
                "display_name": display, "description": "", "backend": backend,
                "source": "test-discovery", "enabled": 1, "available": 1,
                "context_window": 200000, "supports_vision": 0,
                "supports_tools": 1, "supports_reasoning": 1,
                "default_temperature": 0.3, "default_max_tokens": 8192,
                "last_error": None,
                "metadata_json": (
                    '{"resolved_identity":"%s",'
                    '"discovered_capabilities":{},"discovery_order":%d}'
                    % (selector, order)
                ),
            }
        rows = [
            _row("claude-code", "opus", "Opus", "cc", 0),
            _row("claude-code", "sonnet", "Sonnet", "cc", 1),
            _row("codex", "gpt-5-codex", "GPT-5 Codex", "codex", 0),
        ]
        monkeypatch.setattr(
            model_catalog, "get_model_status_catalog_rows", lambda: rows,
        )
        monkeypatch.setattr(
            model_status, "_harness_discovery_envelope", lambda _provider: {},
        )

    @pytest.mark.asyncio
    async def test_cc_models_listed_when_connection_available(self, monkeypatch):
        import api.models as am
        import connections.models as cm
        from llm import catalog as model_catalog

        monkeypatch.setattr(cm, "provider_may_run",
                            lambda provider, db=None: provider == "claude-code")
        out = await am.list_models()
        cc = [m for m in out["models"] if m["provider"] == "claude-code"]
        assert [m["id"] for m in cc] == [
            "claude-code/opus", "claude-code/sonnet"]
        assert all(m["status"] == "ready" and m["backend"] == "cc" for m in cc)
        assert all("Claude Code" in m["name"] for m in cc)

    @pytest.mark.asyncio
    async def test_cc_row_unavailable_not_dropped_without_connection(self, monkeypatch):
        # AC-P1a-2: a configured-but-unavailable harness renders as a present,
        # disabled row with an honest reason — never filtered out.
        import api.models as am
        import connections.models as cm
        from llm import catalog as model_catalog

        monkeypatch.setattr(cm, "provider_may_run",
                            lambda provider, db=None: False)
        out = await am.list_models()
        cc = [m for m in out["models"] if m["provider"] == "claude-code"]
        assert [row["id"] for row in cc] == [
            "claude-code/opus", "claude-code/sonnet",
        ]
        assert all(row["status"] == "unavailable" for row in cc)
        assert all(row["status_reason"] == "connection_unavailable" for row in cc)

    @pytest.mark.asyncio
    async def test_codex_models_listed_when_connection_available(self, monkeypatch):
        import api.models as am
        import connections.models as cm
        from llm import catalog as model_catalog

        monkeypatch.setattr(cm, "provider_may_run",
                            lambda provider, db=None: provider == "codex")
        out = await am.list_models()
        codex = [m for m in out["models"] if m["provider"] == "codex"]
        assert [m["id"] for m in codex] == ["codex/gpt-5-codex"]
        assert codex[0]["status"] == "ready" and codex[0]["backend"] == "codex"

    @pytest.mark.asyncio
    async def test_codex_row_unavailable_not_dropped_without_connection(self, monkeypatch):
        # AC-P1a-2: unavailable Codex harness renders present+disabled, not dropped.
        import api.models as am
        import connections.models as cm
        from llm import catalog as model_catalog

        monkeypatch.setattr(cm, "provider_may_run",
                            lambda provider, db=None: False)
        out = await am.list_models()
        codex = [m for m in out["models"] if m["provider"] == "codex"]
        assert len(codex) == 1
        assert codex[0]["status"] == "unavailable"
        assert codex[0]["status_reason"] == "connection_unavailable"
        assert codex[0]["backend"] == "codex"
        assert codex[0]["id"] == "codex/gpt-5-codex"
        assert "Codex" in codex[0]["name"]

    @pytest.mark.asyncio
    async def test_models_missing_context_window_are_not_ready(self, monkeypatch):
        import api.models as am
        from llm import capabilities
        from llm import catalog as model_catalog
        from llm import network_discovery

        monkeypatch.setattr(capabilities, "get_active_local_threads", lambda _active: [])
        monkeypatch.setattr(network_discovery, "get_servers", lambda: [
            {"id": "srv-local", "status": "online"},
        ])
        monkeypatch.setattr(
            network_discovery,
            "get_loaded_model_ids",
            lambda _sid: {"Model-35B"},
        )
        monkeypatch.setattr(model_catalog, "get_model_status_catalog_rows", lambda: [{
            "id": "omlx-missing-window",
            "harness": "native",
            "provider": "omlx",
            "provider_model_id": "Model-35B",
            "litellm_model": "omlx@srv-local/Model-35B",
            "display_name": "Model 35B",
            "description": "MLX",
            "backend": "network",
            "available": 1,
            "context_window": None,
            "supports_vision": 0,
            "supports_tools": 1,
            "default_temperature": 0.3,
            "default_max_tokens": 8192,
            "metadata_json": "{}",
        }])

        out = await am.list_models()

        row = out["models"][0]
        assert row["status"] == "unavailable"
        assert row["status_reason"] == "missing_context_window"
        assert row["capabilities"]["context_window_max"] is None

    @pytest.mark.asyncio
    async def test_cc_models_expose_effort_levels_reasoning_config(self, monkeypatch):
        import api.models as am
        import connections.models as cm
        from llm import catalog as model_catalog

        monkeypatch.setattr(cm, "provider_may_run",
                            lambda provider, db=None: provider == "claude-code")
        out = await am.list_models()
        cc = [m for m in out["models"] if m["provider"] == "claude-code"]
        assert cc, "expected CC models"
        for m in cc:
            caps = m["capabilities"]
            assert caps["supports_reasoning"] is True
            rc = caps["reasoning_config"]
            assert rc is not None
            assert rc["type"] == "effort_levels"
            assert set(rc["values"]) == {"low", "medium", "high", "xhigh", "max"}
            assert rc["default"] == "high"

    @pytest.mark.asyncio
    async def test_codex_models_expose_effort_levels_reasoning_config(self, monkeypatch):
        import api.models as am
        import connections.models as cm
        from llm import catalog as model_catalog

        monkeypatch.setattr(cm, "provider_may_run",
                            lambda provider, db=None: provider == "codex")
        out = await am.list_models()
        codex = [m for m in out["models"] if m["provider"] == "codex"]
        assert codex, "expected Codex models"
        for m in codex:
            caps = m["capabilities"]
            assert caps["supports_reasoning"] is True
            rc = caps["reasoning_config"]
            assert rc is not None
            assert rc["type"] == "effort_levels"
            assert set(rc["values"]) == {"none", "minimal", "low", "medium", "high", "xhigh"}
            assert rc["default"] == "high"
