"""Process-local admission fence for repository policy transitions."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import threading


class RepoPolicyTransitionBusy(RuntimeError):
    """A repository effect or policy transition cannot start safely."""


class _RepoPolicyTransitionCoordinator:
    """One atomic owner for in-process repo effects and policy transitions."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active_effects = 0
        self._transition_active = False

    @contextmanager
    def admit_effect(self) -> Iterator[None]:
        with self._lock:
            if self._transition_active:
                raise RepoPolicyTransitionBusy(
                    "Repository tool effect is blocked by an active "
                    "policy transition."
                )
            self._active_effects += 1
        try:
            yield
        finally:
            with self._lock:
                self._active_effects -= 1

    @contextmanager
    def transition(
        self,
        *,
        active_process_count: Callable[[], int],
    ) -> Iterator[None]:
        with self._lock:
            if self._transition_active:
                raise RepoPolicyTransitionBusy(
                    "Repository policy transition is already in progress."
                )
            if self._active_effects:
                raise RepoPolicyTransitionBusy(
                    "Repository policy transition is blocked by an active "
                    "tool effect."
                )
            try:
                process_count = active_process_count()
            except Exception as exc:
                raise RepoPolicyTransitionBusy(
                    "Owned process activity could not be verified."
                ) from exc
            if (
                isinstance(process_count, bool)
                or not isinstance(process_count, int)
                or process_count < 0
            ):
                raise RepoPolicyTransitionBusy(
                    "Owned process activity could not be verified."
                )
            if process_count:
                raise RepoPolicyTransitionBusy(
                    "Repository policy transition is blocked by an active "
                    "owned process."
                )
            self._transition_active = True
        try:
            yield
        finally:
            with self._lock:
                self._transition_active = False


_COORDINATOR = _RepoPolicyTransitionCoordinator()


@contextmanager
def admit_repo_effect() -> Iterator[None]:
    """Admit one tool effect unless it conflicts with a repo transition."""
    with _COORDINATOR.admit_effect():
        yield


@contextmanager
def repo_policy_transition(
    *,
    active_process_count: Callable[[], int],
) -> Iterator[None]:
    """Quarantine durable repo policy changes from in-process effects."""
    with _COORDINATOR.transition(active_process_count=active_process_count):
        yield
