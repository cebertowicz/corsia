"""Durable site records for saved-login fill grants, and the agent fence while a site is cleared.

A record is one site in ``<profile>.grant-sites.json``, with the origins on its domain seen while it was
recorded; never a cookie, value or login. Each recorded (profile, domain) is in one ``SiteState``:

| State   | Entered by | Left by | On failure |
|---------|------------|---------|------------|
| ARMED   | ``arm``: an approval goes active | ``end`` makes it PENDING | the ``arm`` write raises, so the approval is not granted |
| PENDING | ``end``: an approval ends; or a record with no state in memory (a restart) | ``complete`` after a confirmed clear: the record is removed, or goes back to ARMED while another grant on the site is active | the record stays; the next launch of the profile clears it before serving |

Owners: this module owns the states, the record file and the fence. ``browser_grant_effects`` calls
``arm`` and ``end`` from the grant code. ``browser_grant_cleanup`` runs each clear and calls ``complete``
and ``release``. ``browser_grant_copy`` filters copies and kept sign-ins against PENDING.

The fence: every ``end`` raises it for (profile, domain), also when its record write fails. The clear that
``end`` scheduled lowers it with ``release`` once the site is cleared or its browsers are confirmed closed.
While it is up, agent commands on every browser holding the profile are refused. A clear whose browser
can be neither closed nor terminated never releases it, so the refusal lasts until restart.

Locks: ``_records_lock`` serializes the record file and the state dicts, is held across that small file's
I/O, and is never taken on the event loop except by ``arm`` and ``end`` where the grant code calls them.
``_fence_lock`` guards only the in-memory fence, so the event loop may take it.
"""
from __future__ import annotations

import asyncio
import enum
import json
import logging
import threading
from pathlib import Path
from urllib.parse import urlsplit

from host_access.browser_domain import url_domain

log = logging.getLogger(__name__)
Key = tuple[str, str]


class SiteState(enum.Enum):
    ARMED = "armed"
    PENDING = "pending"


_records_lock = threading.RLock()
_fence_lock = threading.Lock()
# Process memory only: after a restart every recorded site has no state, which reads as PENDING.
_states: dict[Key, SiteState] = {}
_grants: dict[Key, int] = {}  # active grants on the site in this process
_generation: dict[Key, int] = {}  # changes with each end, and with an origin recorded while PENDING
_fences: dict[Key, int] = {}  # ends whose clear has not released the fence


def _site(site: str) -> str:
    parts = urlsplit(site)
    if parts.scheme not in ("http", "https") or not parts.hostname or not url_domain(site):
        raise ValueError("invalid site")
    host = parts.hostname
    if ":" in host:
        host = f"[{host}]"
    return f"{parts.scheme}://{host}" + (f":{parts.port}" if parts.port is not None else "")


def _key(profile: str, site: str) -> tuple[str, Key]:
    site = _site(site)
    domain = url_domain(site)
    assert domain is not None
    return site, (profile, domain)


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


def _state(key: Key) -> SiteState:
    return _states.get(key, SiteState.PENDING)


def observe(profile: str, url: object) -> None:
    """Record a new matching origin without interrupting the navigation on failure."""
    from host_access.browser_site_clear import _origin
    if not isinstance(url, str):
        return
    domain = url_domain(url)
    if not domain or not (origin := _origin(url, domain)):
        return
    try:
        with _records_lock:
            sites, origins = _data(profile)
            if not any(url_domain(site) == domain for site in sites) or origin in origins:
                return
            _write(profile, sites, [*origins, origin])
            if _state((profile, domain)) is SiteState.PENDING:
                _generation[(profile, domain)] = _generation.get((profile, domain), 0) + 1
    except Exception as exc:
        log.warning("could not record origin for %s (%s)", domain, type(exc).__name__)


async def observe_session(profile: str | None, url: object) -> None:
    """Record an observed origin for the session's saved profile, held by the caller (no lookup per navigation)."""
    if profile is None:
        return
    try:
        await asyncio.to_thread(observe, profile, url)
    except Exception as exc:
        domain = url_domain(url)
        if domain:
            log.warning("could not record origin for %s (%s)", domain, type(exc).__name__)


def origins(profile: str, domain: str) -> list[str]:
    with _records_lock:
        return [origin for origin in _data(profile)[1] if url_domain(origin) == domain]


def arm(profile: str, site: str) -> None:
    """Write the record before a grant becomes active: ARMED, or still PENDING if a clear is owed."""
    site, key = _key(profile, site)
    with _records_lock:
        sites = _read(profile)
        _write(profile, [*sites, site])
        if not _grants.get(key, 0) and any(url_domain(old) == key[1] for old in sites):
            _states[key] = SiteState.PENDING  # left by an earlier process or an unfinished clear
        _states.setdefault(key, SiteState.ARMED)
        _grants[key] = _grants.get(key, 0) + 1


def end(profile: str, site: str) -> int:
    """Raise the fence and make the site PENDING before the grant end commits."""
    site, key = _key(profile, site)
    with _records_lock:
        try:
            _write(profile, [*_read(profile), site])
        except Exception:
            _raise_fence(key)  # Without a durable record this process admits no more agent commands there.
            raise
        if _grants.get(key, 0):
            _grants[key] -= 1
        _states[key] = SiteState.PENDING
        _raise_fence(key)
        _generation[key] = _generation.get(key, 0) + 1
        return _generation[key]


def _raise_fence(key: Key) -> None:
    with _fence_lock:
        _fences[key] = _fences.get(key, 0) + 1


def release(profile: str, domain: str) -> None:
    """Lower one end's fence once its clear has finished or its browsers are confirmed closed."""
    with _fence_lock:
        remaining = _fences.get((profile, domain), 0) - 1
        if remaining > 0:
            _fences[(profile, domain)] = remaining
        else:
            _fences.pop((profile, domain), None)


def fenced(profile: str | None) -> bool:
    """Whether agent commands on browsers holding this profile are refused; memory only."""
    if profile is None:
        return False
    with _fence_lock:
        return any(key[0] == profile for key in _fences)


def pending(profile: str) -> dict[str, tuple[list[str], int]]:
    """Return PENDING domains with their sites and generation."""
    with _records_lock:
        result: dict[str, tuple[list[str], int]] = {}
        for site in _read(profile):
            domain = url_domain(site)
            assert domain is not None
            if _state((profile, domain)) is SiteState.ARMED:
                continue
            sites, _ = result.setdefault(domain, ([], _generation.get((profile, domain), 0)))
            sites.append(site)
        return result


def snapshot(profile: str) -> tuple[set[str], int]:
    """PENDING domains, and a version that changes with every end or origin recorded on the profile."""
    with _records_lock:
        version = sum(value for key, value in _generation.items() if key[0] == profile)
        return set(pending(profile)), version


def complete(profile: str, domain: str, generation: int) -> bool:
    """After a confirmed clear, remove the record, or re-arm it while a grant is active; False if superseded."""
    key = (profile, domain)
    with _records_lock:
        if _generation.get(key, 0) != generation:
            return False
        if _grants.get(key, 0):
            _states[key] = SiteState.ARMED
            return True
        _write(profile, [site for site in _read(profile) if url_domain(site) != domain])
        _states.pop(key, None)
        _grants.pop(key, None)
        return True
