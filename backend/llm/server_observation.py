"""The one writer of a network server's observed state, fed by every source.

A server's state is its status, its observed model set (``_LOADED_BY_SERVER``,
which a row needs before it reports ``ready``) and, through discovery's
reconcile, each model's ``available`` flag. :func:`record_reachable` and
:func:`record_unreachable` are the only transitions; the sources differ only in
what they observed:

- the health sweep probes managed oMLX and LAN External servers every 10s;
- discovery (add, edit, Test connection, startup) observes a cloud External
  server, which the sweep never probes;
- a real dispatch to a cloud External server observes it too.

Nothing re-probes a cloud server, so only a definitive observation moves it: a
connection loss or timeout on this machine is not evidence the provider is gone,
and an offline state it caused would never clear by itself.

Transition and failure matrix for a cloud External server (EAS35):

======================================  =========================================
Observation                             Effect
======================================  =========================================
discovery lists models                  online; observed set = discovered ids;
                                        reconcile sets ``available`` per listing
discovery or dispatch: 401/403          offline; observed set cleared; models
                                        unavailable until add, edit, Test
                                        connection or startup discovery lists
                                        models again
discovery or dispatch: connection or    no change; the caller still reports its
timeout failure, or a stream stall      own error (the turn, Test connection)
any other failure (404, 429, 5xx, ...)  no change
dispatch succeeds                       no change (an offline cloud server has no
                                        live endpoint, so it is never dispatched)
server disabled or removed              no source revives it
recording a dispatch outcome fails      logged and emitted as a product event;
                                        the turn's result is unchanged and the
                                        state stays as it was
======================================  =========================================
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Iterator
from urllib.parse import urlsplit

from infra.db_async import run_in_db

log = logging.getLogger(__name__)

# Strong references: the loop keeps only weak ones to a running task.
_PENDING: set[asyncio.Task] = set()

# oMLX servers whose models must be listed again: going offline marks every
# row unavailable, and only a discovery that lists the models clears the mark.
# Until then each reach rediscovers, so one failed attempt cannot leave a
# reachable server's models unavailable. The value is the last failure, logged
# once per distinct error rather than on every sweep. External servers keep
# their persisted discovery error and rediscover once per offline-to-online.
_UNLISTED: dict[str, str | None] = {}


_NO_TOKEN = object()


def loaded_token(server_id: str) -> object:
    """Capture the observed set before starting an asynchronous probe.

    Sweeps and loads update the set in place, so its identity changes only when an exit
    or going offline starts a new observation.
    """
    from llm import network_discovery as nd

    return nd._LOADED_BY_SERVER.get(server_id)


def forget_loaded_on_exit(server_id: str) -> set[str]:
    """Invalidate the observed set on process exit and return it for reload."""
    from llm import network_discovery as nd

    loaded = nd._LOADED_BY_SERVER.get(server_id, set())
    nd._LOADED_BY_SERVER[server_id] = set()
    nd._notify_models_changed_safe()
    return set(loaded)


async def record_reachable(
    server: dict, *, loaded: set[str] | None, rediscover: bool = True,
    token: object = _NO_TOKEN, **status: Any,
) -> None:
    """A source reached ``server``. ``loaded`` replaces its observed model set."""
    from llm import network_discovery as nd

    sid = server["id"]
    if token is not _NO_TOKEN and loaded_token(sid) is not token:
        return
    await run_in_db(nd.update_server_status, sid, status="online", **status)
    if token is not _NO_TOKEN and loaded_token(sid) is not token:
        return
    if loaded is not None:
        current = nd._LOADED_BY_SERVER.setdefault(sid, set())
        changed = current != loaded
        current.clear()
        current.update(loaded)
        if changed:
            nd._notify_models_changed_safe()
    if rediscover and (server.get("status") == "offline" or sid in _UNLISTED):
        _schedule(nd._discover_and_notify(sid))


async def record_unreachable(server: dict) -> None:
    """A source could not reach ``server``, or was refused by it."""
    from llm import network_discovery as nd

    provider = server.get("provider") or ("external" if server.get("kind") == "external" else "omlx")
    await run_in_db(nd._record_server_offline, server, provider)
    nd._LOADED_BY_SERVER.pop(server["id"], None)
    if server.get("kind") != "external":
        _UNLISTED.setdefault(server["id"], None)
    if server.get("status") != "offline":
        nd._notify_models_changed_safe()


def record_discovery(server_id: str, result: dict) -> bool:
    """Clear an oMLX server's relist mark when discovery listed it, else keep it.

    True when the outcome is new: it listed, or it failed with a different
    error. A retry that fails the same way changes nothing anyone sees.
    """
    if result.get("ok"):
        _UNLISTED.pop(server_id, None)
        return True
    error = result.get("error")
    changed = server_id not in _UNLISTED or _UNLISTED[server_id] != error
    if changed:
        log.warning("discovery of %s listed no models: %s", server_id, error)
    _UNLISTED[server_id] = error
    return changed


async def observe_discovery(
    server: dict, model_ids: list[str] | None, error: BaseException | None = None,
) -> None:
    """Feed one discovery outcome for a server the sweep does not observe."""
    from llm.network_endpoints import is_cloud_external

    if not await run_in_db(is_cloud_external, server):
        return
    if model_ids is not None:
        await record_reachable(server, loaded=set(model_ids), rediscover=False)
    elif error is not None and endpoint_failure_of(error):
        await record_unreachable(server)


def startup_discovery_targets() -> list[dict]:
    """Servers to discover at startup: every online one, and every offline cloud one.

    No sweep ever probes a cloud server, so startup discovery is its
    observation whatever the last one was: a key fixed outside CORSIA, or a row
    an earlier build took offline, recovers here.
    """
    from llm.network_discovery import get_servers
    from llm.network_endpoints import is_cloud_external

    return [
        server for server in get_servers()
        if server.get("status") == "online"
        or (server.get("status") == "offline" and is_cloud_external(server))
    ]


def endpoint_failure_of(error: BaseException) -> str | None:
    """``auth`` when ``error`` proves the endpoint refuses this key, else None.

    Read from the exception's HTTP status only, never its message (see
    :func:`http_status_of`). 401/403 is ``auth``; any other (404, 429, 5xx) is
    not an endpoint failure, nor is a connection loss, a timeout or a stream
    stall.
    """
    return "auth" if http_status_of(error) in (401, 403) else None


def http_status_of(error: BaseException) -> int | None:
    """The first HTTP status on ``error``'s :func:`exception_chain`, else None."""
    import anthropic
    import httpx
    import litellm
    import openai

    for current in exception_chain(error):
        status = _http_status(current, httpx, openai, anthropic, litellm)
        if status is not None:
            return status
    return None


