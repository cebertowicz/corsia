"""Document-comment connector manifest (declared data).

A document is owned on creation, so a comment resolves via PATH A. Identity
includes ``version`` so a comment on a later document version is a distinct
event (EM3/AC-1.8). The same envelope works whether the editor is local,
browser-based, or external.
"""

from __future__ import annotations

from typing import Any

from events.connectors import ConnectorManifest, EventTypeDecl, register
from events.identity_formula import RelationshipDecl

DOCUMENT_INTEGRATION = "local"
DOCUMENT_TYPE = "document"

_COMMENT_TO_DOC = RelationshipDecl("comment", "comment_to_document", "document")

_FILTERABLE = ("document_id", "author", "anchor", "version")


def _classify(raw: dict[str, Any]) -> str:
    return "comment.created"


def _extract_facts(raw: dict[str, Any], _resolved: Any = None) -> dict[str, Any]:
    keys = ("document_id", "comment_id", "author", "anchor", "version")
    return {k: raw[k] for k in keys if raw.get(k) is not None}


def _artifact_ref(event_type: str, facts: dict, raw: dict) -> dict | None:
    document_id = facts.get("document_id")
    if document_id is None:
        return None
    return {"integration": DOCUMENT_INTEGRATION, "type": DOCUMENT_TYPE,
            "external_id": document_id}


def _relationships(event_type: str, facts: dict, raw: dict) -> tuple[dict, ...]:
    document_id = facts.get("document_id")
    if document_id is None:
        return ()
    return ({"edge": _COMMENT_TO_DOC.edge, "to_type": _COMMENT_TO_DOC.to_type,
             "to_ref": document_id},)


MANIFEST = register(ConnectorManifest(
    connector="document",
    namespace="document",
    version="1",
    event_types={
        "comment.created": EventTypeDecl(
            "comment.created", ("document_id", "comment_id", "version"), _FILTERABLE,
            artifact_type=DOCUMENT_TYPE, artifact_integration=DOCUMENT_INTEGRATION,
            relationships=(_COMMENT_TO_DOC,), event_class="informational"),
    },
    classify=_classify,
    extract_facts=_extract_facts,
    build_artifact_ref=_artifact_ref,
    resolve_relationships=_relationships,
))
