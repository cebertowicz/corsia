"""Explicit ASGI target for mock-backed UI E2E tests."""

from __future__ import annotations

from contextlib import asynccontextmanager, nullcontext
import os

from fastapi import FastAPI

import e2e_reset

# A process restarted by POST /api/test/reset puts the captured seed back before anything opens the database.
e2e_reset.restore_if_requested()

from e2e_live_faults import FaultConfiguration, FaultConfigurationError, FaultController, build_router  # noqa: E402
from e2e_mock import mock_engine, router as mock_router, seed_mock_catalog  # noqa: E402
from infra.config import DB_PATH, STATE_ROOT  # noqa: E402
from main import app as production_app  # noqa: E402


_live_fault_opt_in = os.environ.get("CORSIA_E2E_LIVE_FAULTS") == "1"
try:
    _live_fault_controller = FaultController(FaultConfiguration.from_environ()) if _live_fault_opt_in else None
except FaultConfigurationError:
    # This remains an E2E-only app, but bad live-fault setup must be visible to
    # the caller instead of silently exposing a crash endpoint.
    _live_fault_controller = None


@asynccontextmanager
async def _lifespan(_: FastAPI):
    checkpoint_hook = (_live_fault_controller.install_checkpoint_hook()
                       if _live_fault_controller is not None else nullcontext())
    with mock_engine(), checkpoint_hook:
        async with production_app.router.lifespan_context(production_app):
            from loguru import logger
            logger.info("E2E test roots: state_dir={} db_path={}", STATE_ROOT, DB_PATH)
            seed_mock_catalog()
            yield


app = FastAPI(
    title="CORSIA UI E2E",
    lifespan=_lifespan,
    openapi_url=None,
    docs_url=None,
    redoc_url=None,
)
app.include_router(mock_router)
app.include_router(e2e_reset.router, prefix="/api/test", tags=["test"])
_live_fault_router = build_router(_live_fault_controller, opt_in_requested=_live_fault_opt_in)
if _live_fault_router is not None:
    app.include_router(_live_fault_router)
app.mount("/", production_app)
