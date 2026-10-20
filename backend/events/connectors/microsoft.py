"""Microsoft mail uses the same declared email event contract as Gmail."""
from dataclasses import replace

from events.connectors import gmail, register


def _artifact_ref(event_type: str, facts: dict, raw: dict) -> dict | None:
    ref = gmail.MANIFEST.build_artifact_ref(event_type, facts, raw)
    return {**ref, "integration": "microsoft"} if ref else None


MANIFEST = register(replace(
    gmail.MANIFEST,
    connector="microsoft",
    namespace="microsoft",
    event_types={
        key: replace(value, artifact_integration="microsoft")
        for key, value in gmail.MANIFEST.event_types.items()
    },
    build_artifact_ref=_artifact_ref,
))
