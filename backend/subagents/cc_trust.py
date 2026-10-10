"""Claude Code provider-context trust — owner O8.

``runtime-lifecycle-map.md`` §3.2 requires O8 to have a named home of its own
rather than being spread across the runtime's phase modules, and §3.3 rule 2
requires that home to either gain a durable store *or* declare itself
boot-scoped for both harnesses. **This module makes the second declaration
explicitly: CC provider-context distrust is BOOT-SCOPED.** It lives on the live
handle, it dies with the process, and nothing re-establishes it at boot.

Why boot-scoped and not durable, since the asymmetry with Codex is real (Codex
keeps distrust in a module-level set that outlives its handle; CC does not):
§4 Sibling 1 traced the exposure to a verdict of *consistency-only, no live
user-visible exposure through the send path* — a CC feed cannot be reached
without a prepared context, and a prepared context already encodes the fold. It
then ruled on the remedy directly: a durable CC distrust flag would be "a fifth
state to keep synchronized with four others," and the defect it would paper over
is the four-branch replay structure, not the missing column. Collapsing those
branches onto the single question of transcript authority (the
``cc/replay_authority.py`` seam) subsumes this gap; a durable flag would
entrench it. So the honest fix here is to name the lifetime, not to extend it.

What this module therefore guarantees, and all it guarantees:

* one writer, so the next effect O8 needs (a ``runtime_truth`` field for CC
  parity, or the durable store, if either is ever ruled in) has exactly one
  place to land instead of the 38 direct field writes this replaces;
* one stated lifetime, so a reader cannot mistake the CC flag's scope for the
  Codex one — the "same dispatcher, two lifetimes" ambiguity §4 Sibling 1 is
  about.

**A write here does not survive the handle.** ``subagents/lifecycle.py``'s
``terminate_and_pop`` sets the flag and then pops the registry slot (§4
Sibling 2); that write is discarded by construction. Do not add a call here to
"fix" that site — its defect is the write-then-discard order, which belongs to
the O5 handle seam.
"""

from __future__ import annotations

from subagents.cc_runtime_state import _LIVE, _Live


def is_distrusted(live: _Live | None) -> bool:
    """Return whether this exact live handle's provider transcript is distrusted."""
    return bool(getattr(live, "provider_context_untrusted", False))


def distrust(live: _Live | None) -> None:
    """Distrust this exact live handle's provider-local transcript.

    The sole writer. A ``None`` handle is accepted and is a no-op: there is no
    boot-scoped state to carry the decision, which is exactly the asymmetry this
    module's docstring declares.
    """
    if live is not None:
        live.provider_context_untrusted = True


def provider_context_untrusted(thread_id: str) -> bool:
    """Return whether Claude Code's provider-local transcript is distrusted."""
    return is_distrusted(_LIVE.get(thread_id))

