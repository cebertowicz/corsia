"""Pinned, fail-closed runtimes for CORSIA on macOS and Linux."""
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
import threading
import time
import urllib.error
import urllib.request
import zipfile

from infra.platform_paths import APP_FOLDER
from tools.gws_release import GWS_MAC_ARM64_SHA256, GWS_VERSION as _GWS_VERSION


class ManagedRuntimeError(RuntimeError):
    """A download or install that must not use an unpinned fallback."""


class ManagedRuntimeDownloadError(ManagedRuntimeError):
    """A pinned runtime could not be fetched or verified."""


@dataclass(frozen=True, slots=True)
class RuntimePin:
    version: str
    url: str
    sha256: str
    archive: str
    executable: str
    # Set instead of sha256 where the maker publishes SHA-512 only (npm's `integrity`).
    sha512: str = ""


def _pin_record(pin: RuntimePin) -> dict[str, str]:
    """The marker fields of a pin; an unset sha512 is left out so existing installs stay valid."""
    record = asdict(pin)
    if not record["sha512"]:
        del record["sha512"]
    return record


_NODE_VERSION = "26.12.0"
_UV_VERSION = "0.13.2"
_GH_VERSION = "2.102.0"
# The agents' Claude Code and Codex: each maker's own per-platform npm package, fetched
# directly (no npm at run time). Move a version only after the vendor-dependency check.
_CLAUDE_CODE_VERSION = "2.1.325"
_CODEX_VERSION = "0.169.0"
_NPM = "https:" "//registry.npmjs.org"


def _claude_pin(platform_name: str, sha512: str) -> RuntimePin:
    package = f"claude-code-{platform_name}"
    return RuntimePin(_CLAUDE_CODE_VERSION, f"{_NPM}/@anthropic-ai/{package}/-/{package}-{_CLAUDE_CODE_VERSION}.tgz", "", "tar.gz", "package/claude", sha512)


def _codex_pin(platform_name: str, triple: str, sha512: str) -> RuntimePin:
    return RuntimePin(_CODEX_VERSION, f"{_NPM}/@openai/codex/-/codex-{_CODEX_VERSION}-{platform_name}.tgz", "", "tar.gz", f"package/vendor/{triple}/bin/codex", sha512)


