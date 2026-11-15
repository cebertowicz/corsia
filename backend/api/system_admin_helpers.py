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


def update_status_details(result: dict) -> dict:
    """Add the short, read-only Update observations in their original order."""
    from datetime import datetime, timezone

    from automation import self_restart
    from automation.update_outcome import staged_update_activated
    from core.turn_core import active_turn_count
    from infra import build_info
    from infra.settings import get_update_schedule
    from infra.update_operation import project_update_operation

    result["usual_seconds"] = self_restart.usual_update_seconds()
    result["agents_working"] = active_turn_count()
    result["server_time"] = datetime.now(timezone.utc).isoformat()
    # This is the running version; current_version can change on disk before restart.
    result["running_version"] = build_info.running_app_version()
    result["schedule"] = get_update_schedule()
    operation = self_restart.latest_update_operation()
    if operation is not None:
        activated = staged_update_activated(operation)
        result["operation"] = project_update_operation(operation, activated_by_restart=activated)
    return result
