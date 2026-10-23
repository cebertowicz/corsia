"""Raw receipt identity helpers for lesson reporting."""
from __future__ import annotations

from typing import Any


def identity_for_raw(facts: list[dict[str, Any]], *, digest: str, path: str) -> dict[str, Any] | None:
    """Same-read raw hash/path tuples can identify behavior, never arbitrary cost."""
    matches = [fact for fact in facts if fact.get("raw_sha256") == digest and fact.get("path") == path]
    identities = {(fact.get("kind"), fact.get("id"), fact.get("version")) for fact in matches}
    if len(identities) != 1:
        return None
    kind, subject, version = next(iter(identities))
    return {"kind": kind, "id": subject, "version": version}
