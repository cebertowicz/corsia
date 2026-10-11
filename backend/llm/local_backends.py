"""Registry for locally-hosted LLM server backends.

Each backend owns litellm prefix matching, health checks, provider params, and
user-facing not-loaded messages. Cloud providers do not use this registry.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class LocalModelBackend(ABC):
    """Contract for a kind of locally-hosted LLM server."""

    # Unique key used for logging and handler registry lookup. Matches the
    # `provider` column in the models table.
    name: str

    @abstractmethod
    def matches(self, litellm_model: str) -> bool:
        """True if ``litellm_model`` is served by this backend."""

    @abstractmethod
    async def check_health(self, litellm_model: str) -> tuple[bool, str]:
        """Returns ``(healthy, reason)``. ``reason`` is only meaningful when
        ``healthy`` is False."""

    @abstractmethod
    def not_loaded_reason(self, litellm_model: str) -> str:
        """Human-readable reason when the catalog's ``available`` flag is 0.

        Called without making a network round-trip — the health sweep has
        already determined the model isn't loaded; this just phrases it."""

    @abstractmethod
    def make_provider_params(self, litellm_model: str) -> dict[str, Any]:
        """Return the dict passed to ``chat_completion(**params, ...)``.

        Typically includes ``model``, ``api_key``, ``api_base``,
        ``uses_streaming``. Specifics are backend-dependent."""

    @abstractmethod
    def transport_identity(
        self,
        litellm_model: str,
    ) -> tuple[str | None, str | None]:
        """Return wire provider/model identity without resolving an endpoint."""


class OmlxBackend(LocalModelBackend):
    """Direct-to-oMLX backend.

    Format ``omlx@<server_id>/<model_id>`` — the ``@`` guards ``server_id``
    from a normal provider/model split. Health and management ops route
    through ``llm.providers.omlx`` against jundot/oMLX's native HTTP
    surface. Inference flows through ``base_completion`` via ``openai/<model>``
    + the oMLX api_base; oMLX-specific request shaping (``thinking_budget``,
    ``chat_template_kwargs``, ``min_p`` / ``xtc_*`` / ``specprefill*``)
    rides along in ``extra_body``.

    Host:port comes from a ``network_servers`` row populated via manual
    add (``/api/settings/network-servers`` POST).
    """

    name = "omlx"
    _prefix = "omlx@"

    def matches(self, litellm_model: str) -> bool:
        return litellm_model.startswith(self._prefix)

    def _parse(self, litellm_model: str) -> tuple[str, str]:
        """Split ``omlx@<server_id>/<model_id>`` into the two parts."""
        rest = litellm_model[len(self._prefix):]
        server_id, _, provider_model_id = rest.partition("/")
        return server_id, provider_model_id

    def _cfg(self, server_id: str) -> dict[str, Any]:
        """Build the cfg dict ``llm.providers.omlx`` consumes from a
        ``network_servers`` row + active endpoint + stored metadata.
        Returns just ``{"server_id": ...}`` when the row is missing —
        the provider module's own ``host``/``port`` guards take it from there.

        ``api_key`` comes from the key store (set at manual-add time via
        ``set_server_key``) so admin endpoints — model load,
        settings, stats — authenticate without prompting per request."""
        from infra.key_store_servers import resolve_server
        from llm.network_discovery import get_server
        from llm.network_endpoints import get_active_endpoint

        server = get_server(server_id)
        if not server:
            return {"server_id": server_id}
        ep = get_active_endpoint(server_id)
        return {
            "server_id": server_id,
            "host": ep["host"] if ep else None,
            "port": int(server["port"]) if server.get("port") else None,
            "instance_name": server.get("instance_name"),
            "api_key": resolve_server(server_id),
        }

    async def check_health(self, litellm_model: str) -> tuple[bool, str]:
        from infra.db_async import run_in_db
        from llm.network_discovery import get_server
        from llm.network_endpoints import freshness_guard
        from llm.providers.omlx import (
            OmlxLoadedStateError,
            OmlxModelNotLoadedError,
            check_server,
            require_loaded_model,
        )

        server_id, provider_model_id = self._parse(litellm_model)
        server = await run_in_db(get_server, server_id)
        if not server:
            return False, "Network server not found"
        await freshness_guard(server_id)
        cfg = await run_in_db(self._cfg, server_id)
        result = await check_server(cfg)
        if not result.get("running"):
            name = server.get("instance_name", server_id)
            return False, f"oMLX server '{name}' is not reachable"
        try:
            await require_loaded_model(
                result.get("api_base"), provider_model_id, server_id=server_id,
            )
        except OmlxModelNotLoadedError:
            name = server.get("instance_name", server_id)
            return False, f"Model not loaded on '{name}'. Load it on the server and try again."
        except OmlxLoadedStateError as error:
            return False, str(error)
        return True, ""

    def not_loaded_reason(self, litellm_model: str) -> str:
        from llm.network_discovery import get_server
        server_id, _ = self._parse(litellm_model)
        server = get_server(server_id)
        name = server.get("instance_name", server_id) if server else server_id
        return f"Model not loaded on '{name}'. Load it on the server and try again."

    def make_provider_params(self, litellm_model: str) -> dict[str, Any]:
        from llm.network_endpoints import build_api_base
        server_id, provider_model_id = self._parse(litellm_model)
        api_base = build_api_base(server_id)
        if not api_base:
            raise ValueError(f"oMLX server has no reachable endpoint: {server_id}")
        params: dict[str, Any] = {
            "model": f"openai/{provider_model_id}",
            "api_key": self.name,
            "api_base": api_base,
            "omlx_server_id": server_id,
            "uses_streaming": True,
        }
        return params

    def transport_identity(
        self,
        litellm_model: str,
    ) -> tuple[str | None, str | None]:
        _server_id, provider_model_id = self._parse(litellm_model)
        return "openai", provider_model_id or None


