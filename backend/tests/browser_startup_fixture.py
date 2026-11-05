"""Minimal managed-process owner surface used by backend lifespan tests."""
from types import SimpleNamespace

from infra.config import RUNTIME_DIR


class ProcessOwnerTestContract:
    """Minimal managed-process owner surface required by lifespan startup."""

    process_dir = RUNTIME_DIR / "managed-processes"

    def browser_sessions(self):
        return SimpleNamespace(socket_dir=RUNTIME_DIR / "fixture-browser")

    def configure_transition_callback(self, _callback) -> None:
        return None

    def configure_long_running_capacity_callback(self, _callback) -> None:
        return None
