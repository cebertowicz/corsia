"""Reasoning levels for External models, read from the public models.dev catalog.

An OpenAI-compatible model list carries no capability data, and some plan
endpoints list no models at all, so an External endpoint cannot say which
reasoning levels its models accept. models.dev (github.com/anomalyco/models.dev,
MIT) is a contributor-maintained catalog that records, per provider, the base
URL it serves and each model's ``reasoning_options``. Matching the endpoint's
base URL to a provider's ``api`` and then the model id gives the levels that
provider documents, with no table in CORSIA's source: a row added or fixed
upstream takes effect at the next discovery, without a CORSIA release.

The catalog is fetched at discovery only, revalidated by ETag, and the last
good copy is kept on disk. When no copy can be read, levels stay unknown and
the failure is reported; nothing is guessed from a model's name.

``CORSIA_MODEL_CATALOG_URL`` points at a mirror of the same JSON; set it empty
to turn the catalog off (levels then stay unknown).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

CATALOG_URL = "https://models.dev/api.json"
_URL_ENV = "CORSIA_MODEL_CATALOG_URL"
SOURCE = "models.dev"
_TIMEOUT_S = 20.0


class CatalogUnavailable(RuntimeError):
    """No copy of the catalog could be fetched or read."""


class CatalogTurnedOff(CatalogUnavailable):
    """The catalog URL is set empty: levels stay unknown, and that is not a failure."""


def _cache_path() -> Path:
    from infra.config import RUNTIME_DIR

    return RUNTIME_DIR / "models-dev-api.cache"


def _read_cached() -> tuple[dict | None, str | None]:
    """The cached catalog and the ETag it was served with, always as one pair."""
    try:
        etag_line, _, body = _cache_path().read_text().partition("\n")
        catalog = json.loads(body)
    except (OSError, ValueError):
        return None, None
    if not isinstance(catalog, dict):
        return None, None
    return catalog, etag_line.strip() or None


def _write_cached(body: str, etag: str | None) -> None:
    """Write the ETag line and the body as one file, replaced atomically.

    A per-writer temp file keeps concurrent discoveries from interleaving, and
    one file means a later 304 can never pair an ETag with another response's body.
    """
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(f"{(etag or '').strip()}\n{body}")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


async def load_catalog() -> tuple[dict, str | None]:
    """Return ``(catalog, warning)``: the current catalog, or the last good copy.

    ``warning`` names a failed refresh that fell back to the cached copy.
    Raises :class:`CatalogUnavailable` when neither can be read. The file is
    several megabytes, so reading and parsing it run off the event loop.
    """
    url = os.environ.get(_URL_ENV, CATALOG_URL).strip()
    if not url:
        raise CatalogTurnedOff(f"the model catalog is turned off ({_URL_ENV} is empty)")
    cached, etag = await asyncio.to_thread(_read_cached)
    headers = {"If-None-Match": etag} if cached is not None and etag else {}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code == 304 and cached is not None:
            return cached, None
        resp.raise_for_status()
        catalog = await asyncio.to_thread(json.loads, resp.content)
        if not isinstance(catalog, dict):
            raise ValueError("the catalog is not a JSON object")
    except (httpx.HTTPError, ValueError) as exc:
        reason = f"{SOURCE} could not be read: {exc}"
        if cached is None:
            raise CatalogUnavailable(reason) from exc
        log.warning("%s; using the last good copy", reason)
        return cached, f"{reason}; using the last good copy"
    try:
        await asyncio.to_thread(_write_cached, resp.text, resp.headers.get("etag"))
    except OSError as exc:
        log.warning("could not cache the %s catalog: %s", SOURCE, exc)
    return catalog, None


async def reasoning_shapes(
    api_base: str, model_ids: list[str],
) -> tuple[dict[str, dict | None] | None, str | None]:
    """``(shapes, warning)`` for one endpoint's models at discovery.

    ``shapes`` maps every id to its levels or None (not listed). It is None
    when the catalog could not be read at all, so a caller keeps what each row
    already had instead of erasing levels over a network failure. A catalog
    turned off on purpose gives no warning.
    """
    try:
        catalog, warning = await load_catalog()
    except CatalogTurnedOff:
        return None, None
    except CatalogUnavailable as exc:
        log.warning("%s", exc)
        return None, str(exc)
    return {mid: reasoning_shape(catalog, api_base, mid) for mid in model_ids}, warning


def _normalize_base(url: str) -> str:
    return (url or "").strip().rstrip("/").lower()


def providers_for_base(catalog: dict, api_base: str) -> list[str]:
    """Catalog provider ids whose published base URL is ``api_base``."""
    want = _normalize_base(api_base)
    if not want:
        return []
    return [
        pid for pid, provider in catalog.items()
        if isinstance(provider, dict) and _normalize_base(provider.get("api") or "") == want
    ]


def reasoning_shape(catalog: dict, api_base: str, model_id: str) -> dict[str, Any] | None:
    """The effort levels the catalog lists for ``model_id`` behind ``api_base``.

    None when no provider publishes this base URL, the model is not listed, or
    the listing has no effort levels. Only an exact base-URL and model-id match
    counts: the same model served elsewhere may accept a different set.

    The shape offers "Auto" (send nothing, so the provider's own default runs)
    and no "Off": a thinking toggle is a separate, provider-specific parameter
    CORSIA does not send.
    """
    for pid in providers_for_base(catalog, api_base):
        model = (catalog[pid].get("models") or {}).get(model_id)
        if not isinstance(model, dict):
            continue
        for option in model.get("reasoning_options") or []:
            if not isinstance(option, dict) or option.get("type") != "effort":
                continue
            values = [str(v) for v in option.get("values") or [] if isinstance(v, str) and v]
            if values:
                return {
                    "type": "effort_levels",
                    "values": values,
                    "default": None,
                    "supports_auto": True,
                    "supports_off": "none" in values,
                    "source": {"catalog": SOURCE, "provider": pid},
                }
    return None
