"""Durable publication evidence carried by the document's pending journal."""
from __future__ import annotations

import base64
from dataclasses import asdict, dataclass
import json
from pathlib import Path

from registry.shared_principals import WriteAuthority
from registry.shared_target import SharedTarget


@dataclass(frozen=True)
class WriteActor:
    """Server-issued actor context, frozen when a lane operation is enqueued."""

    thread_id: str
    project_id: str | None
    direct_user: bool = False
    description: str | None = None
    origin: dict | None = None
    user_id: str | None = None
    #: The provider call behind an agent write, which its streamed preview was keyed by.
    call_id: str | None = None
    #: The write applies suggestions the person accepted, so its words are the helpers', not the person's edit.
    suggestion_accept: bool = False


@dataclass(frozen=True)
class Publication:
    """Actor and policy captured before rename, with the prior promoted mirror."""

    actor: str
    origin: dict | None
    description: str | None
    prior_mirror: bytes
    decision: dict
    ranges: dict[str, tuple[int, int]] | None = None
    suggestion_accept: bool = False

    @staticmethod
    def decision_for(path: Path, shared: SharedTarget | None, authority: WriteAuthority | None = None) -> dict:
        """Return the stored decision: a shared target record, or a plain write and its ownership decision."""
        if shared is None:
            return {"path": str(path), "plain": True, "authority": authority.to_record() if authority else None}
        return shared.to_record()

    def authority(self) -> WriteAuthority | None:
        """Return the ownership decision made for this write before it landed."""
        shared = self.shared_target()
        if shared is not None:
            return shared.authority
        return WriteAuthority.from_record(self.decision.get("authority"))

    def shared_target(self) -> SharedTarget | None:
        """Restore the shared target this write was decided against, if any."""
        if self.decision.get("plain"):
            return None
        return SharedTarget.from_record(self.decision)

    def target_path(self) -> Path:
        """Return the file this publication registers."""
        return Path(self.decision["path"])

    def encode(self) -> str:
        """Encode evidence without consulting files or the application database."""
        data = asdict(self)
        data["prior_mirror"] = base64.b64encode(self.prior_mirror).decode("ascii")
        return json.dumps(data)

    @classmethod
    def decode(cls, value: str | None) -> Publication | None:
        """Restore exact evidence, including CRDT identities in the prior mirror."""
        if value is None:
            return None
        data = json.loads(value)
        data["prior_mirror"] = base64.b64decode(data["prior_mirror"], validate=True)
        return cls(**data)


@dataclass(frozen=True)
class OwedPublication:
    """One promoted mirror awaiting idempotent application registration."""

    seq: int
    publication: Publication
    mirror_state: bytes
    result_hash: str
