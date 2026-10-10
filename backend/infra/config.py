"""Backend path/config constants.

This module is import-safe: no DB work and no API-key bootstrap. Workspace
selection is env/bootstrap driven; mutable operational state lives under the
platform state root while user content stays at the selected workspace root.

Import-safe also means dependency-free. ``scripts/corsia service install``
imports this module to resolve workspace and state identity on a machine where
``requirements.txt`` has never been installed, so nothing reached at import time
here may need a third-party package. ``tests/test_config_import_light.py``
holds that line.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from infra.platform_paths import (
    LegacyRuntimePaths,
    WorkspaceSelection,
    default_state_root,
    load_bootstrap,
    runtime_db_paths,
    validate_workspace_state_separation,
)

BOOTSTRAP_CONFIG_PATH = Path.home() / ".corsia" / "config.json"


def _bootstrap_config_status() -> bool | None:
    """Whether the bootstrap config exists: True/False, or None if a sandbox
    denies even a stat of it (pathlib's ``exists()`` only swallows
    ENOENT-class errnos, not EPERM)."""
    try:
        return BOOTSTRAP_CONFIG_PATH.exists()
    except PermissionError:
        return None


def _sandboxed_bootstrap_denial_error() -> RuntimeError:
    """Build the clear error for a sandbox-denied bootstrap config stat."""
    return RuntimeError(
        f"Cannot access {BOOTSTRAP_CONFIG_PATH}: permission denied. This is "
        f"expected inside a sandboxed agent environment where the real home "
        f"directory is hard-denied. Isolated runs must set both "
        f"CORSIA_WORKSPACE and CORSIA_STATE_DIR to explicit paths before "
        f"importing backend.infra.config."
    )


def _resolve_workspace() -> Path:
    """Pick the workspace path from env var or bootstrap config."""
    env = os.environ.get("CORSIA_WORKSPACE")
    if env:
        path = Path(env).expanduser().resolve()
        _guard_against_prod_in_ci_or_tests(path, source="CORSIA_WORKSPACE env")
        return path

    bootstrap_status = _bootstrap_config_status()
    if bootstrap_status is None:
        raise _sandboxed_bootstrap_denial_error()

    if bootstrap_status:
        try:
            runtime_paths = load_bootstrap(BOOTSTRAP_CONFIG_PATH)
            path = runtime_paths.workspace.root
            _guard_against_prod_in_ci_or_tests(path, source="bootstrap config")
            return path
        except Exception as e:
            raise RuntimeError(
                f"Failed to read {BOOTSTRAP_CONFIG_PATH}: {e}. "
                f"Delete it and run 'python -m backend.cli init' to recreate."
            ) from e

    raise RuntimeError(
        f"CORSIA workspace not configured. Either set CORSIA_WORKSPACE or "
        f"run 'python -m backend.cli init' to create {BOOTSTRAP_CONFIG_PATH}."
    )


def _resolve_state_root() -> Path:
    """Return the explicit, bootstrapped, or platform-default state root."""
    env_workspace = os.environ.get("CORSIA_WORKSPACE")
    state_dir_env = os.environ.get("CORSIA_STATE_DIR")
    runtime_paths = None
    bootstrap_status = _bootstrap_config_status()
    if bootstrap_status is None:
        if not state_dir_env:
            raise _sandboxed_bootstrap_denial_error()
        # Unreadable + CORSIA_STATE_DIR set: proceed exactly as the
        # no-bootstrap path below (runtime_paths stays None). The sandbox
        # itself denies the real state root, so the legacy pre-G4 cross-check
        # below is best-effort and cannot run without the file.
    elif bootstrap_status:
        try:
            runtime_paths = load_bootstrap(BOOTSTRAP_CONFIG_PATH)
        except Exception as e:
            raise RuntimeError(
                f"Failed to read {BOOTSTRAP_CONFIG_PATH}: {e}. "
                f"Delete it and run 'python -m backend.cli init' to recreate."
            ) from e

    if state_dir_env:
        state_root = default_state_root()
        if isinstance(runtime_paths, LegacyRuntimePaths):
            selected_root = (
                Path(env_workspace).expanduser().resolve()
                if env_workspace
                else runtime_paths.workspace.root
            )
            if (
                selected_root == runtime_paths.workspace.root
                and state_root != runtime_paths.state_root
            ):
                raise RuntimeError(
                    "A legacy bootstrap must keep its selected workspace on "
                    f"the exact pre-G4 state root {runtime_paths.state_root}; "
                    f"refusing CORSIA_STATE_DIR={state_root}."
                )
        _guard_against_prod_state_reuse(state_root)
        return state_root

    if env_workspace:
        selected_root = Path(env_workspace).expanduser().resolve()
        if (
            (runtime_paths is None and _is_ci_or_test())
            or (
                runtime_paths is not None
                and selected_root != runtime_paths.workspace.root
            )
        ):
            raise RuntimeError(
                "Workspace resolution requires CORSIA_STATE_DIR "
                "when CORSIA_WORKSPACE differs from the bootstrapped "
                "workspace; refusing to reuse production state."
            )

    if runtime_paths is not None:
        return runtime_paths.state_root
    return default_state_root()


def _resolve_runtime_roots() -> tuple[Path, Path]:
    """Resolve and validate the final workspace/state pair."""
    workspace = _resolve_workspace()
    state_root = _resolve_state_root()
    try:
        validate_workspace_state_separation(workspace, state_root)
    except ValueError as error:
        if not _is_exact_legacy_runtime_pair(workspace, state_root):
            raise RuntimeError(str(error)) from error
    return workspace, state_root


def _resolve_workspace_selection(workspace: Path) -> WorkspaceSelection:
    """Load one immutable workspace identity for this backend process."""
    selected_root = workspace.expanduser().resolve()
    if _bootstrap_config_status():
        bootstrapped = load_bootstrap(BOOTSTRAP_CONFIG_PATH).workspace
        if bootstrapped.root == selected_root:
            return bootstrapped
    return WorkspaceSelection(
        workspace_id=str(uuid5(NAMESPACE_URL, selected_root.as_uri())),
        root=selected_root,
        name=selected_root.name or "workspace",
    )


def _is_ci_or_test() -> bool:
    """Return whether path safety is running under CI or pytest isolation."""
    return (
        os.environ.get("GITHUB_ACTIONS") == "true"
        or os.environ.get("CI") == "true"
        or os.environ.get("CORSIA_TEST_MODE") == "1"
    )


def _guard_against_prod_state_reuse(state_root: Path) -> None:
    """Prevent a distinct selected workspace from reusing persisted prod state."""
    workspace_env = os.environ.get("CORSIA_WORKSPACE")
    if not workspace_env:
        return

    prod_workspace = _read_prod_workspace_path()
    prod_state_root = _read_prod_state_root()
    if prod_workspace is None or prod_state_root is None:
        return

    selected_workspace = Path(workspace_env).expanduser().resolve()
    if selected_workspace != prod_workspace and state_root == prod_state_root:
        raise RuntimeError(
            "State resolution failed loud: "
            f"state={state_root} IS the prod state root while "
            f"workspace={selected_workspace} differs from the prod workspace "
            f"{prod_workspace}. Set CORSIA_STATE_DIR to isolated state."
        )


def _guard_against_prod_in_ci_or_tests(workspace: Path, *, source: str) -> None:
    """Prevent CI/tests from using the prod workspace.

    The authoritative check is equality with the bootstrap-configured prod
    workspace. HOME-prefix fallback only applies when no bootstrap config exists.
    """
    if os.environ.get("CORSIA_ALLOW_HOME_WORKSPACE_IN_CI") == "1":
        return
    if not _is_ci_or_test():
        return

    prod_workspace = _read_prod_workspace_path()
    if prod_workspace is not None:
        if workspace == prod_workspace:
            raise RuntimeError(
                f"CI workspace resolution failed loud: workspace={workspace} "
                f"IS the prod workspace (source: {source}). This would cause "
                f"SQLite/DuckDB lock contention with the live CORSIA and "
                f"hang it. Set CORSIA_WORKSPACE to an isolated path "
                f"(e.g., $RUNNER_TEMP/corsia-ci-workspace) at the workflow's "
                f"job-level env block, BEFORE pytest invokes any backend "
                f"import. Override (rare): CORSIA_ALLOW_HOME_WORKSPACE_IN_CI=1."
            )
        return

    home = Path.home().resolve()
    try:
        if workspace == home or workspace.is_relative_to(home):
            raise RuntimeError(
                f"CI workspace resolution failed loud: workspace={workspace} "
                f"is under HOME={home} and no bootstrap config exists to "
                f"verify prod separation (source: {source}). Set "
                f"CORSIA_WORKSPACE to an isolated path. Override: "
                f"CORSIA_ALLOW_HOME_WORKSPACE_IN_CI=1."
            )
    except ValueError:
        return


def _read_prod_workspace_path() -> Path | None:
    """Read the prod workspace path from the bootstrap config, or None."""
    if not _bootstrap_config_status():
        return None
    try:
        return load_bootstrap(BOOTSTRAP_CONFIG_PATH).workspace.root
    except (OSError, ValueError):
        return None


def _read_prod_state_root() -> Path | None:
    """Read the selected v2 or exact legacy production state root."""
    if not _bootstrap_config_status():
        return None
    try:
        return load_bootstrap(BOOTSTRAP_CONFIG_PATH).state_root
    except (OSError, ValueError):
        return None


def _is_exact_legacy_runtime_pair(
    workspace: Path,
    state_root: Path,
) -> bool:
    """Return whether the overlapping pair is the selected legacy layout."""
    if not _bootstrap_config_status():
        return False
    try:
        runtime_paths = load_bootstrap(BOOTSTRAP_CONFIG_PATH)
    except (OSError, ValueError):
        return False
    return (
        isinstance(runtime_paths, LegacyRuntimePaths)
        and workspace == runtime_paths.workspace.root
        and state_root == runtime_paths.state_root
    )


WORKSPACE, STATE_ROOT = _resolve_runtime_roots()
WORKSPACE_SELECTION = _resolve_workspace_selection(WORKSPACE)
_OP_DIR = STATE_ROOT
DB_PATH, ANALYTICS_DB_PATH, UPLOADS_DIR = runtime_db_paths(_OP_DIR)
# CORSIA's own captured log records (``infra.otel_logs``); never corsia.db.
OTEL_LOGS_DB_PATH = _OP_DIR / "otel_logs.db"
LOGS_DIR = _OP_DIR / "logs"
RUNTIME_DIR = _OP_DIR / "runtime"
FRONTEND_DIR = Path(__file__).parent.parent.parent / "frontend" / "dist"

# The generic ceiling for documents and other attachments. Images and video
# carry their own, larger ceilings so raising one media policy never silently
# widens what every other upload consumer accepts.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_IMAGE_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_VIDEO_UPLOAD_BYTES = 500 * 1024 * 1024
# Decoding and re-encoding an image that exceeds its provider's limits holds
# the decoded pixels plus encoded copies in memory; bound how many run at once.
MAX_CONCURRENT_IMAGE_NORMALIZATIONS = 2
# How large a frame a decode may open is a property of the machine, not a fixed
# number, so it is not a constant here: ``infra.host_resources`` owns it and
# resolves it on first use. This module is imported by the bootstrap that
# resolves workspace identity, before any dependency is installed, so it must
# not reach anything that needs a third-party package at import time.

VISION_MIME_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
VIDEO_MIME_TYPES = {"video/mp4", "video/quicktime", "video/webm", "video/x-matroska"}
DOCUMENT_MIME_TYPES = {"application/pdf", "text/plain", "text/markdown", "text/csv"}

PROVIDER_IMAGE_LIMITS: dict[str, dict | None] = {
    "anthropic": {
        "max_bytes": 5 * 1024 * 1024,
        "max_dimension": 8000,
        "max_dimension_multi": 2000,
        "multi_image_threshold": 20,
    },
    "openai": {
        "max_bytes": 20 * 1024 * 1024,
        "max_dimension": None,
        "max_dimension_multi": None,
        "multi_image_threshold": None,
    },
    "gemini": {
        "max_bytes": 100 * 1024 * 1024,
        "max_dimension": None,
        "max_dimension_multi": None,
        "multi_image_threshold": None,
    },
    "omlx":     None,
    "external": None,
}
