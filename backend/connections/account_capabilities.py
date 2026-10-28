"""Decode stored account capabilities for discovery without exposing row values."""

import json
import logging

log = logging.getLogger(__name__)


def decode_capabilities(raw: object) -> list[str] | None:
    """Skip a malformed row while allowing healthy accounts to remain discoverable."""
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list) and all(isinstance(item, str) for item in parsed):
            return parsed
    log.warning("Skipping malformed connected account capabilities")
    return None
