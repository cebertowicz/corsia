"""Exclude pending-clear sites from browser copy and write-back state."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from host_access import browser_grant_records as records, browser_site_clear
from host_access.browser_domain import url_domain


def read_kept(path: Path) -> list[dict]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    rows = value.get("cookies") if isinstance(value, dict) else None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("invalid kept browser sign-ins")
    return rows


def cookies(profile: str, rows: list[dict]) -> list[dict]:
    domains = records.pending(profile)
    return [row for row in rows if not any(browser_site_clear.cookie_matches(row, domain) for domain in domains)]


def state(profile: str, value: dict[str, Any]) -> dict[str, Any]:
    with records._lock:
        domains = records.pending(profile)
        return {**value, "cookies": cookies(profile, value.get("cookies") or []),
                "origins": [row for row in value.get("origins") or ()
                            if isinstance(row, dict) and url_domain(row.get("origin")) not in domains]}


def write_state(profile: str, path: Any, value: dict[str, Any]) -> None:
    from host_access.browser_session_launch import _write_private_json
    with records._lock:
        _write_private_json(path, state(profile, value))


def merge(profile: str, gained: list[dict]) -> None:
    from host_access import browser_session_launch as launch
    with records._lock:
        path = launch.kept_signins_path(profile)
        rows = cookies(profile, [*read_kept(path), *gained])
        launch._write_cookies(path, list({launch._cookie_key(row): row for row in rows}.values()))


def kept(profile: str) -> list[dict]:
    from host_access import browser_session_launch as launch
    with records._lock:
        path = launch.kept_signins_path(profile)
        original = read_kept(path)
        rows = cookies(profile, original)
        if len(rows) != len(original):
            launch._write_cookies(path, rows)
        return rows
