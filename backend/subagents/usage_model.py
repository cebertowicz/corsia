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


def usage_model_id(provider: str, live) -> str | None:
    """Ledger model: provider-reported, else catalog-expected. Kept apart
    from ``provider_reported_model_id`` so message rows stay unmixed."""
    reported = getattr(live, "provider_reported_model", None) or getattr(
        live, "expected_reported_model", None
    )
    return resolved_external_model_id(provider, reported)


def usage_effort(live) -> str | None:
    """The effort a live harness session was launched with, or None when unknown.

    A live session keeps the effort it started with whatever a later send
    asked for, so the session (not the request) is what the call ran under.
    """
    effort = getattr(getattr(live, "session", None), "effort", None)
    text = str(effort).strip() if effort is not None else ""
    return text or None
