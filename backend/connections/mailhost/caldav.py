"""CalDAV over ``httpx``: find a host's calendars, then list their events.

TLS is the verifying context from ``connections.mailhost.tls``; the password goes only to the
host the settings name and to hosts of the same site its calendar server points at (iCloud
answers from a numbered sibling host). Redirects to another origin drop the credential.
A failure here never refuses the password (some hosts keep a separate calendar password):
it means the host offers no calendar to CORSIA, and the row says mail only.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

import httpx

from connections.mailhost import tls
from connections.mailhost.endpoints import Endpoints

TIMEOUT_SECONDS = 20.0
MAX_RESPONSE_BYTES = 4_000_000
MAX_CALENDARS = 5
MAX_EVENTS = 250
D, C = "{DAV:}", "{urn:ietf:params:xml:ns:caldav}"
_PROPFIND = '<?xml version="1.0"?><d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop>{props}</d:prop></d:propfind>'
_REPORT = (
    '<?xml version="1.0"?><c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
    '<d:prop><c:calendar-data><c:expand start="{start}" end="{end}"/></c:calendar-data></d:prop>'
    '<c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">'
    '<c:time-range start="{start}" end="{end}"/></c:comp-filter></c:comp-filter></c:filter></c:calendar-query>'
)


class CalendarUnavailable(Exception):
    """The host offers no usable calendar; carries no host text."""


def _same_site(base_host: str, host: str) -> bool:
    """``host`` is the base host or a sibling or child of it under its parent domain."""
    labels = base_host.split(".")
    parent = ".".join(labels[1:]) if len(labels) > 2 else base_host
    return host == base_host or host == parent or host.endswith("." + parent)


def _client(username: str, password: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        auth=(username, password), verify=tls.context(), timeout=TIMEOUT_SECONDS, follow_redirects=True,
        headers={"User-Agent": "CORSIA"},
    )


async def _request(client: httpx.AsyncClient, method: str, url: str, body: str, depth: str) -> bytes | None:
    """The response body of a successful multistatus request; None for 401/403 (credential refused)."""
    try:
        response = await client.request(
            method, url, content=body.encode(), headers={"Depth": depth, "Content-Type": "application/xml; charset=utf-8"},
        )
    except httpx.HTTPError:
        raise CalendarUnavailable from None
    if response.status_code in (401, 403):
        return None
    if response.status_code != 207 or len(response.content) > MAX_RESPONSE_BYTES:
        raise CalendarUnavailable
    return response.content


def _responses(content: bytes) -> list[tuple[str, dict[str, ET.Element]]]:
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        raise CalendarUnavailable from None
    found = []
    for response in root.iter(D + "response"):
        props: dict[str, ET.Element] = {}
        for stat in response.iter(D + "propstat"):
            if " 200 " in f" {stat.findtext(D + 'status') or ''} ":
                for prop in stat.iter(D + "prop"):
                    props.update({child.tag: child for child in prop})
        found.append(((response.findtext(D + "href") or "").strip(), props))
    return found


def _href_in(props: dict[str, ET.Element], tag: str, base: str) -> str | None:
    node = props.get(tag)
    href = node.findtext(D + "href") if node is not None else None
    return urljoin(base, href.strip()) if href else None


def _checked(url: str | None, base_host: str) -> str:
    parts = urlsplit(url or "")
    if parts.scheme != "https" or not parts.hostname or not _same_site(base_host, parts.hostname):
        raise CalendarUnavailable
    return url or ""


async def _discover_with(client: httpx.AsyncClient, base: str) -> tuple[str, ...] | None:
    """The VEVENT calendars at ``base``; None when the credential was refused."""
    host = urlsplit(base).hostname or ""
    content = await _request(client, "PROPFIND", base, _PROPFIND.format(props="<d:current-user-principal/>"), "0")
    if content is None:
        return None
    principal = next((_href_in(props, D + "current-user-principal", base) for _, props in _responses(content)), None)
    principal = _checked(principal or base, host)
    content = await _request(client, "PROPFIND", principal, _PROPFIND.format(props="<c:calendar-home-set/>"), "0")
    if content is None:
        return None
    home = next((_href_in(props, C + "calendar-home-set", principal) for _, props in _responses(content)), None)
    home = _checked(home, host)
    props_xml = "<d:resourcetype/><c:supported-calendar-component-set/>"
    content = await _request(client, "PROPFIND", home, _PROPFIND.format(props=props_xml), "1")
    if content is None:
        return None
    calendars = []
    for href, props in _responses(content):
        kind, components = props.get(D + "resourcetype"), props.get(C + "supported-calendar-component-set")
        is_calendar = kind is not None and kind.find(C + "calendar") is not None
        names = {comp.get("name") for comp in components} if components is not None else {"VEVENT"}
        if is_calendar and "VEVENT" in names:
            calendars.append(_checked(urljoin(home, href), host))
    if not calendars:
        raise CalendarUnavailable
    return tuple(calendars[:MAX_CALENDARS])


async def discover(ep: Endpoints, password: str) -> tuple[str, tuple[str, ...]]:
    """``(username, calendar urls)`` for the first username form the host accepts; else CalendarUnavailable."""
    if not ep.caldav:
        raise CalendarUnavailable
    for username in ep.usernames(ep.caldav_username):
        async with _client(username, password) as client:
            calendars = await _discover_with(client, ep.caldav)
        if calendars is not None:
            return username, calendars
    raise CalendarUnavailable


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


async def fetch_calendar_data(ep: Endpoints, password: str, start: datetime, end: datetime) -> list[str]:
    """The iCalendar text of each event the discovered calendars hold in the window, instances expanded."""
    if not ep.calendars or not ep.caldav_username:
        raise CalendarUnavailable
    body = _REPORT.format(start=_utc(start), end=_utc(end))
    texts: list[str] = []
    async with _client(ep.caldav_username, password) as client:
        for url in ep.calendars:
            content = await _request(client, "REPORT", url, body, "1")
            if content is None:
                raise CalendarUnavailable
            for _, props in _responses(content):
                data = props.get(C + "calendar-data")
                if data is not None and data.text:
                    texts.append(data.text)
    return texts


async def put_event(ep: Endpoints, secret: str, uid: str, ics: str) -> bool:
    """Store one event on the account's first calendar; False (and nothing else) when it cannot be stored."""
    if not ep.calendars or not ep.caldav_username:
        return False
    url = f"{ep.calendars[0].rstrip('/')}/{uid}.ics"
    try:
        async with _client(ep.caldav_username, secret) as client:
            response = await client.put(
                url, content=ics.encode("utf-8"),
                headers={"Content-Type": "text/calendar; charset=utf-8", "If-None-Match": "*"},
            )
    except httpx.HTTPError:
        return False
    return response.status_code in (200, 201, 204)
