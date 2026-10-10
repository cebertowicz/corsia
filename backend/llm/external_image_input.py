"""Whether each External model accepts images: detected once, or set by the user.

An OpenAI- or Anthropic-compatible model list reports no input types, and some
plan endpoints list no models at all, so CORSIA asks the model: one small
solid-green image and the question of its colour, sent through the same
dispatch path a real turn uses. The answer lands on the row's
``supports_vision`` (what dispatch and the composer read), with its provenance
in ``metadata_json['image_input']``. A user's choice wins over detection and
survives rediscovery; nothing is inferred from a model's name.

Probes run only for a model never checked, or when the user tests the endpoint
or asks to detect again; never on a timer.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import struct
import zlib

from infra.database import get_db, write_transaction
from llm.external_provider import PREFIX, PROVIDER, _now, served_model_id

log = logging.getLogger(__name__)

YES, NO, UNKNOWN = "yes", "no", "unknown"
DETECTED, USER = "detected", "user"
_PROBE_TIMEOUT_S = 90
_PROBE_QUESTION = "What colour fills this image? Answer with one word."
_PROBE_COLOUR = "green"
# A provider refusing the image part says so in its error; any other failure
# (auth, rate limit, outage) says nothing about images and stays unknown.
_IMAGE_REJECTION = re.compile(
    r"image|vision|multimodal|modalit|content[ _-]?type|unsupported content",
    re.IGNORECASE,
)


def _probe_png(size: int = 64) -> bytes:
    """A ``size``-pixel square of pure green, as PNG bytes."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    row = b"\x00" + b"\x00\xc8\x00" * size
    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(row * size))
        + chunk(b"IEND", b"")
    )


def probe_messages() -> list[dict]:
    """The one-turn probe: the colour question and the test image."""
    data_url = "data:image/png;base64," + base64.b64encode(_probe_png()).decode()
    return [{
        "role": "user",
        "content": [
            {"type": "text", "text": _PROBE_QUESTION},
            {"type": "image_url", "image_url": {"url": data_url}},
        ],
    }]


def classify_reply(content: str | None, failed: bool) -> tuple[str, str]:
    """``(result, detail)`` for one probe reply."""
    text = (content or "").strip().removeprefix("Error calling LLM:").strip()
    if failed:
        if _IMAGE_REJECTION.search(text):
            return NO, f"The provider refused the image: {text[:200]}"
        return UNKNOWN, f"The check could not finish: {text[:200] or 'no reply'}"
    if _PROBE_COLOUR in text.lower():
        return YES, "Named the colour of a test image."
    return NO, "Answered without seeing the test image."


