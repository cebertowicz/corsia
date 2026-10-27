"""Authenticate managed browser executables before any unsandboxed probe or launch."""
from __future__ import annotations

import hashlib
import platform
import plistlib
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from host_access import browser_identity as identity, browser_launch
from infra.platform_paths import app_bundle_root

# Upstream v0.31.2 asset digests; packaged macOS dependencies are re-signed by the app.
PINNED_NATIVE_VERSION = "0.31.2"
NATIVE_SHA256 = {
    ("Darwin", "arm64"): "98042bcfee9afdf6119d6817a04ef5897f519f290491664911779b084ae1b82a",
    ("Darwin", "x86_64"): "9884a0a43ce2173423c56ec05f618d76ecc7e6ff342f74fb33df598af9bf7168",
    ("Linux", "aarch64"): "18dfe2909e4dfa387899d80dc36ac7c41de17579fe0d5accb995ea754b488b1f",
    ("Linux", "x86_64"): "a3e066710b457aa9a38a0748fedd98d00a626ee74631da9f47ca45b1bdd0410b",
}
GOOGLE_REQUIREMENT = 'anchor apple generic and certificate leaf[subject.OU] = "EQHXZ8M8AV"'
SYSTEM_CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


def runtime_root() -> Path:
    """Reuse the profile subtree's non-leaseable private-state protection."""
    root = browser_launch.profile_root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    identity.ensure_private_dir(root)
    return identity.ensure_private_dir(root / "runtime")


def private_native(source: str) -> str:
    """Copy only authenticated bytes; the writable original is never executed."""
    bundle = app_bundle_root()
    if bundle is not None and Path(source).resolve() == (bundle / "Contents/Resources/bin/agent-browser").resolve():
        return _packaged_native(Path(source), bundle)
    expected = NATIVE_SHA256.get((platform.system(), platform.machine()))
    content = Path(source).read_bytes()
    if expected is None or hashlib.sha256(content).hexdigest() != expected:
        raise identity.BrowserIdentityError("agent-browser must match the pinned native 0.31.2 release; reinstall agent-browser 0.31.2")
    target = runtime_root() / f"agent-browser-{expected}"
    if target.is_symlink() or not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != expected:
        with tempfile.NamedTemporaryFile(prefix="agent-browser-copy-", dir=runtime_root(), delete=False) as output:
            staged = Path(output.name)
        try:
            staged.write_bytes(content)
            staged.chmod(0o700)
            staged.replace(target)
        finally:
            staged.unlink(missing_ok=True)
    if target.is_symlink() or hashlib.sha256(target.read_bytes()).hexdigest() != expected:
        raise identity.BrowserIdentityError("the private agent-browser executable failed integrity verification")
    return str(target)


def _packaged_native(source: Path, bundle: Path) -> str:
    """A signed app re-signs its native dependency; authenticate the private copy to its team."""
    try:
        with (bundle / "Contents/Info.plist").open("rb") as metadata:
            main = bundle / "Contents/MacOS" / plistlib.load(metadata)["CFBundleExecutable"]
        details = subprocess.run(["/usr/bin/codesign", "--display", "--verbose=4", str(main)],
                                 capture_output=True, text=True, timeout=30)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        raise identity.BrowserIdentityError("Shared browser cannot read the app signing identity; reinstall a signed build") from exc
    team = next((line.removeprefix("TeamIdentifier=") for line in details.stderr.splitlines()
                 if line.startswith("TeamIdentifier=")), "")
    if details.returncode or "Signature=adhoc" in details.stderr or not re.fullmatch(r"[A-Za-z0-9]+", team):
        raise identity.BrowserIdentityError("Shared browser is unavailable in an unsigned app build; install a signed build")
    content = source.read_bytes()
    target = runtime_root() / f"agent-browser-bundled-{hashlib.sha256(content).hexdigest()}"
    try:
        if target.is_symlink() or not target.is_file() or target.read_bytes() != content:
            with tempfile.NamedTemporaryFile(prefix="agent-browser-copy-", dir=runtime_root(), delete=False) as output:
                staged = Path(output.name)
            try:
                staged.write_bytes(content)
                staged.chmod(0o700)
                staged.replace(target)
            finally:
                staged.unlink(missing_ok=True)
        requirement = f'anchor apple generic and certificate leaf[subject.OU] = "{team}"'
        verified = subprocess.run(["/usr/bin/codesign", "--verify", "--strict", f"-R={requirement}", str(target)],
                                  capture_output=True, text=True, timeout=30)
        if verified.returncode:
            raise identity.BrowserIdentityError("Shared browser's bundled native signature could not be verified; reinstall a signed build")
        return str(target)
    except BaseException as exc:
        target.unlink(missing_ok=True)
        if isinstance(exc, (OSError, subprocess.SubprocessError)):
            raise identity.BrowserIdentityError("Shared browser's bundled native signature could not be verified; reinstall a signed build") from exc
        raise


def verify_google(executable: str) -> None:
    """Verify the complete app against Google's identity without executing its code."""
    try:
        path = Path(executable).resolve(strict=True)
        app = next((parent for parent in path.parents if parent.suffix == ".app"), None)
        if platform.system() != "Darwin" or app is None:
            raise identity.BrowserIdentityError("Chrome has no verifiable Google application signature")
        result = subprocess.run(["/usr/bin/codesign", "--verify", "--deep", f"-R={GOOGLE_REQUIREMENT}", str(app)],
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise identity.BrowserIdentityError("Chrome's Google application signature could not be checked; try browser setup again") from exc
    if result.returncode:
        raise identity.BrowserIdentityError("Chrome's Google application signature could not be verified")


def trusted_chrome(source: str) -> str:
    """System Chrome is protected in place; writable app copies run only from private state."""
    if Path(source).resolve(strict=True) == SYSTEM_CHROME:
        verify_google(source)
        return str(SYSTEM_CHROME)
    path = Path(source).resolve(strict=True)
    app = next((parent for parent in path.parents if parent.suffix == ".app"), None)
    if app is None or platform.system() != "Darwin":
        raise identity.BrowserIdentityError("Chrome could not be authenticated for managed launch")
    # Verify the installed source and the private copy; never execute an unverified CFT download.
    verify_google(source)
    target = runtime_root() / app.name
    copied = target / path.relative_to(app)
    try:
        if not target.is_symlink() and copied.is_file():
            if not copied.resolve(strict=True).is_relative_to(runtime_root()):
                raise identity.BrowserIdentityError("the private Chrome executable path is invalid")
            verify_google(str(copied))
            return str(copied)
    except identity.BrowserIdentityError:
        pass  # A partial private copy is replaced from the authenticated source.
    if target.is_symlink():
        target.unlink()
    elif target.exists():
        shutil.rmtree(target)
    with tempfile.TemporaryDirectory(prefix="chrome-copy-", dir=runtime_root()) as stage:
        staged = Path(stage) / app.name
        try:
            result = subprocess.run(["/usr/bin/ditto", "--norsrc", str(app), str(staged)],
                                    capture_output=True, text=True, timeout=120)
            if result.returncode:
                raise identity.BrowserIdentityError("Chrome could not be copied into private state")
            staged_executable = (staged / path.relative_to(app)).resolve(strict=True)
            if not staged_executable.is_relative_to(staged):
                raise identity.BrowserIdentityError("the private Chrome executable path is invalid")
            verify_google(str(staged_executable))
            staged.replace(target)
        except (OSError, subprocess.SubprocessError) as exc:
            raise identity.BrowserIdentityError("Chrome could not be copied into private state; try browser setup again") from exc
    return str(copied)
