"""One model identity per usage or turn row, for Cost, Spend and Models reads.

The ledger keeps the literal selector a call was made with. Which model that
selector ran as is owned elsewhere: a harness turn's context manifest records
the identity the catalog expected at call time, and the catalog maps its own ids
and selectors to the model they name. This module only reads those owners, in
that order. It never maps by date, family or string shape: an alias that
neither resolves stays its own literal key and is reported as unresolved; a
literal the catalog does not know is its own identity.

An unresolved alias is named by its own selector without the harness prefix,
never by a catalog name: the catalog names an alias by the model it maps to
today, so that name would claim a version the row does not record.

The catalog's current alias mapping answers only present-tense questions (is
this identity offered now, and what does the catalog call it), never which
model past usage ran as.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from connections.claude_unlisted import base_identity
from connections.source_state import HARNESS_PROVIDERS
from llm.model_status import projected_display_name
from llm.resolution import catalog_row
from subagents.child_thread import resolved_external_model_id

#: Appended to an unresolved alias's name: the row is that alias, model version unknown.
UNRESOLVED_SUFFIX = " (version not recorded)"


def identity_columns(alias: str) -> str:
    """Select the literal selector and its manifest identity from ``alias``."""
    return (
        f"{alias}.model AS model,cm.harness AS manifest_harness,"
        "cm.resolved_model AS manifest_model,cm.resolved_model_source AS manifest_source"
    )


def identity_join(alias: str) -> str:
    """Join the context manifest a usage or turn row was delivered under."""
    return f"LEFT JOIN context_manifests cm ON cm.id={alias}.context_manifest_id"


IDENTITY_GROUP = "model,manifest_harness,manifest_model,manifest_source"


@dataclass(frozen=True)
class ModelIdentity:
    """The full identity string a row is keyed by, and how to show it."""

    key: str
    label: str
    resolved: bool


def _meta(row: Mapping[str, Any]) -> dict[str, Any]:
    try:
        meta = json.loads(row.get("metadata_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return meta if isinstance(meta, dict) else {}


def _current_identity(row: Mapping[str, Any]) -> str | None:
    """The identity a harness catalog row resolves to now, or None for Native."""
    if row.get("harness") not in HARNESS_PROVIDERS:
        return None
    resolved = _meta(row).get("resolved_identity")
    return base_identity(str(resolved)) if resolved else ""


def _model_name(key: str) -> str:
    """``key`` without its harness prefix: a row names the model, never the harness it ran under."""
    harness, _, name = key.partition("/")
    return name if harness in HARNESS_PROVIDERS and name else key


def _is_alias(row: Mapping[str, Any]) -> bool:
    """A harness row whose selector names another model, or names none yet."""
    current = _current_identity(row)
    return current is not None and current != base_identity(str(row.get("provider_model_id") or ""))


class IdentityResolver:
    """Resolve rows against one connection's catalog, each distinct shape once."""

    def __init__(self, db) -> None:
        self._db = db
        self._rows: dict[str, dict[str, Any] | None] = {}
        self._labels: dict[str, str] | None = None
        self._resolved: dict[tuple[Any, ...], ModelIdentity] = {}

    def _row(self, model: str) -> dict[str, Any] | None:
        if model not in self._rows:
            try:
                self._rows[model] = catalog_row(model, db=self._db)
            except ValueError:
                # Two catalog rows claim this transport identity: the catalog
                # cannot say which model it names, so neither does this read.
                self._rows[model] = None
        return self._rows[model]

    def _label(self, key: str) -> str:
        """The catalog's name for ``key``, or its model name when no row names it."""
        row = self._row(key)
        if row is not None and not _is_alias(row):
            return projected_display_name(row, _meta(row))
        if self._labels is None:
            self._labels = {}
            marks = ",".join("?" * len(HARNESS_PROVIDERS))
            for raw in self._db.execute(
                f"SELECT * FROM models WHERE harness IN ({marks}) ORDER BY id", HARNESS_PROVIDERS,
            ).fetchall():
                candidate = dict(raw)
                current = _current_identity(candidate)
                if current:
                    self._labels.setdefault(
                        resolved_external_model_id(str(candidate["harness"]), current) or "",
                        projected_display_name(candidate, _meta(candidate)),
                    )
        return self._labels.get(key) or _model_name(key)

    def _known(self, key: str) -> ModelIdentity:
        return ModelIdentity(key, self._label(key), True)

    def __call__(self, row: Mapping[str, Any]) -> ModelIdentity:
        """Resolve one row carrying ``identity_columns``."""
        shape = (row.get("model"), row.get("manifest_harness"), row.get("manifest_model"), row.get("manifest_source"))
        if shape not in self._resolved:
            self._resolved[shape] = self._resolve(*shape)
        return self._resolved[shape]

    def _resolve(self, model: Any, harness: Any, reported: Any, source: Any) -> ModelIdentity:
        literal = str(model or "")
        if source == "catalog" and reported and harness in HARNESS_PROVIDERS:
            # A Native manifest records the transport wire alias, so only a
            # harness turn's catalog-expected identity outranks the ledger.
            key = resolved_external_model_id(str(harness), base_identity(str(reported)))
            if key:
                return self._known(key)
        key = base_identity(literal)
        row = self._row(literal) or self._row(key)
        if row is None:
            # Nothing in the catalog names it: the literal is its own identity.
            return self._known(key)
        if _is_alias(row):
            return ModelIdentity(key, _model_name(key) + UNRESOLVED_SUFFIX, False)
        return self._known(base_identity(str(row["litellm_model"])))

    def offered_now(self, catalog_entries: Iterable[Mapping[str, Any]]) -> set[str]:
        """Identity keys the current catalog offers, an alias by what it names now."""
        offered: set[str] = set()
        for entry in catalog_entries:
            selector = str(entry["litellm_model"])
            offered.add(self({"model": selector}).key)
            row = self._row(selector)
            if row is None:
                continue
            current = _current_identity(row)
            if current:
                offered.add(resolved_external_model_id(str(row["harness"]), current) or "")
        return offered
