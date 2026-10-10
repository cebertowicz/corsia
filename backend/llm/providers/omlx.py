"""oMLX provider operations — direct HTTP to jundot/oMLX's native surface.

This provider module speaks to oMLX directly (no CORSIA-built shell). The endpoint
split is deliberate:

- Public OpenAI-compat ``/v1/...`` — Bearer key once oMLX has one (none on a
  fresh server, or once ``skip_api_key_verification`` is set on a loopback bind).
  ``GET /v1/models``, ``GET /v1/models/status``, ``POST /v1/models/{id}/unload``,
  ``POST /v1/chat/completions`` (warm pattern lives here).

- Admin ``/admin/api/...`` — session cookie from ``POST /admin/api/login``;
  oMLX's admin routes do not accept the Bearer key (model load accepts either).
  Every call goes through ``omlx_endpoints.admin_request``, which signs in with
  the registered key and keeps the session in memory.
  Model load (``POST /admin/api/models/{id}/load``), per-model settings
  (``PUT /admin/api/models/{id}/settings``), saved sampler profiles
  (``GET, POST /admin/api/models/{id}/profiles``), stats, hf-downloads.

cfg dict shape:
    {
        "server_id": str,            # opaque id; preserved for call-site compat
        "host":      str,            # e.g. "127.0.0.1" or "Jarviss-Mac-Studio.local"
        "port":      int,            # e.g. 8000
        "api_key":   str | None,     # optional admin token
        "instance_name": str | None, # display name; pass-through only
    }

cfg comes from ``llm.network_discovery.get_server`` rows, populated via the
manual-add path (``/api/settings/network-servers`` POST). When call sites
pass a sparse cfg (just ``server_id``), ``_resolve_endpoint`` fills in
host/port from the row + active endpoint.

The module is split for size: endpoint/URL helpers live in
``omlx_endpoints``, server+model lifecycle in ``omlx_lifecycle``, settings
ops in ``omlx_settings``, inference shaping in ``omlx_thinking``. Public
surface is preserved here via re-export.
"""

from __future__ import annotations

from llm.providers.omlx_endpoints import (
    _TIMEOUT,
    _admin_base,
    _api_base,
    _health_url,
    _resolve_endpoint,
)
from llm.providers.omlx_lifecycle import (
    _LOAD_TIMEOUT,
    _WARM_TIMEOUT,
    OmlxLoadedStateError,
    OmlxModelNotLoadedError,
    _check_single,
    check_server,
    get_cache_stats,
    list_disk_models,
    list_loaded,
    load_model,
    log,
    probe_cache,
    require_loaded_model,
    shutdown,
    start_server,
    stop_server,
    unload_model,
    warm_cache,
)
from llm.providers.omlx_settings import (
    get_model_config,
    get_model_runtime_info,
    get_model_settings,
    get_settings,
    update_model_settings,
    update_settings,
)
from llm.providers.omlx_thinking import (
    make_inference_extra_body,
    make_prompt_extra_body,
    wire_reasoning_effort,
)

__all__ = [
    "_TIMEOUT",
    "_LOAD_TIMEOUT",
    "_WARM_TIMEOUT",
    "OmlxLoadedStateError",
    "OmlxModelNotLoadedError",
    "_resolve_endpoint",
    "_api_base",
    "_admin_base",
    "_health_url",
    "check_server",
    "list_loaded",
    "list_disk_models",
    "load_model",
    "unload_model",
    "warm_cache",
    "probe_cache",
    "require_loaded_model",
    "get_cache_stats",
    "start_server",
    "stop_server",
    "_check_single",
    "shutdown",
    "get_settings",
    "update_settings",
    "get_model_config",
    "get_model_runtime_info",
    "get_model_settings",
    "update_model_settings",
    "make_inference_extra_body",
    "make_prompt_extra_body",
    "wire_reasoning_effort",
    "log",
]
