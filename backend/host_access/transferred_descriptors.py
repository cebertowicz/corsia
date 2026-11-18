"""One-shot ownership of descriptors passed to an owned process launch."""

from __future__ import annotations

import os
from types import TracebackType


class TransferredDescriptors:
    """Close transferred parent descriptors once, even if their numbers are reused."""

    def __init__(self, descriptors: tuple[int, ...]) -> None:
        self.descriptors = descriptors

    def __enter__(self) -> TransferredDescriptors:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Release the parent copies; subsequent calls cannot close reused numbers."""
        descriptors, self.descriptors = self.descriptors, ()
        seen: set[int] = set()
        for descriptor in descriptors:
            if not isinstance(descriptor, int) or isinstance(descriptor, bool) or descriptor < 0 or descriptor in seen:
                continue
            seen.add(descriptor)
            try:
                os.close(descriptor)
            except OSError:
                pass
