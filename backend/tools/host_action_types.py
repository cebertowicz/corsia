"""Values shared by host-action admission and execution."""

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any

from tools.host_access_errors import HostAccessBlocked


@dataclass(frozen=True, slots=True)
class PreparedAction:
    tool_name: str
    descriptor_json: str = field(repr=False)
    action_hash: str
    requires_elevation: bool
    lexical_path: Path | None = None
    resolved_path: Path | None = None
    host_scope: bool = False
    working_directory: Path | None = None
    working_directory_device: int | None = field(default=None, repr=False)
    working_directory_inode: int | None = field(default=None, repr=False)
    effective_timeout: int | None = None
    parameter_overrides: tuple[tuple[str, Any], ...] = ()
    literal_command: str | None = field(default=None, repr=False)
    literal_credentials: int = field(default=0, repr=False)

    def review_descriptor(self) -> dict[str, Any]:
        value = json.loads(self.descriptor_json)
        if not isinstance(value, dict):
            raise HostAccessBlocked("Host action descriptor is invalid.")
        return value


@dataclass(frozen=True, slots=True)
class ActionPermit:
    permit_id: str = field(repr=False)
    lease_id: str = field(repr=False)
    thread_id: str
    lease_revision: int
    application_hash: str = field(repr=False)
    action: PreparedAction
    external_agent: bool = field(default=False, repr=False)


@dataclass(frozen=True, slots=True)
class HostActionAuthorization:
    """Opaque registry result; only safe parameter overrides are public."""

    _permit: ActionPermit | None = field(default=None, repr=False)
    _parameter_overrides: tuple[tuple[str, Any], ...] = field(default=(), repr=False)

    @property
    def parameter_overrides(self) -> dict[str, Any]:
        return dict(self._parameter_overrides)
