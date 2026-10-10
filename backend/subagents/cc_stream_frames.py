"""Provider-facing stream-json frame encoding and decoding for Claude Code."""

from __future__ import annotations

import base64
import asyncio
import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.media_projection import ValidatedMediaProjection

log = logging.getLogger(__name__)


async def read_raw_event(session, limit: int) -> tuple[bool, object | None]:
    """Read one provider frame, preserving the existing dropped-line policy."""
    assert session._proc is not None and session._proc.stdout is not None
    try:
        line = await session._proc.stdout.readline()
    except (asyncio.LimitOverrunError, ValueError) as exc:
        log.error("cc_stream: dropped oversized stdout line (limit %s bytes): %s", limit, exc)
        if session._on_event is not None:
            await session._on_event({"kind": "stream_line_dropped"})
        return False, None
    if not line:
        return True, None
    try:
        return False, json.loads(line)
    except json.JSONDecodeError:
        log.debug("cc_stream: skipping non-JSON line: %r", line[:120])
        return False, None


def build_user_frame(
    text: str,
    media_projection: "ValidatedMediaProjection | None" = None,
    *, input_id: str | None = None,
) -> bytes:
    """Build one exact Claude Code 2.1.218 stream-json user frame."""
    images = media_projection.images if media_projection is not None else ()
    content: str | list[dict] = text
    if images:
        content = [
            *(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": image.mime_type,
                        "data": base64.b64encode(image.data).decode("ascii"),
                    },
                }
                for image in images
            ),
            {"type": "text", "text": text},
        ]
    payload = {"type": "user", "message": {"role": "user", "content": content}}
    if input_id is not None:
        payload["uuid"] = input_id
    return (json.dumps(payload) + "\n").encode()
