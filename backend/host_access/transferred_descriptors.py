"""One-shot ownership of descriptors passed to an owned process launch."""

from __future__ import annotations

from functools import partial, update_wrapper
import os
from typing import Any, Callable


class TransferredDescriptors:
    """Close transferred parent descriptors once, even if their numbers are reused."""

    def __init__(self, descriptors: tuple[int, ...]) -> None:
        self.descriptors = descriptors

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


class transfer_descriptors:
    """Transfer launch descriptors for the whole async call, including admission."""

    def __init__(self, function: Callable[..., Any]) -> None:
        self.function = function
        update_wrapper(self, function)

    def __get__(self, instance: Any, owner: type | None = None) -> Any:
        if instance is None:
            return self
        return partial(self.__call__, instance)

    async def __call__(self, instance: Any, *args: Any, inherited_fds: tuple[int, ...] = (), **kwargs: Any) -> Any:
        owned = TransferredDescriptors(inherited_fds)
        try:
            return await self.function(instance, *args, inherited_fds=owned, **kwargs)
        finally:
            owned.close()
