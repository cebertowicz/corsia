"""What a failed oMLX load says about memory, beside ``omlx_lifecycle.load_model``, which returns it.

oMLX's admin load route answers 500 for every failure, so only its message tells a memory
refusal apart. The words are oMLX 0.7.1rc1's (``engine_pool`` and ``exceptions``); recheck them
on each oMLX bump.
"""

from __future__ import annotations

#: A load refused by the memory free right now: the projected total over a ceiling, a model over
#: the dynamic (free-memory) ceiling, or Metal running out mid-load. A model too big for the
#: static or Metal cap isn't here, because freeing memory doesn't move those.
_NO_FREE_MEMORY = ("projected memory", "dynamic memory ceiling", "out of gpu memory")


def refused_for_free_memory(message: str) -> bool:
    """Whether a failed ``load_model``'s message says the memory free now was what fell short."""
    lowered = message.lower()
    return any(words in lowered for words in _NO_FREE_MEMORY)
