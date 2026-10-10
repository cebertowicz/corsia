"""Model ids a user declares for an External endpoint that does not list them.

Some providers serve models they never list: Alibaba's Token Plan answers
``/models`` with an empty list, and its Coding Plan answers 404 ("this model
list cannot be queried through an API"). Every documented client for those
plans hardcodes its model ids. An endpoint therefore carries an optional
``declared_models`` list in its server metadata. Discovery and the health sweep
treat each declared id exactly like a listed one, and a list call the provider
does not support (404/405) is not a failure once ids are declared: the host
answered, it just keeps no catalog. Auth failures still mean offline.

A mistyped base path answers the list call with 404 as well, so saving or
testing such an endpoint asks one declared model for a reply before calling
it reachable (:func:`declared_model_refusal`).
"""

from __future__ import annotations

import re

import httpx

META_KEY = "declared_models"
MAX_DECLARED = 100
# The list call is simply absent on these providers, not broken.
LIST_UNSUPPORTED = frozenset({404, 405})
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$")
_PROOF_TIMEOUT_S = 30.0


def clean_model_ids(raw: list[str] | None) -> list[str] | None:
    """The declared ids, trimmed and de-duplicated in order; None when not given.

    Raises ValueError naming the first id that is not a plausible model id, so
    a pasted sentence or a key is refused instead of becoming a catalog row.
    """
    if raw is None:
        return None
    ids: list[str] = []
    for item in raw:
        mid = (item or "").strip()
        if not mid or mid in ids:
            continue
        if not _ID.match(mid):
            raise ValueError(f"Not a model id: {mid[:40]!r}")
        ids.append(mid)
    if len(ids) > MAX_DECLARED:
        raise ValueError(f"At most {MAX_DECLARED} model ids can be declared")
    return ids


def declared_model_ids(meta: dict | None) -> list[str]:
    """The ids stored on an endpoint's metadata; unreadable values count as none."""
    value = (meta or {}).get(META_KEY)
    if not isinstance(value, list):
        return []
    return [m for m in value if isinstance(m, str) and m]


def merge_model_ids(listed: list[str], declared: list[str]) -> list[str]:
    """Listed ids first, then declared ids the endpoint did not list."""
    seen = set(listed)
    return list(listed) + [m for m in declared if m not in seen]


async def discoverable_model_ids(
    api_base: str, dialect: str, api_key: str | None, declared: list[str],
) -> list[str]:
    """Everything discovery should catalog: the listed ids plus the declared ones."""
    from llm import external_provider as ep

    try:
        listed = await ep.fetch_external_models(api_base, dialect, api_key)
    except httpx.HTTPStatusError as exc:
        if not declared or exc.response.status_code not in LIST_UNSUPPORTED:
            raise
        listed = []
    return merge_model_ids(listed, declared)


async def declared_model_refusal(
    base: str, dialect: str, headers: dict, model_id: str,
) -> str | None:
    """Why ``base`` refused a request to a declared model, or None once it serves one.

    Only the status is read: 401/403 is a refused key and 404 a wrong base path
    or model id. Any other answer, even a rate limit, came from the path turns
    use. The request is streamed and dropped at its status line, and carries
    just the model and one message: no sampling parameters, except the output
    cap the Anthropic Messages API refuses to run without.
    """
    from llm.network_endpoints import chat_request_url

    url = chat_request_url(base, dialect=dialect)
    body: dict = {"model": model_id, "stream": True,
                  "messages": [{"role": "user", "content": "Reply with OK."}]}
    if dialect == "anthropic":
        body["max_tokens"] = 1
    try:
        async with httpx.AsyncClient(timeout=_PROOF_TIMEOUT_S) as client:
            async with client.stream("POST", url, headers=headers, json=body) as resp:
                status = resp.status_code
    except httpx.HTTPError:
        return None  # The list call reached this host; a slow model is not a wrong URL.
    if status in (401, 403):
        return f"{url} refused the API key for {model_id} (HTTP {status}). Check the key."
    if status == 404:
        return (f"{url} returned HTTP 404 for {model_id}. Check the base URL, including its "
                f"version path, and the model ID.")
    return None
