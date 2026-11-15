"""Whether each External model accepts images: detected once, or set by the user.

An OpenAI- or Anthropic-compatible model list reports no input types, and some
plan endpoints list no models at all, so CORSIA asks the model: one small
solid-green image and the question of its colour, sent through the same
dispatch path a real turn uses. The answer lands on the row's
``supports_vision`` (what dispatch and the composer read), with its provenance
in ``metadata_json['image_input']``. A user's choice wins over detection and
survives rediscovery; nothing is inferred from a model's name.

Connection, Test connection and rediscovery never run image probes. The first
successful reply triggers a check for that model; Detect now also checks it.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import struct
import uuid
import zlib

from infra.database import get_db, write_transaction
from llm.external_provider import PREFIX, PROVIDER, _now, catalog_model_id, served_model_id

log = logging.getLogger(__name__)

YES, NO, UNKNOWN = "yes", "no", "unknown"
DETECTED, USER = "detected", "user"
_PROBE_TIMEOUT_S = 90
_PENDING: set[asyncio.Task] = set()
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


async def probe_image_input(server_id: str, pmid: str, *, automatic: bool = False) -> tuple[str, str]:
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
        result, detail = classify_reply(str(exc), failed=True)
        return (UNKNOWN if automatic else result), detail
    failed = response.finish_reason == "error"
    result, detail = classify_reply(response.content, failed=failed)
    return (UNKNOWN if automatic and failed else result), detail


def _rows(db, server_id: str) -> list:
    return db.execute(
        "SELECT id, litellm_model, metadata_json, enabled FROM models "
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
    parts = [f"Images: {', '.join(accepting)}."] if accepting else []
    if not accepting and not unknown:
        parts.append("Images: no model accepts them.")
    if unknown:
        label = "Not determined:" if accepting else "Images: not determined for"
        parts.append(f"{label} {', '.join(unknown)}.")
    return " ".join(parts)


def record_image_input(
    server_id: str, pmid: str, result: str, detail: str, source: str = DETECTED,
    replace_user: bool = False, claim: str | None = None,
) -> bool:
    """Store one model's image support.

    A detection never replaces a user choice unless the user asked for
    detection again (``replace_user``)."""
    model_id = catalog_model_id(server_id, pmid)
    with write_transaction(None, scope="external_image_input") as db:
        row = db.execute(
            "SELECT id, metadata_json FROM models WHERE provider = ? AND provider_model_id = ?",
            (PROVIDER, model_id),
        ).fetchone()
        if row is None:
            return False
        meta = _meta(row)
        state = meta.get("image_input") or {}
        if claim is not None and state.get("automatic_claim") != claim:
            return False
        user_set = state.get("source") == USER
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


def _needs_first_use_claim(server_id: str, pmid: str) -> bool:
    """Whether this model still needs a first-use claim, from one read-only lookup.

    Every reply on a model that is already answered, claimed, user-owned or
    absent costs this single indexed read and no write transaction: that is the
    steady state once a model has been checked. It only admits the claim; the
    recheck inside :func:`_claim_first_use` is what arbitrates concurrent
    successes."""
    db = get_db()
    try:
        row = db.execute(
            "SELECT metadata_json FROM models WHERE provider = ? AND provider_model_id = ?",
            (PROVIDER, catalog_model_id(server_id, pmid)),
        ).fetchone()
    finally:
        db.close()
    return row is not None and not _meta(row).get("image_input")


def _claim_first_use(server_id: str, pmid: str, claim: str) -> bool:
    """Persist a single claim before any outbound request, even if it fails.

    The claim asserts nothing beyond the attempt: an unknown answer, no detail,
    the attempt time and the token that fences its late result. So a check that
    never finishes leaves the honest "not determined" reading, not a claim that
    work is still running."""
    with write_transaction(None, scope="external_image_first_use") as db:
        row = db.execute(
            "SELECT id, metadata_json FROM models WHERE provider = ? AND provider_model_id = ?",
            (PROVIDER, catalog_model_id(server_id, pmid)),
        ).fetchone()
        if row is None:
            return False
        meta = _meta(row)
        if meta.get("image_input"):
            return False
        meta["image_input"] = {
            "source": DETECTED, "result": UNKNOWN, "detail": "",
            "checked_at": _now(), "automatic_claim": claim,
        }
        db.execute("UPDATE models SET metadata_json = ? WHERE id = ?", (json.dumps(meta), row["id"]))
    return True


async def _detect_first_use(server_id: str, pmid: str) -> None:
    from infra.db_async import run_in_db

    claim = uuid.uuid4().hex
    try:
        if not await run_in_db(_needs_first_use_claim, server_id, pmid):
            return
        if not await run_in_db(_claim_first_use, server_id, pmid, claim):
            return
        result, detail = await probe_image_input(server_id, pmid, automatic=True)
        committed = await run_in_db(
            record_image_input, server_id, pmid, result, detail, claim=claim,
        )
    except Exception:
        log.exception("External first-use image check failed for %s/%s", server_id, pmid)
        return
    if not committed:
        return  # A user answer or another attempt won: this result is stale, so it says nothing.
    from infra.state.ws import notify_models_changed

    try:
        notify_models_changed()
    except Exception as e:  # The answer is stored; a lost notice cannot undo it or relabel the check.
        log.warning("notify_models_changed failed after image check on %s/%s: %s", server_id, pmid, e)


def schedule_first_use_detection(server_id: str, pmid: str) -> None:
    """Run independently of the successful response, retaining the task until done."""
    task = asyncio.create_task(_detect_first_use(server_id, pmid))
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)


async def set_image_input(server_id: str, pmid: str, mode: str) -> bool:
    """Apply the user's choice: ``yes``/``no`` pins it; ``auto`` detects again now."""
    from infra.db_async import run_in_db

    if mode in (YES, NO):
        label = "Accepts images" if mode == YES else "Text only"
        return await run_in_db(record_image_input, server_id, pmid, mode, f"Set by you: {label}.", USER)
    result, detail = await probe_image_input(server_id, pmid)
    return await run_in_db(record_image_input, server_id, pmid, result, detail, DETECTED, True)
