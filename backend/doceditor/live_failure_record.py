"""Immutable failure provenance decoded from the canonical store record."""
from dataclasses import dataclass


@dataclass(frozen=True)
class FailureRecord:
    attempts: int
    message: str
    generation: int

    @classmethod
    def decode(cls, value: dict) -> "FailureRecord":
        """Read the persisted failure shape without inventing a new clock."""
        return cls(value["attempts"], value["message"], value["generation"])


@dataclass(frozen=True)
class SetupFailure:
    reason: str
    revision: int