# Update pins only in source review: replace URL and maker-published SHA together.
RUNTIME_PINS: dict[str, dict[str, RuntimePin]] = {
    "node": {
        "arm64": RuntimePin(_NODE_VERSION, f"https://nodejs.org/dist/v{_NODE_VERSION}/node-v{_NODE_VERSION}-darwin-arm64.tar.gz", "2a154d4a5458930e0a2d6a1ecf64eddf5a2b3e1c13636b609c2ecb042e8c4553", "tar.gz", f"node-v{_NODE_VERSION}-darwin-arm64/bin/node"),
        "x86_64": RuntimePin(_NODE_VERSION, f"https://nodejs.org/dist/v{_NODE_VERSION}/node-v{_NODE_VERSION}-darwin-x64.tar.gz", "878ecede191c7abc1bff01d3fb81f7ed81aa1161040dba18776b8ff8593614b1", "tar.gz", f"node-v{_NODE_VERSION}-darwin-x64/bin/node"),
        "linux-x86_64": RuntimePin(_NODE_VERSION, f"https://nodejs.org/dist/v{_NODE_VERSION}/node-v{_NODE_VERSION}-linux-x64.tar.xz", "fd47865ecd6391ebb431fc815e01b9135bf83ded5cf373247e9331e196b7aded", "tar.xz", f"node-v{_NODE_VERSION}-linux-x64/bin/node"),
        "linux-arm64": RuntimePin(_NODE_VERSION, f"https://nodejs.org/dist/v{_NODE_VERSION}/node-v{_NODE_VERSION}-linux-arm64.tar.xz", "fd6cc729fa4448c2ac27c3fca5606019ced63c65a15c152c8df03e0cbce7f4c5", "tar.xz", f"node-v{_NODE_VERSION}-linux-arm64/bin/node"),
    },
    "uv": {
        "arm64": RuntimePin(_UV_VERSION, f"https://github.com/astral-sh/uv/releases/download/{_UV_VERSION}/uv-aarch64-apple-darwin.tar.gz", "4a5d878cd0db3a57d79eec638c275e71c394e10533ae54c77b33d940866f53b7", "tar.gz", "uv-aarch64-apple-darwin/uv"),
        "x86_64": RuntimePin(_UV_VERSION, f"https://github.com/astral-sh/uv/releases/download/{_UV_VERSION}/uv-x86_64-apple-darwin.tar.gz", "08f0cb0d63a012ffa07659d1872030c1110fccdd665d49bc3683b0ca262b4a62", "tar.gz", "uv-x86_64-apple-darwin/uv"),
        "linux-x86_64": RuntimePin(_UV_VERSION, f"https://github.com/astral-sh/uv/releases/download/{_UV_VERSION}/uv-x86_64-unknown-linux-gnu.tar.gz", "e14d48c117ec33601d6f860dda47aa5ea490d77a5a9ef770529c448fa6747d9f", "tar.gz", "uv-x86_64-unknown-linux-gnu/uv"),
        "linux-arm64": RuntimePin(_UV_VERSION, f"https://github.com/astral-sh/uv/releases/download/{_UV_VERSION}/uv-aarch64-unknown-linux-gnu.tar.gz", "3143e25fccac6f81b3b400792caa2ec431a5e8824d855cde9550a25f0fac8ea0", "tar.gz", "uv-aarch64-unknown-linux-gnu/uv"),
    },
    "gh": {
        "arm64": RuntimePin(_GH_VERSION, f"https://github.com/cli/cli/releases/download/v{_GH_VERSION}/gh_{_GH_VERSION}_macOS_arm64.zip", "da922c20d1792e5b2cbf375593d7a658acf034c12c84e007e71c76ef959c337e", "zip", f"gh_{_GH_VERSION}_macOS_arm64/bin/gh"),
        "x86_64": RuntimePin(_GH_VERSION, f"https://github.com/cli/cli/releases/download/v{_GH_VERSION}/gh_{_GH_VERSION}_macOS_amd64.zip", "b245f24eb2bf5f75b426b4c26da3651a107f8d5b6f4fddfbfccc5679041378b3", "zip", f"gh_{_GH_VERSION}_macOS_amd64/bin/gh"),
    },
    "claude-code": {
        "arm64": _claude_pin("darwin-arm64", "0ecd5fd41faacb60560c8f959e60c7c243724db5c2c05ac1b6b8a73c3f79441994b9b149bbd9c2ccc827fb0ae0e55c3017abed924cc67b1a209de64861929be8"),
        "x86_64": _claude_pin("darwin-x64", "fb04cae36213fdbe46647c4055530b3670871bf0ad9e0ced4c4e9f367e6ce21e3b2cd5eb0d0cf64693cbb18e571ebf1ab2d37393b1963d4f0679c086fb910421"),
        "linux-x86_64": _claude_pin("linux-x64", "06a67332f3ffce02cb2f7e12fbb3b36c6f9cca8169ee0c161d4e64a15c2cc8c953033fb4cd90266d8f94933bed376457c69e8070c7a76d8223409c685df518df"),
        "linux-arm64": _claude_pin("linux-arm64", "342bd070ec6bebce58e26918523413f25fcebb3c34936c5696859ecb37ec52d5a239e747a2c3e4696e97092a73025d5d32ebc334117deeaa568150eb49d2b657"),
    },
    "codex": {
        "arm64": _codex_pin("darwin-arm64", "aarch64-apple-darwin", "e56b161416d53fbf03fba16f368d4c315c4b3d9766728425a95245ac5e2ac7f4e6be778b9cc40ab4bc97914a39e173930dfa182a9368b9327f17b813540f059d"),
        "x86_64": _codex_pin("darwin-x64", "x86_64-apple-darwin", "2b514a4ac48b97dbf7cf94de6a00505f24110c1206079267732e5dc6c471c13dda693a8ddc023c56b09fa3107128c38e53d3b6417d6f83b5a4070355b61fecf4"),
        "linux-x86_64": _codex_pin("linux-x64", "x86_64-unknown-linux-musl", "a34e4dc803820f28123d1a35ca29f3c6f91149d9ac672043f44ee66e99a345a9d27ccf25ca9a96b50804a385df364c21b82476685bbc9a518eb101855d56d5c3"),
        "linux-arm64": _codex_pin("linux-arm64", "aarch64-unknown-linux-musl", "359fc36905297109b19aa79eed9f6e0b5af3c371a149ba286b071a4ded4b379624f969629d42867535c98029514fff6881010433d4d6c58cefc4f7e3e46ff116"),
    },
    "gws": {
        "arm64": RuntimePin(_GWS_VERSION, f"https://github.com/googleworkspace/cli/releases/download/v{_GWS_VERSION}/google-workspace-cli-aarch64-apple-darwin.tar.gz", GWS_MAC_ARM64_SHA256, "tar.gz", "gws"),
        # Intel Macs run from a source checkout only (the app is arm64), so this pin is used by scripts/install.sh alone.
        "x86_64": RuntimePin(_GWS_VERSION, f"https://github.com/googleworkspace/cli/releases/download/v{_GWS_VERSION}/google-workspace-cli-x86_64-apple-darwin.tar.gz", "51f9bd731404d4bba26c36e2e30dd68c56dccd1f834c01252cb0b14d6a6544b2", "tar.gz", "gws"),
        "linux-x86_64": RuntimePin(_GWS_VERSION, f"https://github.com/googleworkspace/cli/releases/download/v{_GWS_VERSION}/google-workspace-cli-x86_64-unknown-linux-musl.tar.gz", "4db473dde4b1ab872e4ff35d769b0d4af1f1a6441a605e79d5cf8ada9c87e920", "tar.gz", "gws"),
        "linux-arm64": RuntimePin(_GWS_VERSION, f"https://github.com/googleworkspace/cli/releases/download/v{_GWS_VERSION}/google-workspace-cli-aarch64-unknown-linux-musl.tar.gz", "e700fe63524932b10ec2130b47ece90aa850e66005fe52ccfc4cf8767bf9919a", "tar.gz", "gws"),
    },
}
COMMAND_RUNTIME = {"node": "node", "npm": "node", "npx": "node", "uv": "uv", "uvx": "uv", "gh": "gh"}
# The largest pinned archive is far below this cap; the cap bounds a compromised or
# malformed response before it can hold the connected-app lifecycle lock indefinitely.
_MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024
_DOWNLOAD_DEADLINE_SECONDS = 5 * 60
_RUNTIME_LOCKS: dict[str, threading.Lock] = {}
_RUNTIME_LOCKS_GUARD = threading.Lock()


