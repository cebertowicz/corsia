"""CORSIA Backend — FastAPI server with WebSocket agent interface."""

from __future__ import annotations

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse as _ErrorJSONResponse

from app_lifecycle import lifespan

from infra import config  # noqa: F401
from infra.access_log import install_access_log_redaction
from infra.config import FRONTEND_DIR
from infra.product_events import emit_product_event
from infra.request_timing import RequestTimingMiddleware
from ws import websocket_endpoint
from ws.browser_stream import browser_stream_endpoint
from api.usage_rollup import refresh_on_request as refresh_usage_rollup
from api import (
    auth_router,
    setup_router,
    threads_router,
    thread_mutations_router,
    thread_messages_router,
    thread_runtime_router,
    models_router,
    agents_router,
    system_router,
    uploads_router,
    turns_router,
    drain_router,
    costs_router,
    measurement_router,
    measurement_where_to_look_router,
    mcp_router,
    projects_router,
    project_repos_router,
    fs_browse_router,
    provider_sources_router,
    provider_cli_link_router,
    providers_router,
    provider_policy_router,
    vendor_accounts_router,
    discovered_models_router,
    network_servers_router,
    version_router,
    webhooks_router,
    files_router,
    files_revision_router,
    doc_comments_router,
    doc_comment_thread_views_router,
    cc_attach_router,
    conductor_router,
    corsia_agent_router,
    context_bootloader_router,
    delegation_router,
    delegation_docs_router,
    delegation_model_router,
    skills_router,
    host_access_router,
    audit_reads_router,
    browser_router,
    plans_router,
    plans_folded_router,
)
from api.attention import router as attention_router
from api.system_perf import router as system_perf_router
from api.registry import router as registry_router
from api.registry_pane import router as registry_pane_router
from api.registry_ownership import router as registry_ownership_router
from api.doc_reviews import router as doc_reviews_router
from api.cli_install import router as cli_install_router
from api.keys import router as keys_router


install_access_log_redaction()
app = FastAPI(title="CORSIA UI", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RequestTimingMiddleware)


@app.exception_handler(Exception)
async def _emit_error_api(request: Request, exc: Exception):
    """Emit uncaught HTTP route failures as error.api and return generic 500."""
    from infra.internal_principal import current_internal_user

    internal = current_internal_user()
    emit_product_event(
        "error", "api",
        properties={
            "error_type": type(exc).__name__,
            "error_message": "In-process conductor request failed" if internal else str(exc)[:200],
            "path": str(request.url.path),
            "method": request.method,
        },
    )
    return _ErrorJSONResponse(
        status_code=500,
        content={"detail": "Internal Server Error"},
    )


app.include_router(system_router)
app.include_router(system_perf_router)
app.include_router(keys_router)
app.include_router(uploads_router)
app.include_router(turns_router)
app.include_router(drain_router)
app.include_router(threads_router)
app.include_router(thread_mutations_router)
app.include_router(thread_messages_router)
app.include_router(thread_runtime_router)
app.include_router(models_router)
app.include_router(agents_router)
app.include_router(costs_router)
app.include_router(measurement_router, dependencies=[Depends(refresh_usage_rollup)])
app.include_router(measurement_where_to_look_router, dependencies=[Depends(refresh_usage_rollup)])
app.include_router(mcp_router)
app.include_router(projects_router)
app.include_router(project_repos_router)
app.include_router(fs_browse_router)
app.include_router(files_router)
app.include_router(files_revision_router)
app.include_router(doc_comments_router)
app.include_router(doc_comment_thread_views_router)
app.include_router(doc_reviews_router)
app.include_router(providers_router)
app.include_router(vendor_accounts_router)
app.include_router(provider_policy_router)
app.include_router(provider_sources_router)
app.include_router(provider_cli_link_router)
app.include_router(cli_install_router)
app.include_router(discovered_models_router)
app.include_router(network_servers_router)
app.include_router(version_router)
app.include_router(registry_router)
app.include_router(registry_pane_router)
app.include_router(registry_ownership_router)
app.include_router(attention_router)
app.include_router(cc_attach_router)
app.include_router(conductor_router)
app.include_router(corsia_agent_router)
app.include_router(context_bootloader_router)
app.include_router(delegation_router)
app.include_router(delegation_docs_router)
app.include_router(delegation_model_router)
app.include_router(skills_router)
app.include_router(host_access_router)
app.include_router(audit_reads_router)
app.include_router(browser_router)
app.include_router(plans_router)
app.include_router(plans_folded_router)
app.include_router(auth_router)
app.include_router(setup_router)

app.include_router(webhooks_router)

from eval.endpoint import router as eval_router  # noqa: E402
app.include_router(eval_router)

app.add_api_websocket_route("/ws", websocket_endpoint)
app.add_api_websocket_route("/api/browser/{thread_id}/stream", browser_stream_endpoint)


# Build-presence is checked per-request so a build finishing mid-run works
# without a restart; if dist/ is absent we fall back to a JSON health blurb.
from starlette.responses import HTMLResponse, JSONResponse, Response


@app.get("/{path:path}")
async def serve_frontend(path: str):
    """Serve built frontend assets, falling back to index.html for routes."""
    if path.startswith("api/") or path == "api":
        return JSONResponse({"error": "Not found"}, status_code=404)
    if not FRONTEND_DIR.exists():
        return JSONResponse(
            {"status": "CORSIA UI backend running",
             "frontend": "not built yet — export CORSIA_BACKEND_URL, then use vite dev"},
        )
    file_path = FRONTEND_DIR / path
    if file_path.is_file():
        import mimetypes
        content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        content = file_path.read_bytes()
        headers = {}
        if "/_app/immutable/" in f"/{path}":
            headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif path in ("sw.js", "manifest.json"):
            headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            if path == "sw.js":
                # Clear only HTTP/Cache-API entries; cookies/storage must survive SW updates.
                headers["Clear-Site-Data"] = '"cache"'
        return Response(content=content, media_type=content_type, headers=headers)
    index = FRONTEND_DIR / "index.html"
    if index.exists():
        return HTMLResponse(
            content=index.read_text(),
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )
    return HTMLResponse("Not found", status_code=404)


if __name__ == "__main__":
    import uvicorn

    from infra.bind_host import BindHostError, resolve_bind_host
    from infra.config import BOOTSTRAP_CONFIG_PATH
    from infra.default_port import DEFAULT_BACKEND_PORT

    try:
        host = resolve_bind_host(BOOTSTRAP_CONFIG_PATH)
    except BindHostError as error:
        raise SystemExit(f"CORSIA will not start: {error}") from error
    uvicorn.run(app, host=host, port=DEFAULT_BACKEND_PORT)
