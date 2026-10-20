"""Locate the oMLX executable CORSIA manages itself."""

from __future__ import annotations

from pathlib import Path

from infra.platform_paths import app_bundle_root


def managed_omlx_executable(path: str | Path | None = None) -> str | None:
    """Return the bundled oMLX executable, or None outside the packaged app.

    Source installs retain their established PATH and Homebrew behavior. A
    packaged backend must use the executable shipped in its own app bundle, so
    a missing file is package damage rather than an installation problem.
    """
    bundle = app_bundle_root(path)
    if bundle is None:
        return None
    executable = bundle / "Contents" / "Resources" / "bin" / "omlx"
    if not executable.is_file():
        raise RuntimeError("The CORSIA app is damaged: its bundled oMLX executable is missing.")
    return str(executable)


def is_managed_omlx_executable(path: str | Path) -> bool:
    """Whether ``path`` resolves to the packaged oMLX CORSIA manages."""
    try:
        executable = managed_omlx_executable()
    except RuntimeError:
        return False  # a damaged bundle has no managed copy to match; status projections stay readable
    if executable is None:
        return False
    try:
        return Path(path).resolve() == Path(executable).resolve()
    except OSError:
        return False
