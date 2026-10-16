"""Platform state paths and the stable workspace bootstrap format."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from infra.fs_durability import fsync_directory as _fsync_directory

APPLICATION_ID = "com.cebertowicz.corsia"
# The packaged app's folder under Application Support. Held equal to the Rust
# shell's APP_FOLDER and the launcher's CORSIA_APP_FOLDER by a parity test.
APP_FOLDER = "CORSIA"
STATE_DIRECTORY = "state"
BOOTSTRAP_VERSION = 2
_UNSET = object()


def app_bundle_root(path: str | Path | None = None) -> Path | None:
    """Return the ``.app`` bundle this code runs from, or None outside one.

    App mode is a path check on the resolved module location
    (``<X>.app/Contents/Resources/backend/...``), never an environment
    variable, so a source install's environment cannot claim it.
    """
    resolved = Path(__file__ if path is None else path).resolve()
    for parent in resolved.parents:
        if (
            parent.name == "Resources"
            and parent.parent.name == "Contents"
            and parent.parent.parent.suffix == ".app"
        ):
            return parent.parent.parent
    return None


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


def app_metadata_dir(home: Path | None = None) -> Path:
    """Return the packaged app's folder, which holds ``service.json``."""
    home_dir = Path.home() if home is None else Path(home)
    return _resolved(home_dir / "Library" / "Application Support" / APP_FOLDER)


SERVICE_FILE = "service.json"
_SERVICE_FILE_KEYS = ("state_dir", "workspace", "port")


