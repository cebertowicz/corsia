"""Shared request and logging helpers for system restart routes."""

import os
from pathlib import Path

from fastapi import HTTPException, Request

from infra.default_port import DEFAULT_BACKEND_PORT


def own_backend_port(request: Request) -> int:
    server = None if request.scope.get("corsia_listener") == "devices" else request.scope.get("server")
    if server and isinstance(server[1], int):
        return server[1]
    env_port = os.environ.get("BACKEND_PORT")
    if not env_port and request.scope.get("corsia_listener") == "devices":
        return DEFAULT_BACKEND_PORT
    if env_port and env_port.isdigit():
        return int(env_port)
    raise HTTPException(500, "Cannot determine this backend's own port for restart")


def restart_log_path(name: str) -> Path:
    """Return a service-owned log path for detached restart output."""
    from infra.config import LOGS_DIR

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    return LOGS_DIR / name
