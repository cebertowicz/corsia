"""Token estimation and history budget trimming."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache

from loguru import logger

from infra import database as _database


_LONG_UNBROKEN_RUN_CHARS = 80

# Asset identities from the locked tiktoken constructors. Validate before the
# dependency loader, whose missing/corrupt-cache behavior otherwise downloads.
_ENCODING_ASSETS = {
    "cl100k_base": "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7",
    "o200k_base": "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d",
}


def _local_encoding_assets(name: str) -> bool:
    """Qualify LiteLLM's existing cache without importing its network-capable loader."""
    import hashlib
    import os
    import sys
    from importlib.util import find_spec
    from pathlib import Path
    from tiktoken.load import check_hash

    if "litellm.litellm_core_utils.default_encoding" in sys.modules:
        cache = os.environ.get("TIKTOKEN_CACHE_DIR", "")
    else:
        package = find_spec("litellm")
        locations = package.submodule_search_locations if package else None
        if not locations:
            logger.warning("Token counting has no local LiteLLM assets; using labelled estimates")
            return False
        cache = os.environ.get("CUSTOM_TIKTOKEN_CACHE_DIR") or str(
            Path(next(iter(locations))) / "litellm_core_utils" / "tokenizers"
        )
    try:
        # Importing the default owner loads cl100k even when o200k is requested.
        for encoding_name in {"cl100k_base", name}:
            url = f"https://openaipublic.blob.core.windows.net/encodings/{encoding_name}.tiktoken"
            key = hashlib.sha1(url.encode()).hexdigest()
            if not cache or not check_hash((Path(cache) / key).read_bytes(), _ENCODING_ASSETS[encoding_name]):
                logger.warning("Token counting asset {} is unavailable or corrupt; using labelled estimates", encoding_name)
                return False
    except OSError as exc:
        logger.warning("Token counting assets cannot be read; using labelled estimates: {}", exc)
        return False
    return True


@dataclass(frozen=True, slots=True)
class TokenMeasurement:
    """A tokenizer count or labelled estimate; never a provider usage receipt."""

    tokens: int
    upper_bound: int | None
    model: str
    tokenizer_identity: str


@lru_cache(maxsize=32)
def _model_encoding(model: str):
    """Select only encodings bundled by the pinned dependency, without HF lookup."""
    import tiktoken

    provider, separator, identifier = model.partition("/")
    if separator and provider in {"openai", "azure", "codex"}:
        model = identifier
    try:
        name = tiktoken.encoding_name_for_model(model)
    except KeyError:
        return None
    if name not in _ENCODING_ASSETS or not _local_encoding_assets(name):
        return None
    # LiteLLM owns these offline assets and its cache-directory initialization.
    from litellm.litellm_core_utils.default_encoding import encoding

    return encoding if name == encoding.name else tiktoken.get_encoding(name)


def measure_text(text: str, *, model: str) -> TokenMeasurement:
    """Count model-matched text; unknown aliases retain the labelled estimate."""
    encoding = _model_encoding(model)
    if encoding is None:
        return TokenMeasurement(_estimate_tokens(text), None, model, "estimated:characters")
    from core.token_count_cache import TOKEN_COUNTS

    count = TOKEN_COUNTS.count(encoding, text)
    return TokenMeasurement(count, count, model, f"tiktoken:{encoding.name}")


def measure_request(payload: object, *, target) -> TokenMeasurement:
    """Measure frozen rendered input without changing framing, previews or billing."""
    model = target.expected_reported_model or target.transport_model
    if isinstance(payload, dict) and "messages" in payload:
        tokens = _estimate_messages_tokens(payload["messages"], model=model)
        tools = payload.get("tools")
        if tools:
            tokens += measure_text(json.dumps(
                tools, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            ), model=model).tokens + 4
    else:
        rendered = payload if isinstance(payload, str) else json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        )
        tokens = measure_text(rendered, model=model).tokens
    return TokenMeasurement(
        tokens, None, target.catalog_id, request_count_identity(model),
    )


def request_count_identity(model: str | None) -> str:
    """Describe request estimates without claiming exact provider framing/media."""
    encoding = _model_encoding(model) if model else None
    return (
        f"estimated:framing+tiktoken:{encoding.name}"
        if encoding else "estimated:characters"
    )