def macos_arch(machine: str | None = None) -> str:
    normalized = (machine or platform.machine()).lower()
    if normalized in {"arm64", "aarch64"}:
        return "arm64"
    if normalized in {"x86_64", "amd64"}:
        return "x86_64"
    raise ManagedRuntimeError(f"CORSIA has no pinned runtime for architecture {normalized!r}")


def application_cache_root(home: Path | None = None) -> Path:
    """The CORSIA-owned cache parent whose children must not be agent-replaceable."""
    base = Path.home() if home is None else Path(home)
    if platform.system() == "Linux":
        # Not XDG_CACHE_HOME: the installer's shell and the systemd service can disagree on it.
        return base / ".cache" / "corsia"
    return base / "Library" / "Caches" / APP_FOLDER


def runtime_cache_root(home: Path | None = None) -> Path:
    """One executable cache, outside CORSIA state and private roots."""
    return application_cache_root(home) / "runtimes"


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
    return marker == {"kind": kind, "arch": arch, **_pin_record(pin)} and executable.is_file() and os.access(executable, os.X_OK)


def runtime_is_installed(kind: str, *, root: Path | None = None, arch: str | None = None) -> bool:
    selected, pin = _pin(kind, arch)
    return _valid_install(_install_dir(kind, selected, pin, root or runtime_cache_root()), kind, selected, pin)


