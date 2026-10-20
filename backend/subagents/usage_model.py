"""Ledger-only model identity: what a usage receipt is recorded under.

Message attribution and the ledger want different answers when a provider
hasn't reported a model yet. A message row keeps ``provider_reported_model_id``
unmixed -- ``model=None`` until the provider actually confirms one -- so the
model-agreement audit can tell a confirmed report from an assumed one. A
ledger receipt has no such audit: measured volume with no home is volume
lost, so it falls back to the catalog's expected model rather than being
discarded as ``model_unknown``. This module holds that one ledger-only
resolution, shared across the Codex and Claude Code harnesses.
"""

from __future__ import annotations

from subagents.child_thread import resolved_external_model_id


def ledger_model_id(provider: str, reported: str | None, expected: str | None) -> str | None:
    """Ledger model: provider-reported, else catalog-expected."""
    return resolved_external_model_id(provider, reported or expected)


def detached_usage_model_id(target, reported: str | None) -> str:
    """Ledger model of a call made outside a live session (checkpoint fork, host-access review).

    ``target`` is the call's catalog resolution, or telemetry carrying its
    fields. A harness call is keyed as a turn row of that harness would be;
    a Native call keeps its catalog id.
    """
    if target.harness == "native":
        return target.catalog_id
    expected = target.expected_reported_model or target.transport_model
    return ledger_model_id(target.harness, reported, expected) or target.catalog_id


def usage_model_id(provider: str, live) -> str | None:
    """Ledger model of a live session. Kept apart from
    ``provider_reported_model_id`` so message rows stay unmixed."""
    return ledger_model_id(
        provider,
        getattr(live, "provider_reported_model", None),
        getattr(live, "expected_reported_model", None),
    )


def usage_effort(live) -> str | None:
    """The effort a live harness session was launched with, or None when unknown.

    A live session keeps the effort it started with whatever a later send
    asked for, so the session (not the request) is what the call ran under.
    """
    effort = getattr(getattr(live, "session", None), "effort", None)
    text = str(effort).strip() if effort is not None else ""
    return text or None
