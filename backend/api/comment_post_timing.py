"""Where a browser's comment post spent its time, logged from the ``X-Corsia-Comment-Timing`` header.

The browser sends numbers only, as ``key=value`` pairs joined by commas: no comment text and no ids.
The parts known before a POST ride on that POST; the parts known after it ride on the tab's next
comment request. A header that is too long, names an unknown key, repeats a key or carries a value
of the wrong shape is dropped whole, so the log line never carries anything the browser was not
meant to send.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

COMMENT_TIMING_HEADER = "X-Corsia-Comment-Timing"
MAX_HEADER_BYTES = 512

_NUMBER = re.compile(r"\d{1,9}")
_ARTIFACT_ID = re.compile(r"[\w.-]{1,128}")
_OUTCOMES = frozenset({"delivered", "failed", "timeout", "earlier"})
_NUMBER_KEYS = frozenset({"n", "a", "cap", "ens", "pn", "rec", "total", "post", "app", "recms"})


def parse_comment_timing(raw: str | None) -> dict[str, int | str] | None:
    """The header's pairs, or ``None`` when it is absent or malformed in any part."""
    if not raw or len(raw.encode("utf-8", "surrogatepass")) > MAX_HEADER_BYTES:
        return None
    parsed: dict[str, int | str] = {}
    for pair in raw.split(","):
        key, sep, value = pair.strip().partition("=")
        if not sep or key in parsed:
            return None
        if key in _NUMBER_KEYS and _NUMBER.fullmatch(value):
            parsed[key] = int(value)
        elif key == "out" and value in _OUTCOMES:
            parsed[key] = value
        else:
            return None
    return parsed


def log_comment_timing(raw: str | None, method: str, artifact_id: str) -> None:
    """One INFO line for a well-formed header; nothing for an absent or malformed one."""
    parsed = parse_comment_timing(raw)
    if parsed is None:
        return
    fields = " ".join(f"{key}={value}" for key, value in parsed.items())
    artifact = artifact_id if _ARTIFACT_ID.fullmatch(artifact_id) else "-"
    log.info("comment_post_timing method=%s artifact=%s %s", method, artifact, fields)
