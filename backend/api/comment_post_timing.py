"""Where a browser's comment post spent its time, logged from the ``X-Corsia-Comment-Timing`` header.

The browser sends numbers only, as ``key=value`` pairs joined by commas: no comment text and no ids.
The parts known before a POST ride on that POST; the parts known after it ride on the tab's next
comment request. A header that is too long, names an unknown key, repeats a key or carries a value
of the wrong shape is dropped whole, so the log line never carries anything the browser was not
meant to send.

The line goes through loguru: CORSIA's stdlib root handler and its log capture take only WARNING and above,
so an INFO line logged through stdlib ``logging`` would reach neither.
"""

from __future__ import annotations

import re

from loguru import logger

COMMENT_TIMING_HEADER = "X-Corsia-Comment-Timing"

# A number is milliseconds or a count of this tab's posts. Nine digits is 999,999,999 ms, over 11 days: a post's own
# bounds end it within a minute, and a tab's count of posts never gets there.
_NUMBER_DIGITS = 9
_NUMBER = re.compile(rf"\d{{1,{_NUMBER_DIGITS}}}")
_OUTCOMES = frozenset({"delivered", "failed", "timeout", "earlier"})
# A refused post says how: ``failed:<status>`` or ``failed:<status>:<code>``, where code is the refusal's own short
# machine name (``region_target``, ``selection_changed``), never its sentence, so no path or quote can ride along.
# 32 characters: the longest kind a comment post can be refused with today is ``suggestion_empty_insertion``, 26 (the
# others, from ``CommentActionError`` in comment_validation, image_regions, doc_images and comment_recipients, plus
# ``selection_changed`` and ``not_commentable``, are 20 or fewer), so six characters of room remain. The browser's own
# pattern in frontend/src/lib/stores/commentPostTiming.ts repeats this number.
_CODE_CHARS = 32
_FAILED = re.compile(rf"failed:\d{{3}}(?::[a-z][a-z_]{{0,{_CODE_CHARS - 1}}})?")
_LONGEST_OUTCOME = len("failed:999:") + _CODE_CHARS
_NUMBER_KEYS = frozenset({"n", "a", "cap", "ens", "pn", "rec", "total", "post", "app", "recms"})

# The longest header the browser builds (timingHeaders in frontend/src/lib/stores/commentPostTiming.ts): a POST's own
# n, a, cap and ens, plus the last finished post's report, pn, rec, out, total, post, app and recms, every one present:
#   n, a, cap, ens, pn, total, post, app, recms: key, "=", up to 9 digits  -> 27 key chars + 9 * 10 = 117
#   rec: "rec=" and 0 or 1                                                  ->   5
#   out: "out=" and its longest outcome, "failed:999:" and a 32-letter code ->  47
#   10 commas between the 11 pairs                                          ->  10
# 179 bytes. Anything longer is not a header the browser sent, so it is dropped whole.
MAX_HEADER_BYTES = (
    sum(len(key) + 1 + _NUMBER_DIGITS for key in _NUMBER_KEYS - {"rec"})
    + len("rec=1")
    + len("out=") + _LONGEST_OUTCOME
    + len(_NUMBER_KEYS)
)

# The artifact id from the route, logged only in the one shape artifact ids are made in:
# str(uuid.uuid4())[:12] (registry/registration_writer.py), 8 hex digits, a hyphen and 3 more. Any other value logs "-".
_ARTIFACT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{3}")


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
        elif key == "out" and (value in _OUTCOMES or _FAILED.fullmatch(value)):
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
    logger.info("comment_post_timing method={} artifact={} {}", method, artifact, fields)
