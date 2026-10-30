"""Filter each GitHub recipient against the marker for this delivery."""
from dataclasses import dataclass

from events.resolve import Target
from events.self_caused import delivery_is_self_caused


@dataclass(frozen=True)
class SelfCausedFilter:
    """Use the same caller-specific marker check for every recipient."""

    delivery_id: str | None

    def __call__(self, target: Target, db) -> str | None:
        if target.thread and delivery_is_self_caused("github", self.delivery_id, target.thread, db):
            return "self_caused"
        return None
