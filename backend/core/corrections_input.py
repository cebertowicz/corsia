"""The classifier input policy: how one owner message and its thread become model input.

Inference must see exactly the shape the v2 model was trained on, so every rule here
mirrors how its training rows were built:

- context is read from the 40 newest earlier ``user``/``assistant`` rows of the thread
  (newest first by ``created_at`` then ``id``); system wrappers and routed/sent-by-agent
  deliveries are skipped by the rule that defines an owner message (``is_wrapper_or_delivery``),
  limited to the delivery keys training skipped, and the first six others kept; each is stripped, cut to its last
  1,500 characters and masked; the newest are kept while their total stays within 8,000
  characters; they run oldest first, labelled ``Agent`` or ``User``;
- the owner message is masked and tokenized after the context, and the token sequence is
  truncated from the left with ``[CLS]`` and ``[SEP]`` added after truncation (Hugging Face
  ``truncation_side='left'``), to ``max(1,024, owner tokens + 2)`` tokens, bounded here at
  2,048.

Character caps also bound the work per call: nothing longer than these caps is pickled to
the model process or tokenized. Context is at most 6 x 1,500 characters by the training
rule itself. The owner message is cut to its last ``OWNER_MESSAGE_CHARS``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from core.corrections_recording import is_wrapper_or_delivery

MAX_OWNER_TOKENS = 1024
MAX_INPUT_TOKENS = 2048
CONTEXT_ROWS = 40
CONTEXT_MESSAGES = 6
CONTEXT_MESSAGE_CHARS = 1500
CONTEXT_TOTAL_CHARS = 8000
# 16 characters per token over the 2,048-token window (32,768 characters). Prose and code
# average about four characters per token with this tokenizer, so a message cut here still
# fills the window with a 4x margin and the kept tail is what left truncation keeps anyway.
# Only long runs of one repeated character pack more than 16 characters into a token; such
# a message keeps fewer than 2,048 tokens. Tokenizing 32,768 characters takes milliseconds.
OWNER_MESSAGE_CHARS = 16 * MAX_INPUT_TOKENS
# A stored message is read as its head (for the wrapper test) and tail (for the kept text);
# both cover every non-pathological message exactly.
CONTEXT_HEAD_CHARS = 1024
CONTEXT_TAIL_CHARS = 2000
# Training skipped only these deliveries; agent-sent rows without them stayed in context.
CONTEXT_DELIVERY_KEYS = ("routed_event", "send_idempotency_key")

MASK = "[MASKED]"
_PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|$)", re.S)
_KV = re.compile(
    r"(?i)\b([A-Za-z0-9_.-]*(?:api[_-]?key|apikey|secret|token|passw(?:or)?d|passwd|pwd|credential"
    r"|auth(?:orization)?|bearer|private[_-]?key)[A-Za-z0-9_.-]*)(\s*[:=]\s*|\s+is\s+)([\"']?)([^\s\"',;]{6,})"
)
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{12,}")
_KNOWN = re.compile(
    r"\b(sk-[A-Za-z0-9_-]{16,}|sk-ant-[A-Za-z0-9_-]{10,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}"
    r"|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})"
)
_LONG = re.compile(r"\b(?=[A-Za-z0-9+/_=-]*\d)(?=[A-Za-z0-9+/_=-]*[A-Za-z])[A-Za-z0-9+/_=-]{32,}\b")
_HEX = re.compile(r"\b[0-9a-fA-F]{32,}\b")


def mask(text: str) -> str:
    """Replace credentials and long identifiers (hashes, keys) as the training rows did."""
    text = _PEM.sub(MASK, text)
    text = _KNOWN.sub(MASK, text)
    text = _BEARER.sub(lambda match: match.group(1) + " " + MASK, text)
    text = _KV.sub(lambda match: match.group(1) + match.group(2) + match.group(3) + MASK, text)
    text = _HEX.sub(MASK, text)
    return _LONG.sub(MASK, text)


def shape_context(rows: Iterable[tuple[str, str, str, Any]]) -> tuple[tuple[str, str], ...]:
    """Build model context from newest-first ``(role, head, tail, metadata)`` message rows."""
    kept: list[tuple[str, str]] = []
    total = 0
    for role, head, tail, metadata in rows:
        lead = (head or "").lstrip()
        if not lead or is_wrapper_or_delivery(lead, metadata, CONTEXT_DELIVERY_KEYS):
            continue
        text = mask((tail or "").strip()[-CONTEXT_MESSAGE_CHARS:])
        if total + len(text) > CONTEXT_TOTAL_CHARS:
            break
        kept.append(("Agent" if role == "assistant" else "User", text))
        total += len(text)
        if len(kept) == CONTEXT_MESSAGES:
            break
    return tuple(reversed(kept))


def owner_text(owner: str) -> str:
    """The masked owner message, cut to the tail the token window can hold."""
    return mask(owner[-OWNER_MESSAGE_CHARS:])


def build_text(context: list[tuple[str, str]], owner_message: str) -> str:
    """The exact v2 training format: context first, owner message last."""
    parts = [f"[{role}] {text}" for role, text in context]
    parts.append(f"[OWNER] {owner_message}")
    return "\n".join(parts)


def tokenize(tokenizer: Any, context: list[tuple[str, str]], owner: str) -> tuple[list[int], bool, int]:
    """Return ids, whether they were truncated, and the untruncated length.

    Truncation is configured on every call, never inherited from the saved tokenizer file.
    """
    tokenizer.no_padding()
    tokenizer.no_truncation()
    owner_tokens = len(tokenizer.encode(f"[OWNER] {owner}", add_special_tokens=False).ids)
    cap = min(MAX_INPUT_TOKENS, max(MAX_OWNER_TOKENS, owner_tokens + tokenizer.num_special_tokens_to_add(False)))
    text = build_text(context, owner)
    original = len(tokenizer.encode(text, add_special_tokens=True).ids)
    tokenizer.enable_truncation(cap, stride=0, strategy="longest_first", direction="left")
    return tokenizer.encode(text, add_special_tokens=True).ids, original > cap, original