def machine_key() -> str:
    """This machine's pin key: the Mac architecture, or ``linux-<arch>`` on Linux."""
    arch = macos_arch()
    return f"linux-{arch}" if platform.system() == "Linux" else arch


def pinned_executable_path(kind: str, *, root: Path | None = None) -> Path | None:
    """Where this machine's pinned executable lives once installed; fetches nothing.

    None only when no pin exists for this machine.
    """
    try:
        selected, pin = _pin(kind, machine_key())
    except ManagedRuntimeError:
        return None
    return _expected_executable(_install_dir(kind, selected, pin, root or runtime_cache_root()), pin)


def remove_other_versions(kind: str, *, root: Path | None = None) -> None:
    """Delete installed versions of ``kind`` other than this machine's current pin."""
    try:
        _selected, pin = _pin(kind, machine_key())
    except ManagedRuntimeError:
        return
    parent = (root or runtime_cache_root()) / kind
    with _runtime_lock(kind):
        for version_dir in parent.iterdir() if parent.is_dir() else ():
            if version_dir.name == pin.version or version_dir.name.startswith(".") or version_dir.is_symlink():
                continue
            _make_writable(version_dir)
            shutil.rmtree(version_dir, ignore_errors=True)


def installed_runtime_path(kind: str, *, root: Path | None = None) -> Path | None:
    """The verified executable of a pinned runtime for this machine, or None; fetches nothing."""
    try:
        selected = machine_key()
        _selected, pin = _pin(kind, selected)
    except ManagedRuntimeError:
        return None
    install = _install_dir(kind, selected, pin, root or runtime_cache_root())
    return _expected_executable(install, pin) if _valid_install(install, kind, selected, pin) else None


def runtime_fetch_plan(command: str, *, path: str | None = None, root: Path | None = None) -> tuple[str, RuntimePin] | None:
    """Describe a missing pinned download without downloading it."""
    kind = COMMAND_RUNTIME.get(command)
    if kind is None or shutil.which(command, path=path) is not None:
        return None
    selected, pin = _pin(kind)
    return None if runtime_is_installed(kind, root=root, arch=selected) else (kind, pin)


def _download(url: str, destination: Path) -> str:
    digest = hashlib.sha256()
    deadline = time.monotonic() + _DOWNLOAD_DEADLINE_SECONDS
    received = 0
    try:
        with urllib.request.urlopen(url, timeout=30) as response, destination.open("xb") as output:
            while chunk := response.read(1024 * 1024):
                if time.monotonic() >= deadline:
                    raise TimeoutError("pinned runtime download exceeded its deadline")
                received += len(chunk)
                if received > _MAX_DOWNLOAD_BYTES:
                    raise ManagedRuntimeError("pinned runtime download exceeded its byte limit")
                digest.update(chunk)
                output.write(chunk)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError) as exc:
        raise ManagedRuntimeDownloadError(f"download failed: {type(exc).__name__}") from exc
    return digest.hexdigest()


def _matches_pin(sha256: str, archive: Path, pin: RuntimePin) -> bool:
    """Check the download against its pin; a SHA-512 pin re-reads the archive."""
    if not pin.sha512:
        return sha256 == pin.sha256
    digest = hashlib.sha512()
    with archive.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest() == pin.sha512


def _safe_member(name: str) -> bool:
    path = Path(name)
    return not path.is_absolute() and ".." not in path.parts


