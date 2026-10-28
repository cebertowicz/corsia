"""Library installed-skill notice connector manifest (declared data).

A library producer reports that a thread-owned installed skill changed upstream
or was withdrawn.  The manifest identifies that installed copy for the generic
platform resolver; it neither stores state nor chooses a route.
"""

from __future__ import annotations

from typing import Any

from events.connectors import ConnectorManifest, EventTypeDecl, register

LIBRARY_CONNECTOR = "library"
LIBRARY_INTEGRATION = "library"
INSTALLED_SKILL_TYPE = "installed-skill"
INSTALLED_PACK_TYPE = "installed-pack"
PACK_CHANGED_EVENT = "library.pack_changed"
ITEM_CHANGED_EVENT = "library.item_changed"
ITEM_WITHDRAWN_EVENT = "library.item_withdrawn"


# Library notices always resolve through the installed copy's registered
# artifact. No rule binding is needed, so this connector exposes no filterable
# fields.
_FILTERABLE: tuple[str, ...] = ()
_FACTS = (
    "external_id",
    "name",
    "installed_version",
    "library_version",
    "item_manifest",
    "library_index_generation",
    "installed_at",
    "changed_files",
    "edited",
    "withdrawn_reason",
)
# What a digest note line shows. Names, versions and file paths come from the
# reviewed index; the free-text withdrawn_reason stays out of the line.
_CHANGED_LINE_FACTS = ("name", "installed_version", "library_version", "edited", "changed_files")
_WITHDRAWN_LINE_FACTS = ("name", "installed_version", "edited")


def _classify(raw: dict[str, Any]) -> str:
    return raw.get("event_type", "")


def _extract_facts(raw: dict[str, Any], _resolved: Any = None) -> dict[str, Any]:
    facts = {key: raw[key] for key in _FACTS if raw.get(key) is not None}
    name = facts.get("name")
    if name is not None:
        # Group notices by the actual reviewed item state, not a mutable version.
        manifest = facts.get("item_manifest")
        facts["item_key"] = f"{name}:{manifest}" if manifest is not None else name
    return facts


def _artifact_ref(event_type: str, facts: dict, raw: dict) -> dict | None:
    external_id = facts.get("external_id")
    if external_id is None:
        return None
    return {
        "integration": LIBRARY_INTEGRATION,
        "type": INSTALLED_PACK_TYPE if event_type == PACK_CHANGED_EVENT else INSTALLED_SKILL_TYPE,
        "external_id": external_id,
    }


MANIFEST = register(ConnectorManifest(
    connector=LIBRARY_CONNECTOR,
    namespace=LIBRARY_CONNECTOR,
    version="1",
    event_types={
        PACK_CHANGED_EVENT: EventTypeDecl(
            PACK_CHANGED_EVENT,
            ("external_id", "item_manifest", "library_index_generation", "installed_at"),
            _FILTERABLE,
            artifact_type=INSTALLED_PACK_TYPE,
            artifact_integration=LIBRARY_INTEGRATION,
            event_class="informational",
            collapse_fact="item_key",
            digest_line_facts=_CHANGED_LINE_FACTS,
        ),
        ITEM_CHANGED_EVENT: EventTypeDecl(
            ITEM_CHANGED_EVENT,
            ("external_id", "item_manifest", "library_index_generation", "installed_at"),
            _FILTERABLE,
            artifact_type=INSTALLED_SKILL_TYPE,
            artifact_integration=LIBRARY_INTEGRATION,
            event_class="informational",
            collapse_fact="item_key",
            digest_line_facts=_CHANGED_LINE_FACTS,
        ),
        ITEM_WITHDRAWN_EVENT: EventTypeDecl(
            ITEM_WITHDRAWN_EVENT,
            ("external_id", "item_manifest", "library_index_generation", "installed_at"),
            _FILTERABLE,
            artifact_type=INSTALLED_SKILL_TYPE,
            artifact_integration=LIBRARY_INTEGRATION,
            event_class="actionable",
            collapse_fact="item_key",
            digest_line_facts=_WITHDRAWN_LINE_FACTS,
        ),
    },
    classify=_classify,
    extract_facts=_extract_facts,
    build_artifact_ref=_artifact_ref,
))