async def probe_image_input(server_id: str, pmid: str) -> tuple[str, str]:
    """Send the test image to one External model and classify its answer."""
    from infra.db_async import run_in_db
    from llm.external_provider import build_external_params
    from llm.router import chat_completion

    try:
        params = await run_in_db(build_external_params, server_id, pmid)
        response = await asyncio.wait_for(
            chat_completion(
                probe_messages(),
                model=params["model"],
                api_key=params["api_key"],
                api_base=params["api_base"],
                retry_transient_errors=False,
            ),
            timeout=_PROBE_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        return UNKNOWN, f"The check timed out after {_PROBE_TIMEOUT_S}s."
    except Exception as exc:  # the answer is "unknown", never a crashed discovery
        return classify_reply(str(exc), failed=True)
    return classify_reply(response.content, failed=response.finish_reason == "error")


def _rows(db, server_id: str) -> list:
    return db.execute(
        "SELECT id, litellm_model, metadata_json FROM models "
        "WHERE provider = ? AND litellm_model LIKE ? AND available = 1",
        (PROVIDER, f"{PREFIX}{server_id}/%"),
    ).fetchall()


def _meta(row) -> dict:
    try:
        meta = json.loads(row["metadata_json"] or "{}")
    except (TypeError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}


def image_input_state(metadata_json: str | None) -> dict | None:
    """The stored ``image_input`` record of a row, if any."""
    return _meta({"metadata_json": metadata_json}).get("image_input")


def models_to_probe(server_id: str) -> list[str]:
    """Served ids whose image support detection should settle now.

    A user's choice is never probed over; a checked row only when marked for
    a re-check. Probing a row replaces its record, which clears the mark, so
    one Test connection never probes a model twice."""
    db = get_db()
    try:
        rows = _rows(db, server_id)
    finally:
        db.close()
    ids = []
    for row in rows:
        state = _meta(row).get("image_input") or {}
        if state.get("source") == USER:
            continue
        if state and not state.get("recheck"):
            continue
        ids.append(served_model_id(row["litellm_model"]))
    return ids


def image_input_summary(server_id: str) -> str:
    """One sentence naming which of the endpoint's models accept images."""
    db = get_db()
    try:
        rows = _rows(db, server_id)
    finally:
        db.close()
    if not rows:
        return ""
    results = {
        served_model_id(r["litellm_model"]): (_meta(r).get("image_input") or {}).get("result")
        for r in rows
    }
    accepting = sorted(pmid for pmid, result in results.items() if result == YES)
    unknown = sorted(pmid for pmid, result in results.items() if result in (None, UNKNOWN))
    parts = [f"Images: {', '.join(accepting) if accepting else 'no model accepts them'}."]
    if unknown:
        parts.append(f"Not determined: {', '.join(unknown)}.")
    return " ".join(parts)


def record_image_input(
    server_id: str, pmid: str, result: str, detail: str, source: str = DETECTED,
    replace_user: bool = False,
) -> bool:
    """Store one model's image support.

    A detection never replaces a user choice unless the user asked for
    detection again (``replace_user``)."""
    litellm_model = f"{PREFIX}{server_id}/{pmid}"
    with write_transaction(None, scope="external_image_input") as db:
        row = db.execute(
            "SELECT id, metadata_json FROM models WHERE provider = ? AND litellm_model = ?",
            (PROVIDER, litellm_model),
        ).fetchone()
        if row is None:
            return False
        meta = _meta(row)
        user_set = (meta.get("image_input") or {}).get("source") == USER
        if source == DETECTED and user_set and not replace_user:
            return False
        now = _now()
        meta["image_input"] = {
            "source": source, "result": result, "detail": detail, "checked_at": now,
        }
        # Unknown keeps whatever the row already said rather than guessing.
        vision_sql = "" if result == UNKNOWN else ", supports_vision = ?"
        params: list = [json.dumps(meta)]
        if result != UNKNOWN:
            params.append(1 if result == YES else 0)
        db.execute(
            f"UPDATE models SET metadata_json = ?{vision_sql}, updated_at = ? WHERE id = ?",
            (*params, now, row["id"]),
        )
    return True


def mark_for_recheck(server_id: str) -> None:
    """Have the next detection pass probe every detected model again.

    The last answer stays visible (and in force) until the new one lands."""
    with write_transaction(None, scope="external_image_recheck") as db:
        for row in _rows(db, server_id):
            meta = _meta(row)
            state = meta.get("image_input")
            if not state or state.get("source") == USER:
                continue
            meta["image_input"] = {**state, "recheck": True}
            db.execute(
                "UPDATE models SET metadata_json = ? WHERE id = ?",
                (json.dumps(meta), row["id"]),
            )


async def detect_image_input(server_id: str) -> int:
    """Probe the models that need it, one at a time; returns how many were probed."""
    from infra.db_async import run_in_db

    pmids = await run_in_db(models_to_probe, server_id)
    for pmid in pmids:
        result, detail = await probe_image_input(server_id, pmid)
        log.info("external image probe %s/%s: %s", server_id, pmid, result)
        await run_in_db(record_image_input, server_id, pmid, result, detail)
    return len(pmids)


async def set_image_input(server_id: str, pmid: str, mode: str) -> bool:
    """Apply the user's choice: ``yes``/``no`` pins it; ``auto`` detects again now."""
    from infra.db_async import run_in_db

    if mode in (YES, NO):
        label = "Accepts images" if mode == YES else "Text only"
        return await run_in_db(record_image_input, server_id, pmid, mode, f"Set by you: {label}.", USER)
    result, detail = await probe_image_input(server_id, pmid)
    return await run_in_db(record_image_input, server_id, pmid, result, detail, DETECTED, True)
