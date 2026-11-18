"""Bounded process-local cache of pure source-to-destination span mappings."""
from collections import OrderedDict
from hashlib import sha256
from threading import Lock

# Conservative accounting assumes four bytes per character plus entry overhead.
MAX_BYTES = 64 * 1024 * 1024
MAX_ENTRIES = 128
_cache = OrderedDict()
_bytes = 0
_lock = Lock()


def destination_digest(text: str) -> bytes:
    return sha256(text.encode('utf-8', errors='surrogatepass')).digest()


def map_spans(snapshot_id, old, new, spans, digest, calculate):
    """Reuse a mapping only after checking both full texts and all input ranges."""
    if snapshot_id is None:
        return calculate(old, new, spans)
    key = (snapshot_id, tuple(spans), digest)
    with _lock:
        entry = _cache.get(key)
        if entry is not None and entry[0] == old and entry[1] == new:
            _cache.move_to_end(key)
            return list(entry[2])
    result = calculate(old, new, spans)
    cost = 4 * (len(old) + len(new)) + 256 * (len(spans) + 1)
    if cost > MAX_BYTES:
        return result
    global _bytes
    with _lock:
        prior = _cache.pop(key, None)
        if prior is not None:
            _bytes -= prior[3]
        _cache[key] = (old, new, tuple(result), cost)
        _bytes += cost
        while _bytes > MAX_BYTES or len(_cache) > MAX_ENTRIES:
            _, evicted = _cache.popitem(last=False)
            _bytes -= evicted[3]
    return result
