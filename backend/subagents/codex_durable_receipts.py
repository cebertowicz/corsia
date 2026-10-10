"""Append-only receipt parsing owned by the session's managed tool task."""

from __future__ import annotations

import json
import os
from pathlib import Path


class ReceiptArtifactError(ValueError):
    """The exact local rollout cannot prove this tool's current usage."""


def _append_unique(values: list, value) -> None:
    """Append ``value`` if it is not already present."""
    if value not in values:
        values.append(value)


def _response_item_ids(payload: dict) -> list[str]:
    """Return every provider identifier that can own this rollout item."""
    ids: list[str] = []
    for field in ("call_id", "id"):
        value = payload.get(field)
        if isinstance(value, str) and value:
            _append_unique(ids, value)
    return ids


def _counts(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        raise ReceiptArtifactError("receipt has no token counts")
    result = {}
    for source, dest in (
        ("input_tokens", "inputTokens"), ("output_tokens", "outputTokens"),
        ("cached_input_tokens", "cachedInputTokens"),
        ("reasoning_output_tokens", "reasoningOutputTokens"),
    ):
        count = value.get(source, 0 if source not in {"input_tokens", "output_tokens"} else None)
        if type(count) is not int or count < 0:
            raise ReceiptArtifactError(f"invalid receipt {source}")
        result[dest] = count
    return result


def normalize_receipt(payload: dict) -> dict[str, dict[str, int]]:
    """Convert durable response occupancy and thread spend to the wire shape."""
    return {"last": _counts(payload.get("usage")),
            "total": _counts(payload.get("thread_token_usage"))}


def usage_identity(usage: dict) -> tuple:
    """One identity shared by durable receipts and later stdout totals."""
    total = usage.get("total") or {}
    return tuple(total.get(key, 0) for key in (
        "inputTokens", "outputTokens", "cachedInputTokens", "reasoningOutputTokens",
    ))


class RolloutReceiptCursor:
    """Keep complete and partial appended bytes across serial tool callbacks."""

    CHUNK_BYTES = 64 * 1024

    def __init__(self, path: str | Path, thread_id: str, *, fresh: bool = False) -> None:
        self.path, self.thread_id = Path(path), thread_id
        self.offset = 0
        self.identity: tuple[int, int] | None = None
        self.buffer = b""
        self.turn_id: str | None = None
        self.group: list[str] = []
        self.receipts: dict[tuple[str, str], dict] = {}
        self.require_metadata = False
        try:
            with self.path.open("rb") as stream:
                stat = os.fstat(stream.fileno())
                self.identity = (stat.st_dev, stat.st_ino)
                self.offset = stat.st_size
                if self.offset:
                    stream.seek(self.offset - 1)
                    if stream.read(1) != b"\n":
                        raise ReceiptArtifactError("pre-Turn rollout must end at a complete line")
        except FileNotFoundError as error:
            if not fresh:
                raise ReceiptArtifactError("missing resumed rollout") from error
            # Only a freshly returned thread/start path may be created lazily.
            self.require_metadata = True
        except OSError as error:
            raise ReceiptArtifactError(f"cannot open rollout: {error}") from error

    def _read_chunk(self, *, read: bool) -> None:
        try:
            with self.path.open("rb") as stream:
                stat = os.fstat(stream.fileno())
                identity = (stat.st_dev, stat.st_ino)
                if self.identity is not None and identity != self.identity:
                    raise ReceiptArtifactError("rollout was replaced")
                if stat.st_size < self.offset:
                    raise ReceiptArtifactError("rollout was truncated")
                self.identity = identity
                if read:
                    stream.seek(self.offset)
                    chunk = stream.read(self.CHUNK_BYTES)
                    self.offset += len(chunk)
                    self.buffer += chunk
        except OSError as error:
            # At a tool RPC the fresh rollout must already have been created.
            raise ReceiptArtifactError(f"cannot read rollout: {error}") from error

    def read_receipt(self, turn_id: str, call_id: str) -> dict | None:
        """Read at most one bounded chunk, caching every call in a response."""
        key = (turn_id, call_id)
        # Validate even cached members, but consume buffered lines before
        # reading another chunk and do not accumulate bytes for cached calls.
        self._read_chunk(read=key not in self.receipts and b"\n" not in self.buffer)
        if key in self.receipts:
            return self.receipts[key]
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            try:
                record = json.loads(line)
                payload = record["payload"]
                if not isinstance(payload, dict):
                    raise ValueError("non-object payload")
                kind = record["type"]
            except (ValueError, KeyError, TypeError) as error:
                raise ReceiptArtifactError("malformed rollout record") from error
            if self.require_metadata:
                if kind != "session_meta" or payload.get("id") != self.thread_id:
                    raise ReceiptArtifactError("fresh rollout session identity mismatch")
                self.require_metadata = False
            if kind == "event_msg" and payload.get("type") == "task_started":
                self.turn_id, self.group = payload.get("turn_id"), []
            elif kind == "response_item" and payload.get("type") in {"function_call", "custom_tool_call"}:
                ids = _response_item_ids(payload)
                if not ids:
                    raise ReceiptArtifactError("provider call has no direct call ID")
                for direct_id in ids:
                    _append_unique(self.group, direct_id)
            elif kind == "token_usage_record":
                if (not self.turn_id or payload.get("thread_id") != self.thread_id
                        or payload.get("turn_id") != self.turn_id
                        or not payload.get("response_id")):
                    raise ReceiptArtifactError("receipt provider identity mismatch")
                usage = normalize_receipt(payload)
                for direct_id in self.group:
                    self.receipts[(self.turn_id, direct_id)] = usage
                self.group = []
                if key in self.receipts:
                    return self.receipts[key]
        return None