def service_file_lines(path: Path) -> list[str]:
    """Parse the app's ``service.json`` into ``key=value`` lines for shell callers.

    The file is parsed, never executed. Only ``state_dir`` and ``workspace``
    (absolute, printable paths) and ``port`` (1-65535) are honoured; anything
    else becomes a ``note=`` line that never repeats a value. The bundle's
    launcher and its lifecycle script both read the file through this one
    function.
    """

    def name(value: object) -> str:
        text = str(value)
        return text if text.isprintable() and len(text) <= 64 else "<unprintable name>"

    try:
        with open(path, "rb") as handle:
            data = json.loads(handle.read(65536).decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
    except Exception as error:
        return [f"note={SERVICE_FILE} unreadable: {name(type(error).__name__)}"]
    lines = [
        f"note={SERVICE_FILE}: ignoring unsupported key {name(key)}"
        for key in data
        if key not in _SERVICE_FILE_KEYS
    ]
    for key in ("state_dir", "workspace"):
        value = data.get(key)
        if isinstance(value, str) and value.startswith("/") and value.isprintable():
            lines.append(f"{key}={value}")
        elif key in data:
            lines.append(f"note={SERVICE_FILE}: ignoring an invalid {key} value")
    port = data.get("port")
    if isinstance(port, int) and not isinstance(port, bool) and 0 < port < 65536:
        lines.append(f"port={port}")
    elif "port" in data:
        lines.append(f"note={SERVICE_FILE}: ignoring an invalid port value")
    return lines


def create_adoption_snapshot(state_root: Path) -> Path:
    """Snapshot an adopted state root before a newer app can migrate it.

    The snapshot deliberately lives under ``backups/adopt-*``: database
    migration retention only examines sibling pre-migration files and cannot
    prune this transaction's recovery point. SQLite owns the database copy;
    every other state entry except older backups is copied with its mode.
    """
    state_root = Path(state_root).resolve()
    database = state_root / "corsia.db"
    if not database.is_file():
        raise FileNotFoundError(f"missing CORSIA database at {database}")
    snapshot = state_root / "backups" / (
        f"adopt-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex}"
    )
    snapshot.mkdir(parents=True, mode=0o700)
    snapshot.chmod(0o700)
    database_copy = snapshot / "corsia.db"
    with sqlite3.connect(database) as source, sqlite3.connect(database_copy) as destination:
        source.backup(destination)
    database_copy.chmod(database.stat().st_mode & 0o777)

    files = snapshot / "files"
    files.mkdir(mode=0o700)
    files.chmod(0o700)
    for source in state_root.iterdir():
        if source.name in {"backups", "corsia.db", "corsia.db-wal", "corsia.db-shm"}:
            continue
        destination = files / source.name
        if source.is_dir():
            shutil.copytree(source, destination, copy_function=shutil.copy2)
        else:
            shutil.copy2(source, destination)
    return snapshot


def restore_adoption_snapshot(state_root: Path, snapshot: Path) -> None:
    """Restore the full state-root snapshot after the app service is stopped."""
    state_root = Path(state_root).resolve()
    snapshot = Path(snapshot).resolve()
    database_copy = snapshot / "corsia.db"
    files = snapshot / "files"
    if not database_copy.is_file() or not files.is_dir():
        raise ValueError("adoption snapshot is incomplete")
    for name in ("corsia.db-wal", "corsia.db-shm"):
        (state_root / name).unlink(missing_ok=True)
    for entry in state_root.iterdir():
        if entry.name == "backups":
            continue
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    restoring = state_root / ".corsia.db.adoption-restore"
    restoring.unlink(missing_ok=True)
    with sqlite3.connect(database_copy) as source, sqlite3.connect(restoring) as destination:
        source.backup(destination)
    restoring.chmod(database_copy.stat().st_mode & 0o777)
    os.replace(restoring, state_root / "corsia.db")
    for source in files.iterdir():
        destination = state_root / source.name
        if source.is_dir():
            shutil.copytree(source, destination, copy_function=shutil.copy2)
        else:
            shutil.copy2(source, destination)


def default_state_root(
    *,
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
    home: Path | None = None,
    application_id: str | None = None,
    app_bundle: Path | None | object = _UNSET,
) -> Path:
    """Return the configured or platform-default operational state root.

    ``CORSIA_STATE_DIR`` is an exact root override. Otherwise the root is the
    ``state`` child of an application folder: on macOS
    ``~/Library/Application Support/<folder>/state``, where the folder is
    ``CORSIA`` for the packaged app and the application id for a source
    install, so a missed adoption check never opens the other install's data.
    Windows and other platforms use LocalAppData and the XDG state directory
    (falling back to ``~/.local/state``).
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
        bundle = app_bundle_root() if app_bundle is _UNSET else app_bundle
        folder = APP_FOLDER if bundle is not None else app_id
        return _resolved(
            home_dir / "Library" / "Application Support" / folder / STATE_DIRECTORY
        )
    elif platform_name.startswith("win"):
        local_app_data = environment.get("LOCALAPPDATA")
        base = (
            Path(local_app_data) if local_app_data else home_dir / "AppData" / "Local"
        )
    else:
        xdg_state = environment.get("XDG_STATE_HOME")
        base = Path(xdg_state) if xdg_state else home_dir / ".local" / "state"

    return _resolved(base / app_id / STATE_DIRECTORY)


# The source-install adoption transaction moves precisely these operational
# entries from a legacy workspace-local state root. Workspace projections such
# as `context/` and `skills/` are deliberately absent and remain in place.
RUNTIME_STATE_ENTRY_NAMES = (
    "corsia.db",
    "corsia.db-wal",
    "corsia.db-shm",
    "analytics.duckdb",
    "uploads",
    "backups",
    "keys",
    "devices",
    "logs",
)


def runtime_state_paths(state_root: Path) -> tuple[Path, ...]:
    """Return every moveable operational path for state-layout transitions."""
    return tuple(Path(state_root) / name for name in RUNTIME_STATE_ENTRY_NAMES)


def runtime_db_paths(state_root: Path) -> tuple[Path, Path, Path]:
    """Return the database, analytics, and upload roots used at runtime."""
    database, _wal, _shm, analytics, uploads, *_rest = runtime_state_paths(state_root)
    return database, analytics, uploads


_PREVIEW_LAYOUT_APP_METADATA = frozenset({"service.json"})


def move_preview_layout(app_dir: Path, state_root: Path) -> list[str]:
    """Move a pre-release flat app folder's state into its ``state`` child once.

    A preview app kept ``corsia.db`` directly in the app folder. When that
    database exists and the state root does not, every entry except the app's
    explicit metadata is renamed into the new state root (same volume, so each
    move is atomic). Returns the moved names; anything else returns an empty
    list untouched. The entries gather in a hidden sibling first, so an
    interrupted move is finished by the next call instead of leaving an empty
    state root.
    """
    app_dir = Path(app_dir)
    state_root = Path(state_root)
    if state_root.parent != app_dir or state_root.exists():
        return []
    staging = app_dir / f".{state_root.name}.moving"
    if not ((app_dir / "corsia.db").is_file() or staging.is_dir()):
        return []
    staging.mkdir(exist_ok=True)
    moved = []
    for source in sorted(app_dir.iterdir(), key=lambda path: path.name):
        if source.name in _PREVIEW_LAYOUT_APP_METADATA or source == staging:
            continue
        os.rename(source, staging / source.name)
        moved.append(source.name)
    os.rename(staging, state_root)
    _fsync_directory(app_dir)
    return moved


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
