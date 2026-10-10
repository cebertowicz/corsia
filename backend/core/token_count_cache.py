"""Bounded memo of tokenizer counts, keyed by encoding and content digest.

A next-turn preview counts the same history rows and context sources on every
open, and the source inventory and context preflight count the same source
text within one preview. Encoding is the dominant cost, so counts are kept per
``(encoding name, BLAKE2b-128 digest of the text)``. Keying on the content, not
a row id, means an edited row can never read a stale count: its new text is a
new key. The digest keeps the bytes of the text out of memory.

Bound: ``MAX_ENTRIES`` keys, least recently used evicted first; each entry is
a short tuple and an int, so the cache stays around a megabyte at the bound.
"""

from __future__ import annotations

from collections import OrderedDict
import hashlib
import threading
from typing import Any

MAX_ENTRIES = 8192


class TokenCountCache:
    """Thread-safe LRU of encoded lengths; encoding runs outside the lock."""

    def __init__(self, max_entries: int = MAX_ENTRIES) -> None:
        self.max_entries = max_entries
        self._entries: OrderedDict[tuple[str, bytes], int] = OrderedDict()
        self._lock = threading.Lock()

    def count(self, encoding: Any, text: str) -> int:
        digest = hashlib.blake2b(
            text.encode("utf-8", "surrogatepass"), digest_size=16,
        ).digest()
        key = (str(encoding.name), digest)
        with self._lock:
            cached = self._entries.get(key)
            if cached is not None:
                self._entries.move_to_end(key)
                return cached
        counted = len(encoding.encode(text, disallowed_special=()))
        with self._lock:
            self._entries[key] = counted
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
        return counted

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


TOKEN_COUNTS = TokenCountCache()
