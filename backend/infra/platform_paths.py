"""Platform state paths and the stable workspace bootstrap format."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from infra.fs_durability import fsync_directory as _fsync_directory

APPLICATION_ID = "com.cebertowicz.corsia"
STATE_DIRECTORY = "state"
BOOTSTRAP_VERSION = 2


@dataclass(frozen=True, slots=True)
class WorkspaceSelection:
    """One selected workspace with identity independent of its root path."""

    workspace_id: str
    root: Path
    name: str

    def __post_init__(self) -> None:
        """Validate identity fields and canonicalize the selected root."""
        object.__setattr__(
            self,
            "workspace_id",
            _nonblank_string(self.workspace_id, "workspace id"),
        )
        object.__setattr__(self, "root", _resolved(self.root))
        object.__setattr__(
            self,
            "name",
            _nonblank_string(self.name, "workspace name"),
        )


@dataclass(frozen=True, slots=True)
class LegacyRuntimePaths:
    """A path-only bootstrap pinned to its legacy operational state."""

    workspace: WorkspaceSelection

    @property
    def state_root(self) -> Path:
        """Return the exact workspace-local state used by legacy installs."""
        return _resolved(self.workspace.root / ".corsia")


def validate_workspace_state_separation(
    workspace_root: Path,
    state_root: Path,
) -> None:
    """Reject either root containing the other root."""
    resolved_workspace = workspace_root.expanduser().resolve()
    resolved_state = state_root.expanduser().resolve()
    if (
        resolved_state == resolved_workspace
        or resolved_state.is_relative_to(resolved_workspace)
        or resolved_workspace.is_relative_to(resolved_state)
    ):
        raise ValueError(
            "CORSIA state root must be outside workspace root; "
            "workspace and state roots must not overlap: "
            f"workspace={resolved_workspace}, state={resolved_state}"
        )


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    """The selected workspace and its separate operational state root."""

    workspace: WorkspaceSelection
    state_root: Path

    def __post_init__(self) -> None:
        """Canonicalize state and reject overlapping runtime roots."""
        object.__setattr__(self, "state_root", _resolved(self.state_root))
        validate_workspace_state_separation(
            self.workspace.root,
            self.state_root,
        )

    @property
    def workspace_root(self) -> Path:
        """Return the selected workspace's current filesystem root."""
        return self.workspace.root


def default_state_root(
    *,
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
    home: Path | None = None,
    application_id: str | None = None,
) -> Path:
    """Return the configured or platform-default operational state root.

    ``CORSIA_STATE_DIR`` is an exact root override. Otherwise CORSIA keeps
    operational state in an application-specific ``state`` child beneath
    Application Support on macOS, LocalAppData on Windows, or the XDG state
    directory on other platforms (falling back to ``~/.local/state``). The
    child leaves room for application metadata beside operational state.
    """
    environment = os.environ if env is None else env
    platform_name = sys.platform if platform is None else platform
    home_dir = Path.home() if home is None else Path(home)
    app_id = (
        application_id or environment.get("CORSIA_APPLICATION_ID") or APPLICATION_ID
    )

    override = environment.get("CORSIA_STATE_DIR")
    if override:
        return _resolved(override)

    if platform_name == "darwin":
        base = home_dir / "Library" / "Application Support"
    elif platform_name.startswith("win"):
        local_app_data = environment.get("LOCALAPPDATA")
        base = (
            Path(local_app_data) if local_app_data else home_dir / "AppData" / "Local"
        )
    else:
        xdg_state = environment.get("XDG_STATE_HOME")
        base = Path(xdg_state) if xdg_state else home_dir / ".local" / "state"

    return _resolved(base / app_id / STATE_DIRECTORY)


def runtime_db_paths(state_root: Path) -> tuple[Path, Path, Path]:
    """Return the canonical operational data paths for the single instance."""
    return (
        Path(state_root) / "corsia.db",
        Path(state_root) / "analytics.duckdb",
        Path(state_root) / "uploads",
    )


def load_bootstrap(path: Path) -> RuntimePaths | LegacyRuntimePaths:
    """Load a v2 bootstrap or an explicit pre-G4 legacy selection."""
    bootstrap_path = Path(path)
    payload = json.loads(bootstrap_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid bootstrap {bootstrap_path}: expected an object")

    workspace_payload = payload.get("workspace")
    version = payload.get("version")
    if version in (None, 1) and isinstance(workspace_payload, str):
        root = _resolved(workspace_payload)
        selection = WorkspaceSelection(
            workspace_id=_legacy_workspace_id(root),
            root=root,
            name=root.name or "workspace",
        )
        return LegacyRuntimePaths(workspace=selection)

    if version != BOOTSTRAP_VERSION or not isinstance(workspace_payload, dict):
        raise ValueError(
            f"Invalid bootstrap {bootstrap_path}: expected version {BOOTSTRAP_VERSION}"
        )

    workspace_id = _required_string(
        workspace_payload,
        "id",
        bootstrap_path,
    )
    workspace_name = _required_string(
        workspace_payload,
        "name",
        bootstrap_path,
    )
    workspace_root = _required_string(
        workspace_payload,
        "root",
        bootstrap_path,
    )
    state_root = _required_string(payload, "state_root", bootstrap_path)
    return RuntimePaths(
        workspace=WorkspaceSelection(
            workspace_id=workspace_id,
            root=_resolved(workspace_root),
            name=workspace_name,
        ),
        state_root=_resolved(state_root),
    )


def write_bootstrap(
    path: Path,
    runtime_paths: RuntimePaths,
    exposure: dict[str, str] | None = None,
) -> None:
    """Atomically replace ``path`` with a complete v2 bootstrap payload.

    ``exposure`` carries the setup keys (``infra.bind_host.exposure_keys``).
    """
    bootstrap_path = Path(path)
    if not isinstance(runtime_paths, RuntimePaths):
        raise TypeError(
            "Cannot persist legacy runtime paths as a current bootstrap; "
            "migrate the workspace first"
        )
    bootstrap_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": BOOTSTRAP_VERSION,
        "workspace": {
            "id": runtime_paths.workspace.workspace_id,
            "name": runtime_paths.workspace.name,
            "root": str(runtime_paths.workspace.root),
        },
        "state_root": str(runtime_paths.state_root),
        **(exposure or {}),
    }
    encoded = json.dumps(payload, indent=2) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        dir=bootstrap_path.parent,
        prefix=f".{bootstrap_path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
            temporary.write(encoded)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, bootstrap_path)
        _fsync_directory(bootstrap_path.parent)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary_path.unlink(missing_ok=True)
        raise


def _legacy_workspace_id(root: Path) -> str:
    """Derive a repeatable identity while upgrading a legacy path-only file."""
    return str(uuid5(NAMESPACE_URL, root.as_uri()))


def _required_string(
    payload: Mapping[str, object],
    key: str,
    path: Path,
) -> str:
    """Return a required non-empty bootstrap string."""
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Invalid bootstrap {path}: missing {key!r}")
    return value


def _nonblank_string(value: object, label: str) -> str:
    """Return a trimmed, non-empty identity value."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"CORSIA {label} must be a non-empty string")
    return value.strip()


def _resolved(path: str | Path) -> Path:
    """Return a normalized absolute path without requiring it to exist."""
    return Path(path).expanduser().resolve()
