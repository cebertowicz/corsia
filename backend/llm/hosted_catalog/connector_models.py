"""Connector capability records, sourced from the hosted-catalog snapshot.

The data lives in ``snapshot/connectors/<id>.json`` and is read through the
general loader. Exact per-model runtime facts such as context windows do not
belong here; harness discovery owns them in the canonical model catalog.
"""

from __future__ import annotations

import copy
from typing import Any

from llm.hosted_catalog.loader import load_section

_SECTION = "connectors"


def capabilities_for(connector_id: str) -> dict[str, Any] | None:
    """Return the capability descriptor for a connector, or None if absent.

    None is a normal outcome for connectors that have no ``capabilities`` block
    (e.g. inference-provider connectors whose caps live in model-profile rows).
    The returned dict is a deep copy so callers cannot mutate the cached snapshot.
    """
    section = load_section(_SECTION)
    row = section.get(connector_id)
    if row is None:
        return None
    caps = row.get("capabilities")
    if caps is None:
        return None
    return copy.deepcopy(caps)
