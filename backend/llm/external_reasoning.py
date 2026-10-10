"""Reasoning levels on External catalog rows: written at discovery, read at dispatch.

Discovery stores the levels the public catalog lists for each model
(:mod:`llm.public_model_catalog`) as the row's ``reasoning_shape``, the same
stored shape oMLX discovery writes. Dispatch reads them back so that only a
listed level is ever sent (``reasoning_config.listed_effort_levels``, the
same lookup the picker uses, keyed by the row's own ``litellm_model``).
"""

from __future__ import annotations

from llm.reasoning_config import reasoning_shape_from_metadata


async def discovery_shapes(
    dialect: str, api_base: str, model_ids: list[str],
) -> tuple[dict[str, dict | None] | None, str | None]:
    """``(shapes, warning)`` for one endpoint's models; OpenAI dialect only.

    The catalog lists OpenAI-compatible bases and the ``reasoning_effort``
    vocabulary sent there, so an Anthropic-dialect endpoint gets no levels.
    ``shapes`` is None when the catalog could not be read at all.
    """
    if dialect != "openai" or not model_ids:
        return {mid: None for mid in model_ids}, None
    from llm.public_model_catalog import reasoning_shapes

    return await reasoning_shapes(api_base, model_ids)


def keep_stored_shape(stored_metadata_json: str | None, metadata: dict) -> None:
    """Carry a row's stored levels into ``metadata`` when none could be read now."""
    kept = reasoning_shape_from_metadata(stored_metadata_json)
    if kept:
        metadata["reasoning_shape"] = kept
