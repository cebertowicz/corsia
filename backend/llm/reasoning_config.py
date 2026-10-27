"""Reasoning-capability descriptors per provider/model.

The catalog boolean is not enough: some providers use effort enums, some use
token budgets, and local models may use a boolean chat-template toggle.

Cloud (anthropic/openai) reasoning shapes come from the **hosted-catalog**
snapshot (``llm.hosted_catalog.cloud_models``) — they used to be hardcoded family
tables here and in ``anthropic_thinking.py`` (the "lists that must stay in sync"
smell, PROVIDER-STRATEGY-MANAGED-VS-EXTERNAL.md §8 #4).

Local (oMLX) reasoning shapes are **discovered from the server**, which reads
each model's chat template and reports what that template actually accepts.
They are built in ``reasoning_shape_from_discovery`` and stored on the catalog
row, so adding a model needs no code here. This replaced a model-name matcher,
which could only ever be as accurate as a model's filename.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from llm.resolution import ExecutionTarget


def reasoning_shape_from_discovery(
    model: dict[str, Any], *, managed_effort: str | None = None,
) -> dict[str, Any] | None:
    """Build a reasoning descriptor from what an oMLX server reported.

    Three outcomes, in order:

    * discrete levels the template validates → an ``effort_levels`` picker,
      opening on whatever level the server is currently configured to use.
      ``managed_effort`` is CORSIA's level for its own server
      (``local_profiles.MANAGED_REASONING_EFFORT``): when that server sets no
      level and the model lists it, the picker opens on it and
      ``send_when_unset`` names it, since only a level on the wire overrides
      the template's default;
    * a thinking toggle but no levels → a ``boolean`` control, opening on the
      server's current setting;
    * no thinking support, or a server too old to report any of this → None,
      and no control is offered.

    A server that omits these fields is saying "I can't tell you", which is not
    the same as "this model has none" — both yield None here, but the absent
    case must never be recorded as a positive claim about the model.
    """
    efforts = model.get("reasoning_efforts")
    # In oMLX 0.7.1rc1, [] means the template reads reasoning_effort but does
    # not name standard levels. Its own _reasoning_levels exposes the generic
    # options in that distinct case. An omitted field is an older server and
    # must not be conflated with []; its generic custom list is untrusted.
    if efforts == []:
        efforts = model.get("reasoning_effort_options")
    if efforts:
        levels = [str(level) for level in efforts]
        configured = model.get("configured_reasoning_effort")
        if configured not in levels and managed_effort in levels:
            return {
                "type": "effort_levels",
                "values": levels,
                "default": managed_effort,
                "send_when_unset": managed_effort,
            }
        declared = model.get("reasoning_effort_default")
        default = configured if configured in levels else declared
        return {
            "type": "effort_levels",
            "values": levels,
            "default": default if default in levels else levels[0],
        }

    thinking_default = model.get("thinking_default")
    if thinking_default is None:
        return None

    configured_toggle = model.get("configured_enable_thinking")
    current = (
        bool(configured_toggle)
        if configured_toggle is not None
        else bool(thinking_default)
    )
    return {"type": "boolean", "default": current}


def reasoning_config_for(
    provider: str, provider_model_id: str, litellm_model: str | None = None,
) -> dict[str, Any] | None:
    """Return a reasoning-capability descriptor for the given model, or None
    if the model doesn't support extended thinking.

    Resolution order: the shape stored on the catalog row wins — for oMLX that
    is what the server reported at discovery; then cloud shapes come from the
    hosted-catalog snapshot. A non-reasoning model, or one whose server is too
    old to report its capability, legitimately resolves to None.

    A caller holding the catalog row passes its ``litellm_model``: one External
    model id can be served by several endpoints, each with its own levels.
    """
    if not provider_model_id:
        return None
    catalog_shape = _catalog_reasoning_shape(provider, provider_model_id, litellm_model)
    if catalog_shape is not None:
        return catalog_shape
    if provider in ("openai", "anthropic"):
        from llm.hosted_catalog.cloud_models import reasoning_shape_for
        return reasoning_shape_for(provider, provider_model_id)
    return None


def reasoning_config_for_model(model: str) -> dict[str, Any] | None:
    """Resolve the reasoning shape for a catalog model id or litellm model string.

    None when the model has no catalog row or no shape; callers treat that as
    "vocabulary unknown".
    """
    from llm.catalog import get_model, get_model_by_litellm

    row = get_model(model) or get_model_by_litellm(model)
    if not row:
        return None
    return reasoning_config_for(
        row["provider"], row["provider_model_id"], row.get("litellm_model"),
    )


def listed_effort_levels(model: str) -> list[str] | None:
    """The effort levels this model's row lists, or None when they are unknown."""
    config = reasoning_config_for_model(model)
    if not config or config.get("type") != "effort_levels":
        return None
    return [str(v) for v in config.get("values") or []] or None


