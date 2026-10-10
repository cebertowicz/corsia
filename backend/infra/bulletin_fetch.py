"""Bounded parsing, verification, and retrieval for signed live bulletins."""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx
from loguru import logger

from infra import bulletins


@dataclass(frozen=True)
class FetchFailure:
    reason: str
    item: str | None = None


@dataclass(frozen=True)
class FetchedBulletins:
    bulletins: list[dict[str, Any]]
    complete: bool
    failures: list[FetchFailure]


_BULLETIN_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_INDEX_NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\.md")
_FETCH_DEADLINE_SECONDS = 15.0
_MAX_BULLETIN_LIFETIME = timedelta(days=90)
_MAX_FUTURE_PUBLICATION_SKEW = timedelta(days=1)


def _date(value: str | date | datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def validate_bulletin(content: bytes) -> tuple[dict[str, Any] | None, str | None]:
    """Parse and validate signed-bulletin fields without checking a signature."""
    if len(content) > bulletins.MAX_BULLETIN_BYTES:
        return None, "oversized"
    try:
        import frontmatter

        document = frontmatter.loads(content.decode("utf-8"))
        metadata = dict(document.metadata)
        published_value = metadata["published"]
        expires_value = metadata["expires"]
    except Exception:  # A signed malformed document must not kill the fetch loop.
        return None, "unparseable"
    if not isinstance(published_value, (str, date, datetime)) or not isinstance(
        expires_value, (str, date, datetime),
    ):
        return None, "unparseable"
    try:
        published_date = _date(published_value)
        expires_date = _date(expires_value)
    except ValueError:
        return None, "unparseable"
    if (
        published_date > datetime.now(timezone.utc).date() + _MAX_FUTURE_PUBLICATION_SKEW
        or expires_date - published_date > _MAX_BULLETIN_LIFETIME
    ):
        return None, "unparseable"
    required = ("id", "title", "system_version")
    if not all(metadata.get(name) for name in required):
        return None, "unparseable"
    if not all(isinstance(metadata[name], str) for name in required):
        return None, "unparseable"
    bulletin_id = str(metadata["id"])
    if not _BULLETIN_ID_RE.fullmatch(bulletin_id):
        return None, "unparseable"
    providers = metadata.get("providers")
    if providers is not None and (not isinstance(providers, list) or not all(isinstance(item, str) for item in providers)):
        return None, "unparseable"
    withdrawn = metadata.get("withdrawn")
    if withdrawn is not None and not isinstance(withdrawn, bool):
        return None, "unparseable"
    superseded_by = metadata.get("superseded_by")
    if superseded_by is not None and not isinstance(superseded_by, str):
        return None, "unparseable"
    body = document.content
    if not isinstance(body, str):
        return None, "unparseable"
    if len(body.encode("utf-8")) > bulletins.MAX_BULLETIN_BODY_BYTES:
        return None, "oversized"
    fields: dict[str, Any] = {
        "id": bulletin_id, "title": metadata["title"], "published": published_date.isoformat(),
        "expires": expires_date.isoformat(), "system_version": metadata["system_version"], "body": body,
    }
    for name in ("providers", "withdrawn", "superseded_by"):
        if name in metadata:
            fields[name] = metadata[name]
    return fields, None


def _verify(content: bytes, signature: bytes) -> tuple[dict[str, Any] | None, str | None]:
    if not signature:
        return None, "unsigned"
    if not bulletins._signature_valid(content, signature):
        return None, "bad_signature"
    return validate_bulletin(content)


async def _bounded_get(
    client: httpx.AsyncClient, url: str, *, headers: dict[str, str] | None = None, limit: int,
) -> tuple[httpx.Response | None, bytes | None, str | None]:
    """Fetch bounded raw bytes so compressed responses cannot expand in memory."""
    request_headers = {"Accept-Encoding": "identity", **(headers or {})}
    async with client.stream("GET", url, headers=request_headers) as response:
        if response.headers.get("content-encoding"):
            return response, None, "unparseable"
        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > limit:
                    return response, None, "oversized"
            except ValueError:
                return response, None, "unparseable"
        if response.status_code != 200:
            return response, None, "network"
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_raw():
            size += len(chunk)
            if size > limit:
                return response, None, "oversized"
            chunks.append(chunk)
        return response, b"".join(chunks), None


async def fetch_live_bulletins(
    *, base_url: str = bulletins.DEFAULT_FEED_URL,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FetchedBulletins:
    """Fetch bounded, signed bulletin candidates without mutating workspace state."""
    if not bulletins.PUBLIC_KEYS:
        return FetchedBulletins([], True, [])
    try:
        async with asyncio.timeout(_FETCH_DEADLINE_SECONDS):
            async with httpx.AsyncClient(timeout=5.0, transport=transport) as client:
                index, index_bytes, index_error = await _bounded_get(
                    client, base_url.rstrip("/") + "/index.json", limit=bulletins.MAX_INDEX_BYTES,
                )
                if index is None:
                    return FetchedBulletins([], False, [FetchFailure("network")])
                if index_error or index_bytes is None:
                    return FetchedBulletins([], False, [FetchFailure(index_error or "network", "index.json")])
                return await _fetch_index(client, base_url, index_bytes)
    except (asyncio.TimeoutError, httpx.HTTPError) as exc:
        logger.warning("live bulletin fetch failed: {}", exc)
        return FetchedBulletins([], False, [FetchFailure("network")])


async def _fetch_index(
    client: httpx.AsyncClient, base_url: str, index_bytes: bytes,
) -> FetchedBulletins:
    try:
        names = json.loads(index_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return FetchedBulletins([], False, [FetchFailure("unparseable", "index.json")])
    if (
        not isinstance(names, list)
        or len(names) > bulletins.MAX_INDEX_ENTRIES
        or not all(isinstance(name, str) and _INDEX_NAME_RE.fullmatch(name) for name in names)
    ):
        return FetchedBulletins([], False, [FetchFailure("unparseable", "index.json")])
    candidates: list[dict[str, Any]] = []
    failures: list[FetchFailure] = []
    complete = True
    for name in names:
        fields, failure, item_complete = await _fetch_one(client, base_url, name)
        if fields is not None:
            candidates.append(fields)
        if failure is not None:
            failures.append(failure)
        complete = complete and item_complete
    return FetchedBulletins(candidates, complete, failures)


async def _fetch_one(
    client: httpx.AsyncClient, base_url: str, name: str,
) -> tuple[dict[str, Any] | None, FetchFailure | None, bool]:
    try:
        _source, content, source_error = await _bounded_get(
            client, base_url.rstrip("/") + "/" + name, limit=bulletins.MAX_BULLETIN_BYTES,
        )
        _signature, signature, signature_error = await _bounded_get(
            client, base_url.rstrip("/") + "/" + name + ".sig", limit=4096,
        )
    except httpx.InvalidURL:
        return None, FetchFailure("unparseable", name), False
    if source_error or signature_error or content is None or signature is None:
        failure_reason = source_error or signature_error or "network"
        return None, FetchFailure(failure_reason, name), False
    fields, verification_failure = _verify(content, signature)
    return fields, FetchFailure(verification_failure, name) if verification_failure else None, verification_failure is None