class ExternalBackend(LocalModelBackend):
    """Generic External (user-owned) provider — observe-only.

    Format ``external@<server_id>/<model_id>``. Dispatch params are built per
    the server's ``dialect`` (``openai`` → litellm OpenAI-compat;
    ``anthropic`` → the native SDK with a custom ``base_url``). CORSIA observes
    load/readiness through the shared health sweep but never load/unloads — the
    user runs the endpoint. Substrate (``network_servers``/endpoints) is shared
    with the managed oMLX server; ``kind='external'`` is the only divergence."""

    name = "external"
    _prefix = "external@"

    def matches(self, litellm_model: str) -> bool:
        return litellm_model.startswith(self._prefix)

    def _parse(self, litellm_model: str) -> tuple[str, str]:
        """Split ``external@<server_id>/<model_id>`` into the two parts."""
        rest = litellm_model[len(self._prefix):]
        server_id, _, provider_model_id = rest.partition("/")
        return server_id, provider_model_id

    async def check_health(self, litellm_model: str) -> tuple[bool, str]:
        from infra.db_async import run_in_db
        from llm.network_discovery import get_server
        from llm.network_endpoints import freshness_guard, get_active_endpoint

        server_id, _ = self._parse(litellm_model)
        server = await run_in_db(get_server, server_id)
        if not server:
            return False, "External provider not found"
        await freshness_guard(server_id)
        if await run_in_db(get_active_endpoint, server_id) is None:
            name = server.get("instance_name", server_id)
            return False, f"External provider '{name}' is not reachable"
        return True, ""

    def not_loaded_reason(self, litellm_model: str) -> str:
        from llm.network_discovery import get_server
        server_id, _ = self._parse(litellm_model)
        server = get_server(server_id)
        name = server.get("instance_name", server_id) if server else server_id
        return f"Model not available on '{name}'. Run it on your server and try again."

    def make_provider_params(self, litellm_model: str) -> dict[str, Any]:
        from llm.external_provider import build_external_params
        server_id, provider_model_id = self._parse(litellm_model)
        return build_external_params(server_id, provider_model_id)

    def transport_identity(
        self,
        litellm_model: str,
    ) -> tuple[str | None, str | None]:
        from llm.network_discovery import get_server

        server_id, provider_model_id = self._parse(litellm_model)
        server = get_server(server_id)
        if not server:
            return None, provider_model_id or None
        dialect = "anthropic" if server.get("dialect") == "anthropic" else "openai"
        return dialect, provider_model_id or None


# Registry. Order doesn't matter; matches() is prefix-specific and exclusive.
_BACKENDS: tuple[LocalModelBackend, ...] = (
    ExternalBackend(),
    OmlxBackend(),
)


def find_backend(litellm_model: str) -> LocalModelBackend | None:
    """Return the backend that serves ``litellm_model``, or None (cloud)."""
    if not litellm_model:
        return None
    for backend in _BACKENDS:
        if backend.matches(litellm_model):
            return backend
    return None


def is_local(litellm_model: str) -> bool:
    """True if ``litellm_model`` is served by a registered local backend."""
    return find_backend(litellm_model) is not None


def backend_name(litellm_model: str) -> str | None:
    """Return the backend's ``name`` for logging / catalog queries."""
    b = find_backend(litellm_model)
    return b.name if b else None
