"""Keep PENDING sites out of browser copies and kept sign-ins, filtered at write time.

Every write here runs off the event loop. ``_write_filtered`` reads the PENDING sites and a version, writes,
and writes again if an ``end`` changed the version meanwhile. An end that lands after its last check
schedules a clear that waits for this write and then removes the site: from the kept file under
``_kept_lock``, and from a copy under that copy session's command lock, which its seeding holds.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Callable

from host_access import browser_grant_records as records, browser_site_clear
from host_access.browser_domain import url_domain

log = logging.getLogger(__name__)
_kept_lock = threading.Lock()  # one read-modify-write of a kept file at a time; never on the event loop


def read_kept(path: Path) -> list[dict]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    rows = value.get("cookies") if isinstance(value, dict) else None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("invalid kept browser sign-ins")
    return rows


def _cookies(rows: list[dict], domains: set[str]) -> list[dict]:
    return [row for row in rows if not any(browser_site_clear.cookie_matches(row, domain) for domain in domains)]


def _state(value: dict[str, Any], domains: set[str]) -> dict[str, Any]:
    return {**value, "cookies": _cookies(value.get("cookies") or [], domains),
            "origins": [row for row in value.get("origins") or ()
                        if isinstance(row, dict) and url_domain(row.get("origin")) not in domains]}


def _write_filtered(profile: str, write: Callable[[set[str]], None]) -> None:
    while True:
        domains, version = records.snapshot(profile)
        write(domains)
        if records.snapshot(profile)[1] == version:
            return


def write_state(profile: str, path: Any, value: dict[str, Any]) -> None:
    """Write a copy's seed state without any PENDING site."""
    from host_access.browser_session_launch import _write_private_json
    _write_filtered(profile, lambda domains: _write_private_json(path, _state(value, domains)))


def _write_kept(profile: str, rows: list[dict]) -> None:
    from host_access import browser_session_launch as launch
    path = launch.kept_signins_path(profile)
    unique = list({launch._cookie_key(row): row for row in rows}.values())
    _write_filtered(profile, lambda domains: launch._write_cookies(path, _cookies(unique, domains)))


def merge(profile: str, gained: list[dict]) -> None:
    """Add a closing copy's new cookies; an unreadable kept file raises and is left as it is."""
    from host_access import browser_session_launch as launch
    with _kept_lock:
        _write_kept(profile, [*read_kept(launch.kept_signins_path(profile)), *gained])


def kept(profile: str) -> list[dict]:
    """Kept cookies to apply, without PENDING sites; an unreadable file applies none and stays."""
    from host_access import browser_session_launch as launch
    path = launch.kept_signins_path(profile)
    with _kept_lock:
        try:
            original = read_kept(path)
        except (OSError, ValueError) as exc:
            log.warning("kept browser sign-ins unreadable; none applied (%s)", type(exc).__name__)
            return []
        while True:
            domains, version = records.snapshot(profile)
            rows = _cookies(original, domains)
            if len(rows) != len(original):
                launch._write_cookies(path, rows)
            if records.snapshot(profile)[1] == version:
                return rows


def drop(profile: str, domain: str) -> None:
    """Remove one site from the kept file; an unreadable file holds nothing that can be applied."""
    from host_access import browser_session_launch as launch
    path = launch.kept_signins_path(profile)
    with _kept_lock:
        try:
            rows = read_kept(path)
        except (OSError, ValueError) as exc:
            log.warning("kept browser sign-ins unreadable at site clear (%s)", type(exc).__name__)
            return
        launch._write_cookies(path, [row for row in rows if not browser_site_clear.cookie_matches(row, domain)])


def forget_applied(profile: str, applied: set[str]) -> None:
    """Remove cookies now in the browser from the kept file."""
    from host_access import browser_session_launch as launch
    path = launch.kept_signins_path(profile)
    with _kept_lock:
        rows = read_kept(path)
        launch._write_cookies(path, [row for row in rows if json.dumps(row, sort_keys=True) not in applied])
