"""Pinned, fail-closed runtimes CORSIA may fetch on demand on macOS."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
import platform
import shutil
import stat
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile

from infra.platform_paths import APPLICATION_ID


class ManagedRuntimeError(RuntimeError):
    """A download or install that must not use an unpinned fallback."""


@dataclass(frozen=True, slots=True)
class RuntimePin:
    version: str
    url: str
    sha256: str
    archive: str
    executable: str


_NODE_VERSION = "26.12.0"
_UV_VERSION = "0.13.2"
_GH_VERSION = "2.102.0"

# Update pins only in source review: replace URL and maker-published SHA together.
RUNTIME_PINS: dict[str, dict[str, RuntimePin]] = {
    "node": {
        "arm64": RuntimePin(_NODE_VERSION, f"https://nodejs.org/dist/v{_NODE_VERSION}/node-v{_NODE_VERSION}-darwin-arm64.tar.gz", "2a154d4a5458930e0a2d6a1ecf64eddf5a2b3e1c13636b609c2ecb042e8c4553", "tar.gz", f"node-v{_NODE_VERSION}-darwin-arm64/bin/node"),
        "x86_64": RuntimePin(_NODE_VERSION, f"https://nodejs.org/dist/v{_NODE_VERSION}/node-v{_NODE_VERSION}-darwin-x64.tar.gz", "878ecede191c7abc1bff01d3fb81f7ed81aa1161040dba18776b8ff8593614b1", "tar.gz", f"node-v{_NODE_VERSION}-darwin-x64/bin/node"),
    },
    "uv": {
        "arm64": RuntimePin(_UV_VERSION, f"https://github.com/astral-sh/uv/releases/download/{_UV_VERSION}/uv-aarch64-apple-darwin.tar.gz", "4a5d878cd0db3a57d79eec638c275e71c394e10533ae54c77b33d940866f53b7", "tar.gz", "uv-aarch64-apple-darwin/uv"),
        "x86_64": RuntimePin(_UV_VERSION, f"https://github.com/astral-sh/uv/releases/download/{_UV_VERSION}/uv-x86_64-apple-darwin.tar.gz", "08f0cb0d63a012ffa07659d1872030c1110fccdd665d49bc3683b0ca262b4a62", "tar.gz", "uv-x86_64-apple-darwin/uv"),
    },
    "gh": {
        "arm64": RuntimePin(_GH_VERSION, f"https://github.com/cli/cli/releases/download/v{_GH_VERSION}/gh_{_GH_VERSION}_macOS_arm64.zip", "da922c20d1792e5b2cbf375593d7a658acf034c12c84e007e71c76ef959c337e", "zip", f"gh_{_GH_VERSION}_macOS_arm64/bin/gh"),
        "x86_64": RuntimePin(_GH_VERSION, f"https://github.com/cli/cli/releases/download/v{_GH_VERSION}/gh_{_GH_VERSION}_macOS_amd64.zip", "b245f24eb2bf5f75b426b4c26da3651a107f8d5b6f4fddfbfccc5679041378b3", "zip", f"gh_{_GH_VERSION}_macOS_amd64/bin/gh"),
    },
}
_COMMAND_RUNTIME = {"node": "node", "npx": "node", "uv": "uv", "uvx": "uv", "gh": "gh"}


def macos_arch(machine: str | None = None) -> str:
    normalized = (machine or platform.machine()).lower()
    if normalized in {"arm64", "aarch64"}:
        return "arm64"
    if normalized in {"x86_64", "amd64"}:
        return "x86_64"
    raise ManagedRuntimeError(f"CORSIA has no pinned runtime for architecture {normalized!r}")


def runtime_cache_root(home: Path | None = None) -> Path:
    """One executable cache, outside CORSIA state and private roots."""
    return (Path.home() if home is None else Path(home)) / "Library" / "Caches" / APPLICATION_ID / "runtimes"


def _pin(kind: str, arch: str | None = None) -> tuple[str, RuntimePin]:
    try:
        selected = arch or macos_arch()
        return selected, RUNTIME_PINS[kind][selected]
    except KeyError as exc:
        raise ManagedRuntimeError(f"CORSIA has no pinned {kind} runtime for this Mac") from exc


def _install_dir(kind: str, arch: str, pin: RuntimePin, root: Path) -> Path:
    return root / kind / pin.version / arch


def _expected_executable(directory: Path, pin: RuntimePin) -> Path:
    return directory / pin.executable


def _marker_path(directory: Path) -> Path:
    return directory / ".corsia-runtime.json"


def _valid_install(directory: Path, kind: str, arch: str, pin: RuntimePin) -> bool:
    try:
        marker = json.loads(_marker_path(directory).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    executable = _expected_executable(directory, pin)
    return marker == {"kind": kind, "arch": arch, **asdict(pin)} and executable.is_file() and os.access(executable, os.X_OK)


def runtime_is_installed(kind: str, *, root: Path | None = None, arch: str | None = None) -> bool:
    selected, pin = _pin(kind, arch)
    return _valid_install(_install_dir(kind, selected, pin, root or runtime_cache_root()), kind, selected, pin)


def runtime_fetch_plan(command: str, *, path: str | None = None, root: Path | None = None) -> tuple[str, RuntimePin] | None:
    """Describe a missing pinned download without downloading it."""
    kind = _COMMAND_RUNTIME.get(command)
    if kind is None or shutil.which(command, path=path) is not None:
        return None
    selected, pin = _pin(kind)
    return None if runtime_is_installed(kind, root=root, arch=selected) else (kind, pin)


def _download(url: str, destination: Path) -> str:
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(url, timeout=30) as response, destination.open("xb") as output:
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
                output.write(chunk)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError) as exc:
        raise ManagedRuntimeError(f"download failed: {type(exc).__name__}") from exc
    return digest.hexdigest()


def _safe_member(name: str) -> bool:
    path = Path(name)
    return not path.is_absolute() and ".." not in path.parts


def _safe_tar_member(member: tarfile.TarInfo) -> bool:
    if not _safe_member(member.name):
        return False
    if not (member.issym() or member.islnk()):
        return True
    target = Path(member.name).parent / member.linkname
    if target.is_absolute():
        return False
    depth = 0
    for part in target.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if depth == 0:
                return False
            depth -= 1
        else:
            depth += 1
    return True


def _extract(archive: Path, destination: Path, pin: RuntimePin) -> None:
    if pin.archive == "tar.gz":
        with tarfile.open(archive, "r:gz") as source:
            tar_members = source.getmembers()
            if any(not _safe_tar_member(member) for member in tar_members):
                raise ManagedRuntimeError("download archive has an unsafe path")
            for member in tar_members:
                source.extract(member, destination)
    elif pin.archive == "zip":
        with zipfile.ZipFile(archive) as source:
            zip_members = source.infolist()
            if any(not _safe_member(member.filename) or stat.S_ISLNK(member.external_attr >> 16) for member in zip_members):
                raise ManagedRuntimeError("download archive has an unsafe path")
            source.extractall(destination, members=zip_members)
    else:
        raise ManagedRuntimeError("unsupported pinned archive format")


def _make_read_execute_only(path: Path) -> None:
    for item in sorted(path.rglob("*"), reverse=True):
        mode = item.stat().st_mode
        item.chmod(0o555 if item.is_dir() or mode & stat.S_IXUSR else 0o444)
    path.chmod(0o555)


def ensure_runtime(kind: str, *, root: Path | None = None, arch: str | None = None) -> Path:
    """Fetch one pinned runtime atomically, or return its verified executable."""
    selected, pin = _pin(kind, arch)
    base = root or runtime_cache_root()
    final = _install_dir(kind, selected, pin, base)
    if _valid_install(final, kind, selected, pin):
        return _expected_executable(final, pin)
    if final.exists():
        shutil.rmtree(final)
    final.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{kind}-{pin.version}-", dir=final.parent))
    try:
        archive = staging / "download"
        if _download(pin.url, archive) != pin.sha256:
            raise ManagedRuntimeError("download checksum did not match the pinned runtime")
        unpacked = staging / "runtime"
        unpacked.mkdir()
        _extract(archive, unpacked, pin)
        executable = _expected_executable(unpacked, pin)
        if not executable.is_file():
            raise ManagedRuntimeError("download did not contain the pinned executable")
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        _marker_path(unpacked).write_text(json.dumps({"kind": kind, "arch": selected, **asdict(pin)}, sort_keys=True), encoding="utf-8")
        os.replace(unpacked, final)
        _make_read_execute_only(final)
        return _expected_executable(final, pin)
    except ManagedRuntimeError:
        raise
    except (OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
        raise ManagedRuntimeError(f"pinned runtime install failed: {type(exc).__name__}") from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def command_path(command: str, *, path: str | None = None, root: Path | None = None) -> str | None:
    """Resolve a person install first, then only a matching CORSIA pin."""
    if found := shutil.which(command, path=path):
        return found
    kind = _COMMAND_RUNTIME.get(command)
    if kind is None:
        return None
    executable = ensure_runtime(kind, root=root)
    return str(executable if command == "node" else executable.with_name(command))


def command_bin_dir(command: str, *, root: Path | None = None) -> Path | None:
    kind = _COMMAND_RUNTIME.get(command)
    return ensure_runtime(kind, root=root).parent if kind else None


def installed_command_path(command: str, *, root: Path | None = None) -> str | None:
    """Return a previously verified CORSIA command without fetching anything."""
    kind = _COMMAND_RUNTIME.get(command)
    if kind is None or not runtime_is_installed(kind, root=root):
        return None
    _arch, pin = _pin(kind)
    executable = _expected_executable(_install_dir(kind, macos_arch(), pin, root or runtime_cache_root()), pin)
    return str(executable if command == "node" else executable.with_name(command))
