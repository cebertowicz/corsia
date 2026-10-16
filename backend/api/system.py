"""Health, settings, system status, restart, admin."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request

from api.setup import _trusted_request
from api.system_admin import router as system_admin_router
from api.system_reviewer import (
    host_access_reviewer_status as _host_access_reviewer_status,
    reviewer_save_error,
)
from infra import bulletins, config
from infra import state
from infra.auth import authenticate, require_human_user, require_user
from infra.cpu_sampler import apply_cpu_profiler, cpu_sampler_running
from infra.db_async import run_in_db
from infra.settings import (
    delete_agent_default,
    get_feature_flags,
    get_host_access_auto_approve,
    get_host_access_reviewer_fallback,
    save_agent_defaults,
    save_setting_merge,
)

router = APIRouter(prefix="/api", tags=["system"])
router.include_router(system_admin_router)

# Host-access review settings change what activates, so only a human session may
# save them. The reviewer model has its own conductor rule in update_settings.
_HUMAN_ONLY_HOST_ACCESS_SETTINGS = frozenset({
    "host_access_auto_approve",
    "host_access_reviewer_fallback",
})


@router.get("/health")
async def health():
    if state._thread_driver_failure is not None:
        raise HTTPException(503, f"thread driver failed: {state._thread_driver_failure}")
    return {"status": "ok"}


@router.get("/system/operation-active")
async def system_operation_active(request: Request):
    """Expose only whether the durable update/restart lock currently holds."""
    if not _trusted_request(request):
        raise HTTPException(403)
    from automation import self_restart

    return {"active": await run_in_db(self_restart.in_flight_request) is not None}


def _security_alert_path() -> Path:
    """Return the alert sentinel beneath the selected operational state root."""
    return config.STATE_ROOT / "security" / "ALERT"


@router.get("/security/alert")
async def security_alert(user: dict = Depends(require_user)):
    """Return security audit alert data if present."""
    alert_path = _security_alert_path()
    if alert_path.exists():
        try:
            import json as _json

            return _json.loads(alert_path.read_text())
        except Exception:
            return {
                "vulns": -1,
                "errors": -1,
                "report": str(alert_path),
                "date": "unknown",
            }
    return None


@router.delete("/security/alert")
async def dismiss_security_alert(token: str):
    """Dismiss the security alert banner."""
    user = authenticate(token)
    if not user:
        raise HTTPException(401)
    alert_path = _security_alert_path()
    if alert_path.exists():
        alert_path.unlink()
    return {"ok": True}


def _features_snapshot(flags: dict | None = None) -> dict:
    """The canonical enumerated feature truth both GET and PATCH return.

    Every feature is enumerated with its default, so a response that only
    carries one saved flag can never read as "Focus off": the client replaces
    its whole feature object from this snapshot.
    """
    resolved = flags if flags is not None else get_feature_flags()
    return {
        "debug_version_overlay": resolved.get("debug_version_overlay", True),
        "focus_mode": resolved.get("focus_mode", True),
        # The sampler's real state, not the saved flag: a start that failed reads off.
        "cpu_profiler": cpu_sampler_running(),
    }


async def _save_features(features: object) -> dict:
    """Save feature flags, apply the ones with runtime effect, return the snapshot.

    The settings PATCH's one features path, for the person and the conductor alike,
    so the running process never falls out of step with the saved flags.
    """
    if not isinstance(features, dict):
        raise HTTPException(400, "features must be an object")
    if not all(isinstance(value, bool) for value in features.values()):
        raise HTTPException(400, "feature values must be booleans")
    await run_in_db(save_setting_merge, "features", features)
    flags = await run_in_db(get_feature_flags)
    await asyncio.to_thread(apply_cpu_profiler, bool(flags.get("cpu_profiler", False)))
    snapshot = _features_snapshot(flags)
    # Return the same canonical enumeration GET returns, so a partial write
    # (e.g. a debug-only PATCH) can never drop an untouched feature's truth.
    return snapshot


def _settings_snapshot() -> dict:
    flags = get_feature_flags()
    from core.workspace_context_sources import workspace_context_files
    from infra.database import get_db
    from tools.browser import browser_available

    from jobs.corrections_realtime import state_snapshot as corrections_classifier_state

    db = get_db()
    try:
        context_rows = [
            row.to_dict() for row in workspace_context_files(config.WORKSPACE, db=db)
        ]
        live_bulletins = bulletins.live_preferences(db)
        corrections_classifier = corrections_classifier_state(db)
    finally:
        db.close()

    return {
        "workspace_context_files": context_rows,
        "host_access_auto_approve": get_host_access_auto_approve(),
        "corrections_classifier": corrections_classifier,
        "host_access_reviewer_fallback": get_host_access_reviewer_fallback(),
        "features": _features_snapshot(flags),
        "capabilities": {"browser_automation": browser_available()},
        "live_bulletins": live_bulletins,
        **_host_access_reviewer_status(),
    }


@router.get("/settings")
async def get_settings(user: dict = Depends(require_user)):
    """Return system settings."""
    return await run_in_db(_settings_snapshot)


def _save_exact_model_defaults(body: dict) -> dict:
    """Save the no-fallback model settings in ``body``: exact runnable IDs only."""
    updated: dict[str, str | None] = {}
    if "host_access_reviewer_model" in body:
        reviewer_model = body["host_access_reviewer_model"]
        if reviewer_model is None:
            delete_agent_default("host_access_reviewer_model")
            updated["host_access_reviewer_model"] = None
        else:
            error = reviewer_save_error(reviewer_model)
            if error is not None:
                raise HTTPException(400, error)
            save_agent_defaults({"host_access_reviewer_model": reviewer_model})
            updated["host_access_reviewer_model"] = reviewer_model
    return updated


def _valid_live_bulletin_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    return bool(parsed.hostname) and (
        parsed.scheme == "https" or (
            parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        )
    )


def _save_live_bulletin_settings(enabled: bool | None, base_url: str | None) -> dict[str, object]:
    """Persist live bulletin settings and return their stored normalized values."""
    from infra.database import get_db, write_transaction

    db = get_db()
    try:
        with write_transaction(db, scope="system_live_bulletins_settings"):
            return bulletins.save_live_preferences(
                db, enabled=enabled, base_url=base_url,
            )
    finally:
        db.close()


def _conductor_sets_reviewer(user: dict, body: dict) -> bool:
    """A conductor's corsia_api call may choose the reviewer model, never clear it.

    ``internal`` is the principal corsia_api binds only after its conductor-capability
    check; every other machine credential still needs the signed-in person.
    """
    return user.get("token_kind") == "internal" and isinstance(body["host_access_reviewer_model"], str)


@router.patch("/settings")
async def update_settings(body: dict, user: dict = Depends(require_user)):
    """Update system settings."""
    if "core_model" in body or "fallback_model" in body:
        raise HTTPException(
            400,
            "Core Agent model settings have been removed. Choose a model for each thread.",
        )
    if "devices" in body:
        raise HTTPException(400, "Devices must be changed through /api/devices")

    if _HUMAN_ONLY_HOST_ACCESS_SETTINGS.intersection(body) or "live_bulletins" in body:
        require_human_user(user)
    if "host_access_reviewer_model" in body and not _conductor_sets_reviewer(user, body):
        require_human_user(user)

    updated = {}
    if "host_access_reviewer_model" in body:
        updated.update(await run_in_db(_save_exact_model_defaults, body))

    if "host_access_auto_approve" in body:
        auto_approve = body["host_access_auto_approve"]
        if not isinstance(auto_approve, bool):
            raise HTTPException(400, "host_access_auto_approve must be a boolean")
        save_agent_defaults({"host_access_auto_approve": auto_approve})
        updated["host_access_auto_approve"] = auto_approve

    if "host_access_reviewer_fallback" in body:
        fallback = body["host_access_reviewer_fallback"]
        if not isinstance(fallback, bool):
            raise HTTPException(400, "host_access_reviewer_fallback must be a boolean")
        await run_in_db(save_agent_defaults, {"host_access_reviewer_fallback": fallback})
        updated["host_access_reviewer_fallback"] = fallback

    if "live_bulletins" in body:
        live_bulletins = body["live_bulletins"]
        if not isinstance(live_bulletins, dict):
            raise HTTPException(400, "live_bulletins must be an object")
        enabled = live_bulletins.get("enabled")
        base_url = live_bulletins.get("base_url")
        if enabled is not None and not isinstance(enabled, bool):
            raise HTTPException(400, "live_bulletins.enabled must be a boolean")
        if base_url is not None and not _valid_live_bulletin_url(base_url):
            raise HTTPException(400, "live_bulletins.base_url must be https, or http on loopback")
        updated["live_bulletins"] = await run_in_db(
            _save_live_bulletin_settings, enabled, base_url,
        )

    if "features" in body:
        updated["features"] = await _save_features(body["features"])

    return {"ok": True, **updated}


def _network_setup() -> str | None:
    """The install choice: "local" (this computer only), "server" (reachable
    from other devices), or None when none is recorded (all interfaces)."""
    from infra.bind_host import recorded_setup
    from infra.config import BOOTSTRAP_CONFIG_PATH

    try:
        return recorded_setup(BOOTSTRAP_CONFIG_PATH)
    except OSError:
        return None


_system_cache: dict = {"data": None, "ts": 0.0}
_SYSTEM_CACHE_TTL = 5  # seconds


def _live_platform_status() -> dict:
    """The non-cached poller state included in each system status response."""
    from events.connectors.github_notifications import poller_status
    from events.connectors.github_pr_quiet_watch import quiet_watch_status
    from events.connectors.gmail_poller import poller_status as gmail_poller_status

    return {
        "github_notifications": poller_status(),
        "gmail_poller": gmail_poller_status(),
        "pr_quiet_watch": quiet_watch_status(),
    }


@router.get("/system")
async def system_status(user: dict = Depends(require_user)):
    """Return cached system performance metrics and live platform poller state."""

    now = time.monotonic()
    if (
        _system_cache["data"] is not None
        and now - _system_cache["ts"] < _SYSTEM_CACHE_TTL
    ):
        return {
            **_system_cache["data"],
            **_live_platform_status(),
            "network_setup": _network_setup(),
        }

    import psutil

    GIB = 1024**3

    def _collect_system_stats():
        import subprocess

        result = {}
        try:
            vm = psutil.virtual_memory()
            free = vm.available
            total = vm.total
            result["memory"] = {
                "total_gb": round(total / GIB, 1),
                "free_mb": round(free / (1024**2)),
                "free_gb": round(free / GIB, 2),
                "active_gb": 0.0,
                "wired_gb": 0.0,
                "compressed_gb": 0.0,
                "pressure": "critical"
                if free < 512 * 1024 * 1024
                else "warning"
                if free < 2 * GIB
                else "ok",
            }
        except Exception as e:
            result["memory"] = {"error": str(e)}

        try:
            top = subprocess.run(
                ["top", "-l", "1", "-n", "0"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            for line in top.stdout.split("\n"):
                if "CPU usage" in line:
                    parts = line.split(",")
                    for part in parts:
                        if "idle" in part:
                            idle = float(part.strip().split("%")[0])
                            result["cpu"] = {
                                "usage_pct": round(100 - idle, 1),
                                "idle_pct": idle,
                            }
                            break
                    break
        except Exception as e:
            result["cpu"] = {"error": str(e)}

        return result

    result = await asyncio.to_thread(_collect_system_stats)
    _system_cache["data"] = result
    _system_cache["ts"] = time.monotonic()
    return {
        **result,
        **_live_platform_status(),
        "network_setup": _network_setup(),
    }