def _safe_tar_member(member: tarfile.TarInfo) -> bool:
    if not _safe_member(member.name):
        return False
    if not (member.issym() or member.islnk()):
        return True
    target = Path(member.linkname)
    if member.issym():
        target = Path(member.name).parent / target
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
    if pin.archive in {"tar.gz", "tar.xz"}:
        with tarfile.open(archive, "r:gz" if pin.archive == "tar.gz" else "r:xz") as source:
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


def _make_writable(path: Path) -> None:
    """Restore enough permissions to remove a prior immutable failed install."""
    if path.is_symlink():
        path.unlink()
        return
    for item in sorted(path.rglob("*"), reverse=True):
        if not item.is_symlink():
            item.chmod(0o755 if item.is_dir() else 0o644)
    path.chmod(0o755)


def _runtime_lock(kind: str) -> threading.Lock:
    """Return the one process-local install lock for a pinned runtime kind."""
    with _RUNTIME_LOCKS_GUARD:
        return _RUNTIME_LOCKS.setdefault(kind, threading.Lock())


def ensure_runtime(kind: str, *, root: Path | None = None, arch: str | None = None) -> Path:
    """Fetch one pinned runtime atomically, or return its verified executable."""
    selected, pin = _pin(kind, arch)
    base = root or runtime_cache_root()
    final = _install_dir(kind, selected, pin, base)
    with _runtime_lock(kind):
        if _valid_install(final, kind, selected, pin):
            return _expected_executable(final, pin)
        staging: Path | None = None
        try:
            if final.exists():
                _make_writable(final)
                shutil.rmtree(final)
            final.parent.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix=f".{kind}-{pin.version}-", dir=final.parent))
            archive = staging / "download"
            if not _matches_pin(_download(pin.url, archive), archive, pin):
                raise ManagedRuntimeDownloadError("download checksum did not match the pinned runtime")
            unpacked = staging / "runtime"
            unpacked.mkdir()
            _extract(archive, unpacked, pin)
            executable = _expected_executable(unpacked, pin)
            if not executable.is_file():
                raise ManagedRuntimeError("download did not contain the pinned executable")
            executable.chmod(executable.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            _marker_path(unpacked).write_text(json.dumps({"kind": kind, "arch": selected, **_pin_record(pin)}, sort_keys=True), encoding="utf-8")
            os.replace(unpacked, final)
            _make_read_execute_only(final)
            return _expected_executable(final, pin)
        except ManagedRuntimeError:
            raise
        except (OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
            raise ManagedRuntimeError(f"pinned runtime install failed: {type(exc).__name__}") from exc
        finally:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)


def command_path(command: str, *, path: str | None = None, root: Path | None = None) -> str | None:
    """Resolve a person install first, then only a matching CORSIA pin."""
    if found := shutil.which(command, path=path):
        return found
    kind = COMMAND_RUNTIME.get(command)
    if kind is None:
        return None
    executable = ensure_runtime(kind, root=root)
    return str(executable if command == "node" else executable.with_name(command))


def runtime_bin_dir(kind: str, *, root: Path | None = None) -> Path | None:
    """Where a pinned runtime's commands will be once installed; fetches nothing."""
    try:
        selected, pin = _pin(kind)
    except ManagedRuntimeError:
        return None
    return _expected_executable(_install_dir(kind, selected, pin, root or runtime_cache_root()), pin).parent


def command_bin_dir(command: str, *, root: Path | None = None) -> Path | None:
    kind = COMMAND_RUNTIME.get(command)
    return ensure_runtime(kind, root=root).parent if kind else None


def installed_command_path(command: str, *, root: Path | None = None) -> str | None:
    """Return a previously verified CORSIA command without fetching anything."""
    kind = COMMAND_RUNTIME.get(command)
    if kind is None or not runtime_is_installed(kind, root=root):
        return None
    _arch, pin = _pin(kind)
    executable = _expected_executable(_install_dir(kind, macos_arch(), pin, root or runtime_cache_root()), pin)
    return str(executable if command == "node" else executable.with_name(command))