def exception_chain(error: BaseException) -> Iterator[BaseException]:
    """``error`` and what it wraps, in traceback order.

    A provider SDK wraps the HTTP or transport error, so the chain is walked
    the way a traceback shows it: ``__cause__``, then ``__context__`` unless
    ``raise ... from None`` suppressed it.
    """
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )


def _http_status(error, httpx, openai, anthropic, litellm) -> int | None:
    # litellm's catch-all APIConnectionError carries a synthetic 500.
    if isinstance(error, litellm.APIConnectionError):
        return None
    if isinstance(error, (openai.APIStatusError, anthropic.APIStatusError)):
        return error.status_code
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code
    return None


def observe_dispatch(api_base: str | None, outcome: Any) -> None:
    """Schedule recording one dispatch's outcome; never touches the response path."""
    if api_base:
        _schedule(_record_dispatch(api_base, outcome))


def cloud_server_for_base(api_base: str) -> dict | None:
    """The enabled cloud External server a dispatch went to, read from its base URL.

    The dispatch path carries the provider-wire model (``openai/<id>``), not the
    catalog id. The base is the identity instead: an External server is keyed by
    its URL's host, port and path, so one base names one server.
    """
    from llm.network_discovery import get_servers
    from llm.network_endpoints import anthropic_root, configured_api_base, is_cloud_external

    target = api_base.rstrip("/")
    for server in get_servers():
        if server.get("kind") != "external" or server.get("status") == "disabled":
            continue
        base = configured_api_base(server["id"])
        if base and target in (base, anthropic_root(base)) and is_cloud_external(server):
            return server
    return None


def _schedule(coro) -> None:
    task = asyncio.create_task(coro)
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)


async def _record_dispatch(api_base: str, outcome: Any) -> None:
    try:
        await _apply_dispatch(api_base, outcome)
    except Exception as exc:
        # The turn already has its result; the server keeps its prior state,
        # the same outcome a missed sweep tick had.
        log.exception("recording a cloud dispatch outcome failed for %s", api_base)
        from infra.product_events import emit_product_event

        emit_product_event(
            "external_endpoint", "observation_failed",
            properties={"source": "dispatch", "error": type(exc).__name__},
        )


async def _apply_dispatch(api_base: str, outcome: Any) -> None:
    from llm.network_endpoints import classify_host

    host = urlsplit(api_base).hostname
    if not host or classify_host(host) != "public":
        return  # managed oMLX and LAN External: the sweep observes those
    server = await run_in_db(cloud_server_for_base, api_base)
    if server is None:
        return
    if isinstance(outcome, BaseException):
        failure = endpoint_failure_of(outcome)
    else:
        failure = getattr(outcome, "endpoint_failure", None)
    if failure:
        await record_unreachable(server)