def _text_tokens(text: str, model: str | None) -> int:
    if not text:
        return 0
    return measure_text(text, model=model).tokens if model else _estimate_tokens(text)


def get_db():
    """Resolve the active database seam at call time."""
    return _database.get_db()


def _long_unbroken_token_floor(text: str) -> int:
    """Conservative floor for dense/base64/minified strings.

    The normal chars-per-token heuristic undercounts long no-whitespace runs,
    which can let preflight miss a compaction boundary that the provider later
    hits. Counting those runs at roughly one char per token is intentionally
    conservative for admission control.
    """
    # str.split() with no argument splits on exactly the characters
    # str.isspace() accepts, so each piece is one maximal non-space run.
    return sum(
        length for length in map(len, text.split())
        if length >= _LONG_UNBROKEN_RUN_CHARS
    )


def _tool_definition_token_floor(value: object) -> int | None:
    """Return the dense-string floor for one trusted tool-definition array."""
    if not isinstance(value, list) or not value:
        return None
    if not all(
        isinstance(item, dict)
        and item.get("type") == "function"
        and isinstance(item.get("function"), dict)
        for item in value
    ):
        return None
    total = 0
    pending = list(value)
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            total += _long_unbroken_token_floor(item)
        elif isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
    return total


def _estimate_tokens(text: str | None) -> int:
    """Estimate token count for a string (~3 chars/token, conservative)."""
    if not text:
        return 0
    baseline = len(text) // 3
    if isinstance(text, str) and text[0] == "[" and text[-1] == "]":
        try:
            structured = json.loads(text)
        except (TypeError, ValueError, RecursionError):
            pass
        else:
            # Tool schemas are trusted structured input. Their minified syntax
            # is one whitespace-free run but does not tokenize like base64.
            # Keep the dense floor for any string value inside the schema.
            structured_floor = _tool_definition_token_floor(structured)
            if structured_floor is not None:
                return max(baseline, structured_floor)
    return max(baseline, _long_unbroken_token_floor(text))


