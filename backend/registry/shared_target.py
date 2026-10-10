"""A validated shared-write target and the one record shape it is stored as."""

from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path

from infra.conductor import WORKSPACE_SCOPE
from registry.shared_principals import WriteAuthority

_PATH_FIELDS = ("path", "category_root", "index_path")
_REQUIRED_FIELDS = (*_PATH_FIELDS, "create_parent")
# Journaled by earlier versions and no longer meaningful; dropped when a record is restored.
_RETIRED_FIELDS = ("warning",)


@dataclass(frozen=True, slots=True)
class SharedTarget:
    """A validated shared target plus its ownership decision."""

    path: Path
    category_root: Path
    index_path: Path
    create_parent: bool
    scope_id: str = WORKSPACE_SCOPE
    actor_principal: str | None = None
    owner_thread_id: str | None = None
    update_trigger: str | None = None
    outside_change: bool = False
    classification: str | None = None
    authority: WriteAuthority | None = None

    def to_record(self) -> dict:
        """Return the JSON-safe record a durable journal stores for this target."""
        record = {field.name: getattr(self, field.name) for field in fields(self)}
        for key in _PATH_FIELDS:
            record[key] = str(record[key])
        record["authority"] = self.authority.to_record() if self.authority else None
        return record

    @classmethod
    def from_record(cls, record: dict) -> SharedTarget:
        """Restore a target from ``to_record`` output; unknown keys are refused."""
        values = {key: value for key, value in record.items() if key not in _RETIRED_FIELDS}
        unexpected = set(values) - {field.name for field in fields(cls)}
        if unexpected:
            raise ValueError(f"Shared target record has unexpected keys: {sorted(unexpected)}")
        missing = [key for key in _REQUIRED_FIELDS if key not in values]
        if missing:
            raise ValueError(f"Shared target record is missing keys: {missing}")
        for key in _PATH_FIELDS:
            values[key] = Path(values[key])
        values["authority"] = WriteAuthority.from_record(values.get("authority"))
        return cls(**values)
