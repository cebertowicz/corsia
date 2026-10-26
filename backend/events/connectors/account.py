"""Connected-account sign-in connector manifest (declared data).

The agent that started a provider sign-in is waiting on the person's browser.
When the OAuth callback finishes, ``connections.sign_in_notice`` reports how it
ended as one ``account.sign_in_finished`` event. The event's artifact is the
sign-in itself, first-seen at that moment, and it declares the starting thread
as its owner (the declared-owner path the binder already honors), so the
generic resolver wakes that thread. The event is actionable for every outcome:
success and failure both end the wait. Its identity is the sign-in alone, so a
replayed callback is the same event and the platform delivers it once.

Facts never carry a token, code or secret; ``message`` is a provider error's
own already-safe text.
"""

from __future__ import annotations

from typing import Any

from events.connectors import ConnectorManifest, EventTypeDecl, register

ACCOUNT_CONNECTOR = "account"
ACCOUNT_INTEGRATION = "account"
SIGN_IN_TYPE = "sign-in"
SIGN_IN_FINISHED_EVENT = "account.sign_in_finished"
CONNECTED = "connected"
FAILED = "failed"
#: The binder's declared-owner fact: the thread that owns a first-seen artifact.
OWNER_FACT = "declared_owner_thread"

_FACTS = ("signin_id", "provider", "account_id", "outcome", "address", "cause", "message", OWNER_FACT)
_FILTERABLE = ("provider", "outcome")
_LINE_FACTS = ("provider", "outcome", "address", "cause", "message")


def _classify(raw: dict[str, Any]) -> str:
    return SIGN_IN_FINISHED_EVENT


def _extract_facts(raw: dict[str, Any], _resolved: Any = None) -> dict[str, Any]:
    return {key: raw[key] for key in _FACTS if raw.get(key) is not None}


def _artifact_ref(event_type: str, facts: dict, raw: dict) -> dict | None:
    signin_id = facts.get("signin_id")
    if signin_id is None:
        return None
    return {"integration": ACCOUNT_INTEGRATION, "type": SIGN_IN_TYPE, "external_id": signin_id}


MANIFEST = register(ConnectorManifest(
    connector=ACCOUNT_CONNECTOR,
    namespace=ACCOUNT_CONNECTOR,
    version="1",
    event_types={
        SIGN_IN_FINISHED_EVENT: EventTypeDecl(
            SIGN_IN_FINISHED_EVENT, ("signin_id",), _FILTERABLE,
            artifact_type=SIGN_IN_TYPE,
            artifact_integration=ACCOUNT_INTEGRATION,
            event_class="actionable",
            collapse_fact="signin_id",
            digest_line_facts=_LINE_FACTS,
        ),
    },
    classify=_classify,
    extract_facts=_extract_facts,
    build_artifact_ref=_artifact_ref,
))
