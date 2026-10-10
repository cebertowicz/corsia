"""Managed-engine seam — the one interface a bundled inference engine implements.

A *managed* engine is one whose process lifecycle CORSIA owns: spawn/supervise,
load/unload, health, boot reconcile. Today there is exactly **one** managed
engine — oMLX (Apple-silicon, MLX-accelerated). The point of this seam is
forward-design: a future Windows/Linux managed engine is a NEW implementation of
``ManagedEngine`` + a registry entry, not a rewrite of the lifecycle layer
(HMC18/HMC19). Do not build a second implementation here speculatively.

External providers (``kind='external'``) are user-owned and observe-only — they
have NO managed engine (CORSIA never loads/unloads them). See
``llm.external_provider``.

``cfg`` is the sparse server dict the lifecycle layer passes (at minimum
``{"server_id": ...}``); the engine resolves host/port/endpoint from the
``network_servers`` row itself.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ManagedEngine(Protocol):
    """Contract for a CORSIA-managed inference engine (process lifecycle owner)."""

    name: str

    async def ensure_live(self, cfg: dict) -> dict[str, Any]:
        """Start the engine's server if it isn't already running."""

    async def stop(self, cfg: dict) -> dict[str, Any]:
        """Stop the engine's server."""

    def reconcile(self, cfg: dict) -> dict[str, Any]:
        """Reconcile supervisor state against reality at boot (adopt/clear pids)."""

    async def load(self, cfg: dict, model_key: str, **opts: Any) -> dict[str, Any]:
        """Load a model into the running engine."""

    async def unload(self, cfg: dict, model_key: str) -> dict[str, Any]:
        """Unload a model from the running engine."""

    async def list_loaded(self, cfg: dict) -> list[dict]:
        """Models currently loaded in memory."""

    async def list_disk(self, cfg: dict) -> list[dict]:
        """Models available on disk (not necessarily loaded)."""

    async def health(self, cfg: dict) -> dict[str, Any]:
        """Liveness/health of the engine's server."""


class OmlxEngine:
    """The sole managed engine — thin adapter over ``llm.providers.omlx``.

    Behaviour-preserving: every method delegates to the existing oMLX provider
    functions. Adding a second managed engine means writing a sibling class and
    registering it below — the lifecycle layer calls only this interface."""

    name = "omlx"

    async def ensure_live(self, cfg: dict) -> dict[str, Any]:
        from llm.providers import omlx
        return await omlx.start_server(cfg)

    async def stop(self, cfg: dict) -> dict[str, Any]:
        from llm.providers import omlx
        return await omlx.stop_server(cfg)

    def reconcile(self, cfg: dict) -> dict[str, Any]:
        from llm.providers.omlx_supervisor import reconcile
        return reconcile(cfg)

    async def load(self, cfg: dict, model_key: str, **opts: Any) -> dict[str, Any]:
        from llm.providers import omlx
        return await omlx.load_model(cfg, model_key, **opts)

    async def unload(self, cfg: dict, model_key: str) -> dict[str, Any]:
        from llm.providers import omlx
        return await omlx.unload_model(cfg, model_key)

    async def list_loaded(self, cfg: dict) -> list[dict]:
        from llm.providers import omlx
        return await omlx.list_loaded(cfg)

    async def list_disk(self, cfg: dict) -> list[dict]:
        from llm.providers import omlx
        return await omlx.list_disk_models(cfg)

    async def health(self, cfg: dict) -> dict[str, Any]:
        from llm.providers import omlx
        return await omlx.check_server(cfg)


# The managed-engine registry. oMLX is the sole implementation today; a future
# platform engine is added here as a new entry, nothing else changes.
_ENGINES: dict[str, ManagedEngine] = {"omlx": OmlxEngine()}


def get_managed_engine(provider: str) -> ManagedEngine:
    """Return the managed engine for a provider key, or raise.

    Call sites never invent a provider string — it comes from the
    ``network_servers.provider`` column. External providers are NOT managed and
    must not reach here."""
    engine = _ENGINES.get(provider)
    if engine is None:
        raise ValueError(f"No managed engine for provider: {provider!r}")
    return engine


def is_managed(provider: str) -> bool:
    """True when ``provider`` names a managed engine (vs an External provider)."""
    return provider in _ENGINES
