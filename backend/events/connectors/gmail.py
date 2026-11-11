"""Gmail connector manifest (declared data).

Gmail has no pre-existing artifact for a first-seen thread; the envelope carries
the *candidate* email-thread identity (``thread_id``) and a Connections rule
(S3) supplies ownership on first registration. After that, a repeat message
resolves by ownership exactly like a GitHub PR — same path, no Gmail router.

Gmail's own ids are local to one mailbox, so an event's identity includes its
``account_id`` and the producer namespaces ``thread_id`` as
``google:<account-id>:thread:<gmail-thread-id>``: two mailboxes never collide.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from events.connectors import ConnectorManifest, EventTypeDecl, register
from events.identity_formula import RelationshipDecl
from events.rule_match import CASELESS, DOMAIN

GMAIL_INTEGRATION = "gmail"
EMAIL_THREAD_TYPE = "email-thread"
EMAIL_RECEIVED = "email.received"

_MESSAGE_TO_THREAD = RelationshipDecl("message", "message_to_email_thread", "email-thread")

# ``from`` is the header as screening leaves it (a display name only after a clean screen);
# ``from_address`` and ``from_domain`` are its bare address and domain, on every message.
_FILTERABLE = ("from", "from_address", "from_domain", "to", "label", "thread_id", "message_id",
               "author_origin")


def _classify(raw: dict[str, Any]) -> str:
    return EMAIL_RECEIVED


def _extract_facts(raw: dict[str, Any], _resolved: Any = None) -> dict[str, Any]:
    keys = ("account_id", "from", "from_address", "from_domain", "to", "label", "thread_id",
            "message_id", "date", "history_id", "subject", "screen_status", "needs_person", "injection", "injection_rule", "kind",
            "screen_model")
    return {**{k: raw[k] for k in keys if raw.get(k) is not None}, "author_origin": "outside"}


def _unrouted_scope(facts: Mapping[str, Any]) -> frozenset[str] | None:
    """Mail in a conversation no agent owns reaches agents' own rules in the projects
    its account reaches; the poller's reach filter still holds each recipient to it."""
    from connections.connected_accounts import reached_projects
    from infra.database import get_db

    account = facts.get("account_id")
    if not account:
        return frozenset()
    db = get_db()
    try:
        return reached_projects(str(account), db=db)
    finally:
        db.close()


def _artifact_ref(event_type: str, facts: dict, raw: dict) -> dict | None:
    thread_id = facts.get("thread_id")
    if thread_id is None:
        return None
    return {"integration": GMAIL_INTEGRATION, "type": EMAIL_THREAD_TYPE,
            "external_id": thread_id}


def _relationships(event_type: str, facts: dict, raw: dict) -> tuple[dict, ...]:
    thread_id = facts.get("thread_id")
    if thread_id is None:
        return ()
    return ({"edge": _MESSAGE_TO_THREAD.edge, "to_type": _MESSAGE_TO_THREAD.to_type,
             "to_ref": thread_id},)


MANIFEST = register(ConnectorManifest(
    connector="gmail",
    namespace="gmail",
    version="1",
    event_types={
        EMAIL_RECEIVED: EventTypeDecl(
            EMAIL_RECEIVED, ("account_id", "message_id"), _FILTERABLE,
            artifact_type=EMAIL_THREAD_TYPE, artifact_integration=GMAIL_INTEGRATION,
            relationships=(_MESSAGE_TO_THREAD,), event_class="informational",
            actionable_when={"screen_status": "screened", "injection": False,
                             "needs_person": "now"},
            collapse_fact="thread_id"),
    },
    classify=_classify,
    extract_facts=_extract_facts,
    build_artifact_ref=_artifact_ref,
    resolve_relationships=_relationships,
    unrouted_scope=_unrouted_scope,
    fact_comparison={"from_address": CASELESS, "from_domain": DOMAIN},
))
