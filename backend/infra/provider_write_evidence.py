"""Evidence at the exact provider-write boundary, independent of FIFO settlement."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class ProviderWriteEvidence:
    """Evidence emitted at the adapter's exact provider-write boundary."""

    accepted: bool = False
    ambiguous: bool = False
    proved_no_write: bool = False
    turn_owned: bool = False

    def mark_accepted(self) -> None:
        if self.ambiguous or self.proved_no_write:
            raise RuntimeError("a settled provider write cannot become accepted")
        self.accepted = True

    def mark_ambiguous(self) -> None:
        if self.accepted or self.proved_no_write:
            raise RuntimeError("a settled provider write cannot become ambiguous")
        self.ambiguous = True

    def mark_proved_no_write(self) -> None:
        if self.accepted or self.ambiguous:
            raise RuntimeError("a settled provider write cannot prove no write")
        self.proved_no_write = True

    def transfer_to_turn(self) -> None:
        """Make the admitted Turn the sole owner of final write settlement."""
        if self.accepted or self.ambiguous or self.proved_no_write:
            raise RuntimeError("a settled provider write cannot change owner")
        self.turn_owned = True
