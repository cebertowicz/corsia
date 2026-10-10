"""DuckDB-backed product analytics emit wrapper.

Analytics is non-load-bearing: emits never raise, and failures warn once per
kind before downgrading to debug. ``CORSIA_ANALYTICS_DISABLE=1`` disables it.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from infra import analytics_db

log = logging.getLogger(__name__)

_warn_once: dict[str, bool] = {
    "init": False,
    "capture": False,
    "identify": False,
    "flush": False,
}


_DEFAULT_PROJECT = "corsia"


def _log_failure(kind: str, message: str, exc: BaseException) -> None:
    """Log an analytics failure at WARNING first, DEBUG thereafter."""
    if not _warn_once.get(kind, False):
        log.warning("%s: %s (further occurrences logged at debug)", message, exc)
        _warn_once[kind] = True
    else:
        log.debug("%s: %s", message, exc)


def analytics_disabled() -> bool:
    """Return True if the analytics store is force-disabled via env."""
    return os.getenv("CORSIA_ANALYTICS_DISABLE", "0") == "1"


def emit_product_event(
    category: str,
    action: str,
    *,
    thread_id: str | None = None,
    user_id: str | None = None,
    properties: dict[str, Any] | None = None,
) -> bool:
    """Emit an event and report whether it was durably persisted."""
    if analytics_disabled():
        return False

    project = os.getenv("CORSIA_ANALYTICS_PROJECT", _DEFAULT_PROJECT)
    payload: dict[str, Any] = {
        "project": project,
        "thread_id": thread_id,
        **(properties or {}),
    }
    if user_id is not None:
        payload["user_id"] = user_id

    project_id = payload.get("project_id") or project

    try:
        conn = analytics_db.get_db()
    except ImportError as exc:
        _log_failure("init", "duckdb not installed; analytics disabled", exc)
        return False
    except OSError as exc:
        _log_failure("init", "analytics db open failed", exc)
        return False
    except Exception as exc:  # noqa: BLE001  # duckdb.IOException and any other open failure
        _log_failure("init", "analytics db open failed", exc)
        return False

    try:
        conn.execute(
            "INSERT INTO product_events VALUES (?, ?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc),
                category,
                action,
                json.dumps(payload),
                thread_id,
                project_id,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        _log_failure("capture", "analytics emit failed", exc)
        return False
    return True


def identify_user(
    user_id: str,
    traits: dict[str, Any] | None = None,
) -> None:
    """Associate a user_id with optional traits."""
    if analytics_disabled():
        return

    properties = {"user_id": user_id, **(traits or {})}
    emit_product_event("user", "identified", user_id=user_id, properties=properties)


def flush() -> None:
    """No-op in the DuckDB backend; kept for API compatibility."""
    return


def _reset_for_tests() -> None:
    """Reset module state for tests."""
    for k in _warn_once:
        _warn_once[k] = False
    analytics_db._reset_for_tests()
