"""The composed router lifespan retains lanes until canonical turn joins finish."""
from fastapi import FastAPI
import pytest

from tests.test_app_lifecycle_shutdown import _isolate_lifespan


@pytest.mark.asyncio
async def test_composed_lifespan_closes_documents_after_turn_joins(monkeypatch, isolated_db):
    import app_lifecycle
    from api.files import router
    from doceditor import live_runtime
    from infra import config
    _isolate_lifespan(monkeypatch)
    monkeypatch.setattr(live_runtime, "_manager", None)
    events = []
    close = live_runtime.LiveDocuments.close

    async def capture_close(self):
        events.append("documents")
        await close(self)

    async def join_cc():
        assert live_runtime.get_documents() is not None
        events.append("cc")

    async def join_codex():
        assert live_runtime.get_documents() is not None
        events.append("codex")

    monkeypatch.setattr(live_runtime.LiveDocuments, "close", capture_close)
    monkeypatch.setattr("subagents.cc_runtime.shutdown_all", join_cc)
    monkeypatch.setattr("subagents.codex_runtime.shutdown_all", join_codex)
    app = FastAPI(lifespan=app_lifecycle.lifespan)
    app.include_router(router)
    async with app.router.lifespan_context(app):
        assert live_runtime.get_documents() is not None
        assert live_runtime.get_documents().workspace == config.WORKSPACE
    assert events == ["cc", "codex", "documents"]
    assert live_runtime.get_documents() is None


@pytest.mark.asyncio
async def test_lifespan_starts_document_recovery_after_runtime_readiness(monkeypatch, isolated_db):
    import app_lifecycle
    from doceditor import live_runtime
    _isolate_lifespan(monkeypatch)
    events = []

    async def ready():
        events.append("runtime-ready")

    async def start_documents():
        events.append("documents")

    monkeypatch.setattr(app_lifecycle, "_start_runtime_services", ready)
    monkeypatch.setattr(live_runtime, "start_documents", start_documents)
    async with app_lifecycle.lifespan(FastAPI()):
        assert events == ["runtime-ready", "documents"]