def effective_reasoning_effort(raw: str | None, model: str) -> str | None:
    """The level a thread's saved value means for the model it now uses.

    The saved value is a free string chosen for whatever model the thread had at
    the time; the vocabulary belongs to the model. This is the one place that
    reconciles the two, and every reader of the saved value goes through it, so
    dispatch, cache warm, checkpoints and the picker agree by construction.

    * ``None``, ``auto`` and ``none`` are returned as they are, without a
      catalog read; an unset level on the managed oMLX is filled where the
      request is shaped (``wire_reasoning_effort``);
    * no reported vocabulary (older server, free-form template, toggle-only
      model): the value passes through and the server validates it;
    * a level the model's vocabulary contains: itself;
    * a level it does not contain: the shape's default, which is the level the
      picker shows for exactly this case; or, for a shape with an "auto" mode
      (an External model's published levels), None, so the provider's own
      default runs.
    """
    if raw is None:
        return None
    effort = str(raw).strip().lower()
    if effort in ("", "auto", "none"):
        return effort or None
    config = reasoning_config_for_model(model)
    if config is None or config.get("type") != "effort_levels":
        return effort
    values = [str(v) for v in config.get("values") or []]
    if effort in values:
        return effort
    if config.get("supports_auto"):
        return None
    default = config.get("default")
    return str(default) if default in values else (values[0] if values else None)


def resolve_thread_effort(requested, thread_row: Any, target: ExecutionTarget) -> str | None:
    """The effort a turn sends: the request's, else the thread's, fitted to the target."""
    if requested is None and "reasoning_effort" in thread_row.keys():
        requested = thread_row["reasoning_effort"]
    return effective_reasoning_effort(requested, target.catalog_id)


def _catalog_reasoning_shape(
    provider: str, provider_model_id: str, litellm_model: str | None = None,
) -> dict[str, Any] | None:
    """Look up a stored ``reasoning_shape`` from the catalog, or None.

    None means the catalog holds no shape for this model. A database failure or
    unreadable metadata propagates: for a local model the catalog is the only
    source, so swallowing either would report "no reasoning support" for a
    model that has it.
    """
    from infra.database import get_db

    db = get_db()
    try:
        if litellm_model:
            row = db.execute(
                "SELECT metadata_json FROM models WHERE provider = ? AND litellm_model = ?",
                (provider, litellm_model),
            ).fetchone()
        else:
            row = db.execute(
                "SELECT metadata_json FROM models "
                "WHERE provider = ? AND provider_model_id = ?",
                (provider, provider_model_id),
            ).fetchone()
    finally:
        db.close()
    return reasoning_shape_from_metadata(row["metadata_json"] if row else None)


def reasoning_shape_from_metadata(metadata_json: str | None) -> dict[str, Any] | None:
    """Read the stored shape from already-selected metadata; corrupt JSON raises."""
    if not metadata_json:
        return None
    meta = json.loads(metadata_json)
    if isinstance(meta, dict):
        shape = meta.get("reasoning_shape")
        if isinstance(shape, dict):
            return shape
    return None
