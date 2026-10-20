"""Thread auto-naming on the thread's own model.

The title call runs through the shared isolated structured-inference seam, so
it works on every harness the thread can use (Native, Claude Code, Codex) with
no session, tools, or transcript beyond the quoted recent messages.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

TITLE_MAX_OUTPUT_TOKENS = 1024
THREAD_TITLE_MAX_CHARS = 80
TITLE_TIMEOUT_SECONDS = 60.0
_TITLE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"title": {"type": "string"}},
    "required": ["title"],
    "additionalProperties": False,
}
_TITLE_SYSTEM_PROMPT = (
    "You name conversations. Reply with one JSON object and nothing else, "
    'shaped exactly {"title": "<3-6 word title>"}. The title has no quotes, '
    "no trailing punctuation, and no explanation."
)


def _clean_title(raw: str) -> str | None:
    """Strip reasoning/thinking content, quotes, punctuation from raw LLM title."""
    from llm.think_tags import strip_think_tags
    title = raw.strip()

    title = strip_think_tags(title) or ""

    lines = [l.strip() for l in title.splitlines() if l.strip()]
    if len(lines) > 3:
        title = lines[-1]
    elif lines:
        title = lines[0]

    title = title.strip('"').strip("'").strip(".")
    if title.lower().startswith("title:"):
        title = title[6:].strip()
    if title.lower().startswith("response:"):
        title = title[9:].strip()
    reject_patterns = ["thinking process", "analyze the", "step 1", "let me", "i need to"]
    if any(p in title.lower() for p in reject_patterns):
        return None
    return title[:THREAD_TITLE_MAX_CHARS] if title else None


def _title_object(raw: object) -> object:
    """Return the structured object a harness reply carries.

    Claude Code and Codex hand the seam a parsed object; Native hands it the
    model's raw content string, which may wrap the JSON in reasoning tags or a
    code fence. Anything that is not exactly one JSON object after that raises,
    so truncated or off-format output never becomes a title.
    """
    if not isinstance(raw, str):
        return raw
    from llm.isolated_structured import _strict_json_loads
    from llm.think_tags import strip_think_tags

    text = (strip_think_tags(raw) or "").strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    return _strict_json_loads(text)


def _validate_title_payload(raw: object) -> str:
    raw = _title_object(raw)
    if not isinstance(raw, dict) or set(raw) != {"title"}:
        raise ValueError("title result must be an object with only 'title'")
    title = raw["title"]
    if not isinstance(title, str) or not title.strip():
        raise ValueError("title result must be a non-empty string")
    return title


def _resolve_title_target(model: str, thread_id: str | None):
    """Resolve the thread model's exact target, through the thread's resolver, and its harness binary."""
    from core.checkpoint_render import checkpoint_binary
    from infra.database import get_db
    from llm.resolution import resolve_thread_target

    db = get_db()
    try:
        target = resolve_thread_target(model, thread_id=thread_id, db=db)
        return target, checkpoint_binary(target, db)
    finally:
        db.close()


def _title_reasoning_effort(target) -> str | None:
    """``"none"`` when the target's own model reports it can switch reasoning off.

    A short title does not need reasoning, and a reasoning model can spend the
    whole output cap thinking and return nothing. The switch is only asked for
    on a Native model that reports one (a thinking toggle, or a level list that
    includes ``none``); every other target keeps its own default.
    """
    if target.harness != "native":
        return None
    from llm.reasoning_config import reasoning_config_for_model

    config = reasoning_config_for_model(target.catalog_id) or {}
    kind = config.get("type")
    if kind == "boolean" or (kind == "effort_levels" and "none" in (config.get("values") or [])):
        return "none"
    return None


async def _call_title_llm(prompt: str, model: str, *, thread_id: str | None = None) -> str | None:
    """Generate a title with one fresh isolated call on ``model``."""
    from infra.db_async import run_in_db
    from llm.isolated_structured import invoke_isolated_structured

    try:
        target, binary = await run_in_db(_resolve_title_target, model, thread_id)
        if not target.available:
            # Keep the current title; never pick another model for this thread's history.
            logger.info("Title generation skipped: model={} reason={}", model, target.availability_reason)
            return None
        raw = await invoke_isolated_structured(
            target,
            system_prompt=_TITLE_SYSTEM_PROMPT,
            user_prompt=prompt,
            output_schema=_TITLE_SCHEMA,
            validator=_validate_title_payload,
            max_output_tokens=TITLE_MAX_OUTPUT_TOKENS,
            reasoning_effort=await run_in_db(_title_reasoning_effort, target),
            timeout_seconds=TITLE_TIMEOUT_SECONDS,
            binary=binary, thread_id=thread_id,
        )
    except Exception as e:
        logger.warning("Title generation failed with {}: {}", model, e)
        return None
    return _clean_title(raw)


async def generate_thread_title(messages: list[dict], model: str, *, thread_id: str | None = None) -> str | None:
    """Generate a short thread title on the thread's own ``model``."""
    parts: list[str] = []
    for msg in messages:
        role = msg.get("role")
        content = (msg.get("content") or "").strip()
        if role not in {"user", "assistant"} or not content:
            continue
        label = "User" if role == "user" else "Assistant"
        parts.append(f"{label}: {content[:300]}")

    if not parts:
        return None

    prompt = (
        "Generate a short, descriptive title (3-6 words) for this conversation.\n\n"
        + "\n".join(parts[:8])
    )
    return await _call_title_llm(prompt, model, thread_id=thread_id)
