"""Keep Claude Code selectors the CLI stopped listing but still accepts.

A Claude Code update can rename what ``list_models`` returns (2.1.283 dropped
``opus[1m]`` and folded the 1M window into ``opus``) while ``set_model`` still
accepts the old selector for the same model. Threads, agents and the reviewer
pinned to the old selector must keep running; the picker must not show it as
a duplicate.

An unlisted selector stays dispatchable only when all three hold, so nothing
is ever substituted:

1. the CLI accepts it now (``set_model`` succeeds);
2. it still reports the model recorded for it before, ignoring a ``[...]``
   variant suffix (the listing and the runtime disagree on the suffix);
3. that model is one the CLI still lists.

The ``[1m]`` variant of each listed selector is probed the same way even when
no catalog row ever had it: a fresh install, or an account signed in after the
update, has no old row, yet a review of a PR into ``main`` and a workflow that
needs a larger context still ask for ``<model>[1m]``. Its row is an alias like
any other, so the picker does not show it.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

ALIAS_KEY = "corsiaUnlistedAliasOf"
_VARIANT = re.compile(r"\[[^\]]*\]$")


def base_identity(identity: str) -> str:
    """``claude-opus-5-5[1m]`` and ``claude-opus-5-5`` are the same model."""
    return _VARIANT.sub("", identity.strip())


async def add_accepted_unlisted(
    payload: dict[str, Any],
    prior: list[dict[str, str]],
    probe: Callable[[str], Awaitable[dict[str, Any]]],
) -> dict[str, str]:
    """Append accepted unlisted selectors to a ``list_models`` payload.

    ``prior`` holds ``selector``, ``resolved_identity`` and ``display_name``
    for each non-placeholder catalog row; the ``[1m]`` variant of each listed
    selector is a candidate too. An appended entry copies the listed model's
    capabilities and records which listed selector it aliases.
    Returns, and logs, why each probed selector was not kept: a rejection,
    a timeout and a probe fault each keep their own message.
    """
    models = payload.setdefault("models", [])
    listed: dict[str, dict[str, Any]] = {}
    for raw in models:
        selector = str(raw.get("value") or "").strip()
        resolved = str(raw.get("resolvedModel") or "").strip()
        if selector and selector != "default" and resolved:
            listed.setdefault(base_identity(resolved), raw)
    listed_selectors = {str(raw.get("value") or "").strip() for raw in models}
    candidates = [
        row for row in prior
        if row["selector"] not in listed_selectors
        and row["resolved_identity"]
        and base_identity(row["resolved_identity"]) in listed
    ]
    candidates += _extended_variants(models, listed_selectors | {row["selector"] for row in candidates})
    errors: dict[str, str] = {}
    if not candidates:
        return errors
    results = await asyncio.gather(
        *(probe(row["selector"]) for row in candidates), return_exceptions=True,
    )
    for row, usage in zip(candidates, results, strict=True):
        selector = row["selector"]
        if isinstance(usage, BaseException):
            errors[selector] = (
                f"Claude Code does not list {selector}, and probing it failed: "
                f"{type(usage).__name__}: {usage}"
            )
            log.warning("%s", errors[selector])
            continue
        reported = str(usage.get("model") or "").strip()
        if base_identity(reported) != base_identity(row["resolved_identity"]):
            errors[selector] = (
                f"Claude Code does not list {selector} and maps it to "
                f"{reported or 'no model'}, not {row['resolved_identity']}"
            )
            log.warning("%s", errors[selector])
            continue
        source = listed[base_identity(reported)]
        models.append({
            **source,
            "value": row["selector"],
            "displayName": row["display_name"] or row["selector"],
            "resolvedModel": row["resolved_identity"],
            "contextUsage": usage,
            ALIAS_KEY: str(source.get("value") or ""),
        })
    return errors


def _extended_variants(models: list[dict[str, Any]], known: set[str]) -> list[dict[str, str]]:
    """A ``[1m]`` candidate for each listed selector that names no variant and has none yet."""
    variants: list[dict[str, str]] = []
    for raw in models:
        selector = str(raw.get("value") or "").strip()
        resolved = str(raw.get("resolvedModel") or "").strip()
        if not selector or selector == "default" or not resolved or _VARIANT.search(selector):
            continue
        variant = f"{selector}[1m]"
        if variant in known:
            continue
        known.add(variant)
        name = str(raw.get("displayName") or selector)
        variants.append({"selector": variant, "resolved_identity": f"{base_identity(resolved)}[1m]",
                         "display_name": f"{name} (1M context)"})
    return variants
