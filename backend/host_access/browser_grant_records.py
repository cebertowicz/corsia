"""Private site-only journal for browser fill grants and pending clears."""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from pathlib import Path
from urllib.parse import urlsplit

from host_access.browser_domain import url_domain

log = logging.getLogger(__name__)
_lock = threading.RLock()
# Active grants are deliberately process-only. On restart every journalled site is pending.
_armed: dict[tuple[str, str], int] = {}
_pending: set[tuple[str, str]] = set()
_generation: dict[tuple[str, str], int] = {}
_transitions: dict[tuple[str, str], int] = {}


def _site(site: str) -> str:
    parts = urlsplit(site)
    if parts.scheme not in ("http", "https") or not parts.hostname or not url_domain(site):
        raise ValueError("invalid site")
    host = parts.hostname
    if ":" in host:
        host = f"[{host}]"
    return f"{parts.scheme}://{host}" + (f":{parts.port}" if parts.port is not None else "")


def _path(profile: str) -> Path:
    return Path(f"{profile}.grant-sites.json")


def _data(profile: str) -> tuple[list[str], list[str]]:
    try:
        data = json.loads(_path(profile).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [], []
    if not isinstance(data, dict) or not set(data) <= {"sites", "origins"}:
        raise ValueError("invalid browser site journal")
    sites, origins = data.get("sites"), data.get("origins", [])
    if not isinstance(sites, list) or not isinstance(origins, list):
        raise ValueError("invalid browser site journal")
    if any(not isinstance(item, str) or _site(item) != item for item in [*sites, *origins]):
        raise ValueError("invalid browser site journal")
    if any(url_domain(origin) not in {url_domain(site) for site in sites} for origin in origins):
        raise ValueError("invalid browser site journal")
    return sites, origins


def _read(profile: str) -> list[str]:
    return _data(profile)[0]


def _write(profile: str, sites: list[str], origins: list[str] | None = None) -> None:
    path = _path(profile)
    if not sites:
        path.unlink(missing_ok=True)
        return
    from host_access.browser_identity import ensure_private_dir
    from host_access.browser_session_launch import _write_private_json

    ensure_private_dir(path.parent)
    if origins is None:
        _, origins = _data(profile)
    domains = {url_domain(site) for site in sites}
    kept = sorted({origin for origin in origins if url_domain(origin) in domains})
    data = {"sites": sorted(set(sites))}
    if kept:
        data["origins"] = kept
    _write_private_json(path, data)


def observe(profile: str, url: object) -> None:
    """Record a new matching origin without interrupting the navigation on failure."""
    from host_access.browser_site_clear import _origin
    if not isinstance(url, str):
        return
    domain = url_domain(url)
    if not domain or not (origin := _origin(url, domain)):
        return
    try:
        with _lock:
            sites, origins = _data(profile)
            if not any(url_domain(site) == domain for site in sites) or origin in origins:
                return
            _write(profile, sites, [*origins, origin])
            key = (profile, domain)
            if key in _pending or not _armed.get(key, 0):
                _generation[key] = _generation.get(key, 0) + 1
    except Exception as exc:
        log.warning("could not record origin for %s (%s)", domain, type(exc).__name__)


async def observe_thread(thread_id: str, url: object) -> None:
    """Resolve this session's saved profile and record an observed origin."""
    from host_access.browser_launch import profile_dir
    try:
        profile = await asyncio.to_thread(profile_dir, thread_id)
        if profile is not None:
            await asyncio.to_thread(observe, str(profile), url)
    except Exception as exc:
        domain = url_domain(url)
        if domain:
            log.warning("could not record origin for %s (%s)", domain, type(exc).__name__)


def origins(profile: str, domain: str) -> list[str]:
    with _lock:
        return [origin for origin in _data(profile)[1] if url_domain(origin) == domain]


def arm(profile: str, site: str) -> None:
    """Write the recovery record before a timed grant becomes active."""
    site = _site(site)
    domain = url_domain(site)
    assert domain is not None
    key = (profile, domain)
    with _lock:
        sites = _read(profile)
        if not _armed.get(key, 0) and any(url_domain(old) == domain for old in sites):
            _pending.add(key)
        _write(profile, [*sites, site])
        _armed[key] = _armed.get(key, 0) + 1


def end(profile: str, site: str) -> int:
    """Fence the domain and make an end durable, including for a standing grant."""
    site = _site(site)
    domain = url_domain(site)
    assert domain is not None
    key = (profile, domain)
    with _lock:
        try:
            sites = _read(profile)
            _write(profile, [*sites, site])
        except Exception:
            # A failed journal cannot admit more browser actions in this process.
            _transitions[key] = _transitions.get(key, 0) + 1
            raise
        if _armed.get(key, 0):
            _armed[key] -= 1
        _pending.add(key)
        _transitions[key] = _transitions.get(key, 0) + 1
        _generation[key] = _generation.get(key, 0) + 1
        return _generation[key]


def in_transition() -> bool:
    with _lock:
        return bool(_transitions)


def started(profile: str, domain: str) -> None:
    with _lock:
        key = (profile, domain)
        remaining = _transitions.get(key, 0) - 1
        if remaining > 0:
            _transitions[key] = remaining
        else:
            _transitions.pop(key, None)


def pending(profile: str) -> dict[str, tuple[list[str], int]]:
    """Return pending domains and their sites; after restart all journalled sites are pending."""
    with _lock:
        result: dict[str, tuple[list[str], int]] = {}
        for site in _read(profile):
            domain = url_domain(site)
            assert domain is not None
            key = (profile, domain)
            if key not in _pending and _armed.get(key, 0):
                continue
            sites, _ = result.setdefault(domain, ([], _generation.get(key, 0)))
            sites.append(site)
        return result


def complete(profile: str, domain: str, generation: int) -> bool:
    """Remove a cleared record only if no newer clear is pending."""
    key = (profile, domain)
    with _lock:
        if _generation.get(key, 0) != generation:
            return False
        if not _armed.get(key, 0):
            _write(profile, [site for site in _read(profile) if url_domain(site) != domain])
            _armed.pop(key, None)
        _pending.discard(key)
        return True