def _estimate_content_part_tokens(part: object, *, model: str | None = None) -> int:
    """Estimate model-visible content in OpenAI, Codex, and Claude blocks."""
    if isinstance(part, str):
        return _text_tokens(part, model)
    if not isinstance(part, dict):
        return 0

    part_type = part.get("type")
    if part_type in {"text", "input_text", "output_text"}:
        return _text_tokens(part.get("text", ""), model)
    if part_type in {"image_url", "input_image", "image"}:
        return 1000  # rough image token estimate
    if part_type == "thinking":
        return _text_tokens(part.get("thinking", ""), model)
    if part_type == "tool_use":
        tool_input = json.dumps(
            part.get("input", {}),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return (
            4
            + _text_tokens(part.get("name", ""), model)
            + _text_tokens(tool_input, model)
        )
    if part_type == "tool_result":
        content = part.get("content", "")
        if isinstance(content, list):
            return 4 + sum(_estimate_content_part_tokens(item, model=model) for item in content)
        return 4 + _estimate_content_part_tokens(content, model=model)
    return 0


def estimate_media_projection_tokens(media_projection: object | None) -> int:
    """Reserve the existing image-token estimate for validated fresh media."""
    images = getattr(media_projection, "images", ()) if media_projection else ()
    image_tokens = _estimate_content_part_tokens({"type": "image_url"})
    return len(images) * image_tokens


def _estimate_message_tokens(msg: dict, *, model: str | None = None) -> int:
    """Estimate tokens for a single LLM-format message."""
    tokens = 4  # role overhead

    content = msg.get("content")
    if isinstance(content, str):
        tokens += _text_tokens(content, model)
    elif isinstance(content, list):
        tokens += sum(_estimate_content_part_tokens(part, model=model) for part in content)

    reasoning_content = msg.get("reasoning_content")
    if isinstance(reasoning_content, str):
        tokens += _text_tokens(reasoning_content, model)

    for tc in msg.get("tool_calls", []):
        tokens += 4  # tool_call overhead
        func = tc.get("function", {})
        tokens += _text_tokens(func.get("name", ""), model)
        tokens += _text_tokens(func.get("arguments", ""), model)

    return tokens


def estimate_message_tokens(msg: dict, *, model: str | None = None) -> int:
    """Estimate tokens for a single LLM-format message (public seam).

    Cross-module callers use this name; the underscore-prefixed helpers stay
    module-internal.
    """
    return _estimate_message_tokens(msg, model=model)


def _estimate_messages_tokens(messages: list[dict], *, model: str | None = None) -> int:
    """Estimate total tokens for a list of LLM-format messages."""
    return sum(_estimate_message_tokens(m, model=model) for m in messages)


from infra.constants import (
    DEFAULT_MEMORY_WINDOW,
    HISTORY_LOAD_FRACTION as _HISTORY_LOAD_FRACTION,
    AVG_TOKENS_QA as _AVG_TOKENS_QA,
    AVG_TOKENS_TOOL_HEAVY as _AVG_TOKENS_TOOL_HEAVY,
    TOOL_HEAVY_THRESHOLD as _TOOL_HEAVY_THRESHOLD,
    WINDOW_MIN as _WINDOW_MIN,
    WINDOW_MAX as _WINDOW_MAX,
)


_CONTEXT_UNSET = object()


def compute_memory_window(
    model: str,
    thread_id: str,
    fallback: int = DEFAULT_MEMORY_WINDOW,
    *,
    context_window: int | None | object = _CONTEXT_UNSET,
) -> int:
    """Compute a context-aware memory window (message count) for a thread.

    Derives the window from:
    1. The model's context window size (larger context → more messages)
    2. Thread tool-heaviness (tool-heavy → fewer messages, each costs more tokens)

    Falls back to `fallback` (default 250) if model context is unknown.

    Args:
        model: LiteLLM model identifier for the current turn.
        thread_id: DB thread ID — used to sample recent messages for tool ratio.
        fallback: Safe default if model context is unknown.

    Returns:
        Recommended message count limit for rebuild_messages_from_db().
    """
    if context_window is _CONTEXT_UNSET:
        try:
            from llm.capabilities import get_model_context_window
            ctx_window = get_model_context_window(model)
        except Exception:
            ctx_window = 0
    else:
        ctx_window = int(context_window or 0)

    if not ctx_window:
        logger.debug(f"compute_memory_window: unknown context for {model}, using fallback={fallback}")
        return fallback

    tool_ratio = _sample_tool_ratio(thread_id, sample_size=50)

    if tool_ratio >= _TOOL_HEAVY_THRESHOLD:
        avg_tokens = _AVG_TOKENS_TOOL_HEAVY
        session_type = "tool-heavy"
    else:
        blend = tool_ratio / _TOOL_HEAVY_THRESHOLD
        avg_tokens = int(_AVG_TOKENS_QA + blend * (_AVG_TOKENS_TOOL_HEAVY - _AVG_TOKENS_QA))
        session_type = "qa" if tool_ratio < 0.1 else "mixed"

    history_token_budget = int(ctx_window * _HISTORY_LOAD_FRACTION)
    window = max(_WINDOW_MIN, min(_WINDOW_MAX, history_token_budget // avg_tokens))

    logger.debug(
        f"compute_memory_window: model={model}, ctx={ctx_window}, "
        f"tool_ratio={tool_ratio:.2f} ({session_type}), "
        f"avg_tokens/msg={avg_tokens}, window={window}"
    )
    return window


def _sample_tool_ratio(thread_id: str, sample_size: int = 50) -> float:
    """Return the fraction of recent messages that are tool messages.

    Samples the most recent `sample_size` messages from the DB.
    Returns 0.0 on any error (safe default → Q&A mode).
    """
    try:
        db = get_db()
        rows = db.execute(
            "SELECT role FROM messages WHERE thread_id = ? "
            "AND delivery_state = 'delivered' ORDER BY created_at DESC LIMIT ?",
            (thread_id, sample_size),
        ).fetchall()
        db.close()
        if not rows:
            return 0.0
        tool_count = sum(1 for r in rows if r["role"] == "tool")
        return tool_count / len(rows)
    except Exception as e:
        from loguru import logger
        logger.warning(f"tool-ratio DB lookup failed for {thread_id}; defaulting to 0.0: {e}")
        return 0.0
