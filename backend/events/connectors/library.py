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
    "changed_files",
    "edited",
    "withdrawn_reason",
)


def _classify(raw: dict[str, Any]) -> str:
    return raw.get("event_type", "")


def _extract_facts(raw: dict[str, Any]) -> dict[str, Any]:
    facts = {key: raw[key] for key in _FACTS if raw.get(key) is not None}
    name = facts.get("name")
    if name is not None:
        # A changed item's digest groups by the item and its new version; a
        # withdrawal has no new version and groups by its item alone.
        version = facts.get("library_version")
        facts["item_key"] = f"{name}:{version}" if version is not None else name
    return facts


def _artifact_ref(event_type: str, facts: dict, raw: dict) -> dict | None:
    external_id = facts.get("external_id")
    if external_id is None:
        return None
    return {
        "integration": LIBRARY_INTEGRATION,
        "type": INSTALLED_SKILL_TYPE,
        "external_id": external_id,
    }


MANIFEST = register(ConnectorManifest(
    connector=LIBRARY_CONNECTOR,
    namespace=LIBRARY_CONNECTOR,
    version="1",
    event_types={
        ITEM_CHANGED_EVENT: EventTypeDecl(
            ITEM_CHANGED_EVENT,
            ("external_id", "library_version"),
            _FILTERABLE,
            artifact_type=INSTALLED_SKILL_TYPE,
            artifact_integration=LIBRARY_INTEGRATION,
            event_class="informational",
            collapse_fact="item_key",
        ),
        ITEM_WITHDRAWN_EVENT: EventTypeDecl(
            ITEM_WITHDRAWN_EVENT,
            ("external_id",),
            _FILTERABLE,
            artifact_type=INSTALLED_SKILL_TYPE,
            artifact_integration=LIBRARY_INTEGRATION,
            event_class="actionable",
            collapse_fact="item_key",
        ),
    },
    classify=_classify,
    extract_facts=_extract_facts,
    build_artifact_ref=_artifact_ref,
))
